"""Model backend abstraction.

A backend knows how to list, load, unload and talk to models. Everything
above this layer (router, scheduler, agents) works with these types only, so
adding llama.cpp, vLLM or TensorRT-LLM later means implementing one class.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ModelDescriptor:
    name: str
    backend: str
    family: str = ""
    parameters_b: float = 0.0  # billions
    quantization: str = ""
    size_bytes: int = 0  # on-disk weights
    context_length: int = 0  # maximum the model supports
    capabilities: set[str] = field(default_factory=set)  # completion, tools, thinking, vision, embedding
    architecture: dict[str, Any] = field(default_factory=dict)  # block_count, kv heads ...
    specialties: set[str] = field(default_factory=set)  # coding, reasoning ...

    @property
    def is_chat_model(self) -> bool:
        return "embedding" not in self.capabilities or "completion" in self.capabilities

    def kv_bytes_per_token(self) -> int:
        """Bytes of KV cache per context token (f16 cache)."""
        a = self.architecture
        try:
            layers = int(a["block_count"])
            kv_heads = int(a.get("head_count_kv") or a["head_count"])
            head_dim = int(a["embedding_length"]) // int(a["head_count"])
            return layers * kv_heads * head_dim * 2 * 2
        except (KeyError, ValueError, ZeroDivisionError, TypeError):
            # Heuristic anchored on llama-3 8B (~128 KiB/token).
            scale = max(self.parameters_b, 1.0) / 8.0
            return int(128 * 1024 * scale**0.5)

    def estimate_memory(self, num_ctx: int, parallel: int = 1) -> int:
        """Estimated resident memory for weights + KV cache at ``num_ctx``."""
        weights = int(self.size_bytes * 1.08)  # runtime overhead, compute buffers
        kv = self.kv_bytes_per_token() * num_ctx * max(1, parallel)
        return weights + kv + 512 * 1024 * 1024


@dataclass
class LoadedModel:
    name: str
    size_bytes: int
    context_length: int
    expires_at: str | None = None


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]

    def as_openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]
    id: str = ""


@dataclass
class ChatMessage:
    role: str  # system | user | assistant | tool
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_name: str | None = None

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            d["tool_calls"] = [
                {"function": {"name": t.name, "arguments": t.arguments}} for t in self.tool_calls
            ]
        if self.tool_name:
            d["tool_name"] = self.tool_name
        return d


@dataclass
class ChatResult:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    thinking: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_duration_s: float = 0.0
    load_duration_s: float = 0.0
    prompt_eval_s: float = 0.0
    eval_s: float = 0.0
    done_reason: str = ""

    @property
    def tokens_per_s(self) -> float:
        return self.completion_tokens / self.eval_s if self.eval_s > 0 else 0.0

    @property
    def prompt_tokens_per_s(self) -> float:
        return self.prompt_tokens / self.prompt_eval_s if self.prompt_eval_s > 0 else 0.0


class BackendError(RuntimeError):
    pass


class ModelBackend(abc.ABC):
    name: str = "abstract"

    @abc.abstractmethod
    async def health(self) -> bool: ...

    @abc.abstractmethod
    async def list_models(self) -> list[ModelDescriptor]: ...

    @abc.abstractmethod
    async def loaded(self) -> list[LoadedModel]: ...

    @abc.abstractmethod
    async def load(self, model: str, num_ctx: int, keep_alive: str | int = "30m") -> None: ...

    @abc.abstractmethod
    async def unload(self, model: str) -> None: ...

    @abc.abstractmethod
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
    ) -> ChatResult: ...

    async def close(self) -> None:
        return None
