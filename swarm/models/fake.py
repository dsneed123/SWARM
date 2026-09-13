"""In-memory backend for tests and for exploring the TUI without a GPU.

Responses come from a ``responder`` callable so tests can script exact model
behaviour. Memory sizes and latencies are simulated so scheduler and budget
logic can be exercised deterministically.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from swarm.models.backend import (
    BackendError,
    ChatMessage,
    ChatResult,
    LoadedModel,
    ModelBackend,
    ModelDescriptor,
    ToolSpec,
)

Responder = Callable[[str, list[ChatMessage], list[ToolSpec] | None, dict[str, Any] | None], Any]


@dataclass
class FakeModel:
    name: str
    parameters_b: float
    size_gb: float
    context_length: int = 32768
    capabilities: set[str] = field(default_factory=lambda: {"completion", "tools"})
    specialties: set[str] = field(default_factory=set)
    latency_s: float = 0.0
    tokens_per_s: float = 50.0


def default_responder(model: str, messages: list[ChatMessage], tools, schema) -> Any:
    """Echo-style default: returns a JSON object when a schema is requested."""
    last = messages[-1].content if messages else ""
    if schema:
        return {"conclusion": f"[{model}] {last[:80]}", "confidence": 0.7}
    return f"[{model}] {last[:80]}"


class FakeBackend(ModelBackend):
    name = "fake"

    def __init__(
        self,
        models: list[FakeModel] | None = None,
        responder: Responder | None = None,
    ) -> None:
        self.models = {m.name: m for m in (models or default_models())}
        self.responder = responder or default_responder
        self._loaded: dict[str, int] = {}
        self.calls: list[dict[str, Any]] = []
        self.active: dict[str, int] = {}
        self.max_active: dict[str, int] = {}
        self.load_events: list[tuple[str, str]] = []
        self.healthy = True

    async def health(self) -> bool:
        return self.healthy

    async def list_models(self) -> list[ModelDescriptor]:
        return [
            ModelDescriptor(
                name=m.name,
                backend=self.name,
                family="fake",
                parameters_b=m.parameters_b,
                quantization="Q4_K_M",
                size_bytes=int(m.size_gb * 1024**3),
                context_length=m.context_length,
                capabilities=set(m.capabilities),
                specialties=set(m.specialties),
            )
            for m in self.models.values()
        ]

    async def loaded(self) -> list[LoadedModel]:
        return [
            LoadedModel(name=n, size_bytes=int(self.models[n].size_gb * 1024**3 * 1.1), context_length=ctx)
            for n, ctx in self._loaded.items()
        ]

    async def load(self, model: str, num_ctx: int, keep_alive: str | int = "30m") -> None:
        if model not in self.models:
            raise BackendError(f"unknown model {model}")
        self._loaded[model] = num_ctx
        self.load_events.append(("load", model))

    async def unload(self, model: str) -> None:
        self._loaded.pop(model, None)
        self.load_events.append(("unload", model))

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
        if model not in self.models:
            raise BackendError(f"unknown model {model}")
        if model not in self._loaded:
            self._loaded[model] = num_ctx or 8192
            self.load_events.append(("load", model))
        self.calls.append({"model": model, "messages": messages, "tools": tools, "schema": json_schema})
        self.active[model] = self.active.get(model, 0) + 1
        self.max_active[model] = max(self.max_active.get(model, 0), self.active[model])
        try:
            fm = self.models[model]
            if fm.latency_s:
                await asyncio.sleep(fm.latency_s)
            out = self.responder(model, messages, tools, json_schema)
            if asyncio.iscoroutine(out):
                out = await out
        finally:
            self.active[model] -= 1
        if isinstance(out, ChatResult):
            return out
        if isinstance(out, dict | list):
            out = json.dumps(out)
        text = str(out)
        n = max(1, len(text) // 4)
        return ChatResult(
            content=text,
            prompt_tokens=sum(len(m.content) // 4 for m in messages),
            completion_tokens=n,
            eval_s=n / fm.tokens_per_s,
            total_duration_s=fm.latency_s + n / fm.tokens_per_s,
            done_reason="stop",
        )


def default_models() -> list[FakeModel]:
    return [
        FakeModel("small:7b", 7, 4.5),
        FakeModel("medium:14b", 14, 9.0),
        FakeModel("medium-coder:32b", 32, 20.0, specialties={"coding"}),
        FakeModel("large:70b", 70, 42.0, capabilities={"completion", "tools", "thinking"}),
    ]
