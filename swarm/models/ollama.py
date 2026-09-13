"""Ollama backend over its HTTP API."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from swarm.models.backend import (
    BackendError,
    ChatMessage,
    ChatResult,
    LoadedModel,
    ModelBackend,
    ModelDescriptor,
    ToolCall,
    ToolSpec,
)

log = logging.getLogger(__name__)

_SPECIALTY_HINTS = {
    "coding": ("coder", "code", "starcoder", "codellama", "devstral"),
    "reasoning": ("r1", "reason", "think", "qwq", "phi-4-reasoning"),
    "vision": ("vision", "llava", "vl"),
}


def _parse_params(text: str | None) -> float:
    if not text:
        return 0.0
    m = re.match(r"([\d.]+)\s*([BbMm])", text.strip())
    if not m:
        return 0.0
    n = float(m.group(1))
    return n / 1000 if m.group(2).lower() == "m" else n


def specialties_from_name(name: str) -> set[str]:
    lname = name.lower()
    return {spec for spec, hints in _SPECIALTY_HINTS.items() if any(h in lname for h in hints)}


class OllamaBackend(ModelBackend):
    name = "ollama"

    def __init__(self, host: str = "http://localhost:11434", timeout_s: float = 600.0) -> None:
        if not host.startswith("http"):
            host = "http://" + host
        self.host = host.rstrip("/")
        self._client = httpx.AsyncClient(base_url=self.host, timeout=httpx.Timeout(timeout_s))
        self._descriptor_cache: dict[str, ModelDescriptor] = {}

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str) -> Any:
        try:
            r = await self._client.get(path)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as e:
            raise BackendError(f"ollama GET {path}: {e}") from e

    async def _post(self, path: str, body: dict[str, Any]) -> Any:
        try:
            r = await self._client.post(path, json=body)
            if r.status_code >= 400:
                raise BackendError(f"ollama POST {path}: {r.status_code} {r.text[:300]}")
            return r.json()
        except httpx.HTTPError as e:
            raise BackendError(f"ollama POST {path}: {e}") from e

    async def health(self) -> bool:
        try:
            await self._get("/api/version")
            return True
        except BackendError:
            return False

    async def list_models(self) -> list[ModelDescriptor]:
        data = await self._get("/api/tags")
        out: list[ModelDescriptor] = []
        for m in data.get("models", []):
            name = m["name"]
            try:
                desc = await self.describe(name, m)
            except BackendError as e:
                log.warning("could not describe %s: %s", name, e)
                continue
            out.append(desc)
        return out

    async def describe(self, name: str, tag: dict[str, Any] | None = None) -> ModelDescriptor:
        if name in self._descriptor_cache:
            return self._descriptor_cache[name]
        show = await self._post("/api/show", {"model": name})
        details = show.get("details", {}) or (tag or {}).get("details", {})
        info = show.get("model_info", {}) or {}
        family = details.get("family", "")
        arch: dict[str, Any] = {}
        ctx = 0
        for key, value in info.items():
            # keys look like "llama.context_length", "qwen3moe.block_count"
            if "." not in key:
                continue
            _, _, leaf = key.partition(".")
            if leaf == "context_length":
                ctx = int(value)
            elif leaf in ("block_count", "embedding_length"):
                arch[leaf] = value
            elif leaf == "attention.head_count":
                arch["head_count"] = value
            elif leaf == "attention.head_count_kv":
                arch["head_count_kv"] = value
        caps = set(show.get("capabilities") or [])
        size = int((tag or {}).get("size") or 0)
        desc = ModelDescriptor(
            name=name,
            backend=self.name,
            family=family,
            parameters_b=_parse_params(details.get("parameter_size")),
            quantization=details.get("quantization_level", ""),
            size_bytes=size,
            context_length=ctx,
            capabilities=caps or {"completion"},
            architecture=arch,
            specialties=specialties_from_name(name),
        )
        self._descriptor_cache[name] = desc
        return desc

    async def loaded(self) -> list[LoadedModel]:
        data = await self._get("/api/ps")
        return [
            LoadedModel(
                name=m["name"],
                size_bytes=int(m.get("size_vram") or m.get("size") or 0),
                context_length=int(m.get("context_length") or 0),
                expires_at=m.get("expires_at"),
            )
            for m in data.get("models", [])
        ]

    async def load(self, model: str, num_ctx: int, keep_alive: str | int = "30m") -> None:
        await self._post(
            "/api/chat",
            {
                "model": model,
                "messages": [],
                "keep_alive": keep_alive,
                "options": {"num_ctx": num_ctx},
                "stream": False,
            },
        )

    async def unload(self, model: str) -> None:
        await self._post("/api/chat", {"model": model, "messages": [], "keep_alive": 0, "stream": False})

    async def chat(
        self,
        model: str,
        messages: list[ChatMessage],
        *,
        tools: list[ToolSpec] | None = None,
        json_schema: dict[str, Any] | None = None,
        num_ctx: int | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        keep_alive: str | int | None = None,
        think: bool | None = None,
    ) -> ChatResult:
        body: dict[str, Any] = {
            "model": model,
            "messages": [m.as_dict() for m in messages],
            "stream": False,
        }
        options: dict[str, Any] = {}
        if num_ctx:
            options["num_ctx"] = num_ctx
        if temperature is not None:
            options["temperature"] = temperature
        if max_tokens:
            options["num_predict"] = max_tokens
        if options:
            body["options"] = options
        if tools:
            body["tools"] = [t.as_openai() for t in tools]
        if json_schema:
            body["format"] = json_schema
        if keep_alive is not None:
            body["keep_alive"] = keep_alive
        if think is not None:
            body["think"] = think
        data = await self._post("/api/chat", body)
        msg = data.get("message", {}) or {}
        calls = []
        for tc in msg.get("tool_calls", []) or []:
            fn = tc.get("function", {})
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"_raw": args}
            calls.append(ToolCall(name=fn.get("name", ""), arguments=args or {}, id=tc.get("id", "")))
        ns = 1e9
        return ChatResult(
            content=msg.get("content", "") or "",
            tool_calls=calls,
            thinking=msg.get("thinking", "") or "",
            prompt_tokens=int(data.get("prompt_eval_count") or 0),
            completion_tokens=int(data.get("eval_count") or 0),
            total_duration_s=(data.get("total_duration") or 0) / ns,
            load_duration_s=(data.get("load_duration") or 0) / ns,
            prompt_eval_s=(data.get("prompt_eval_duration") or 0) / ns,
            eval_s=(data.get("eval_duration") or 0) / ns,
            done_reason=data.get("done_reason", ""),
        )
