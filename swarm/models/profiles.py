"""Model profiles: static metadata plus what we have actually observed.

A profile starts from the backend's descriptor (parameter count, context
window, capabilities) and accumulates measurements from real runs on this
machine: resident memory, generation throughput, prompt throughput, load
time, latency, and per-capability reliability. Updates are exponential
moving averages so a single odd run cannot swing routing decisions.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from swarm.core.db import Database
from swarm.core.types import Tier
from swarm.models.backend import ModelDescriptor

ALPHA = 0.3  # EMA weight for new observations


def _ema(old: float | None, new: float) -> float:
    return new if old is None or old == 0 else (1 - ALPHA) * old + ALPHA * new


class CapabilityStats(BaseModel):
    calls: int = 0
    successes: int = 0
    failures: int = 0
    avg_time_s: float = 0.0
    avg_confidence: float = 0.0
    avg_evidence: float = 0.0
    json_failures: int = 0
    tool_failures: int = 0

    @property
    def success_rate(self) -> float:
        # Optimistic prior so unproven models are not starved of work.
        return (self.successes + 2) / (self.calls + 3)


class ModelProfile(BaseModel):
    name: str
    backend: str
    family: str = ""
    parameters_b: float = 0.0
    quantization: str = ""
    size_bytes: int = 0
    context_length: int = 0
    capabilities: list[str] = Field(default_factory=list)
    specialties: list[str] = Field(default_factory=list)
    architecture: dict[str, Any] = Field(default_factory=dict)
    tier: Tier = Tier.STANDARD
    pinned_tier: bool = False
    disabled: bool = False

    # observed on this machine
    observed_memory: int | None = None  # resident bytes when loaded (backend reported)
    observed_ctx: int | None = None  # num_ctx in effect for the observation
    tokens_per_s: float | None = None
    prompt_tokens_per_s: float | None = None
    load_time_s: float | None = None
    avg_latency_s: float | None = None
    calls: int = 0
    failures: int = 0
    per_capability: dict[str, CapabilityStats] = Field(default_factory=dict)
    last_used: float | None = None

    @classmethod
    def from_descriptor(cls, d: ModelDescriptor) -> ModelProfile:
        return cls(
            name=d.name,
            backend=d.backend,
            family=d.family,
            parameters_b=d.parameters_b,
            quantization=d.quantization,
            size_bytes=d.size_bytes,
            context_length=d.context_length,
            capabilities=sorted(d.capabilities),
            specialties=sorted(d.specialties),
            architecture=d.architecture,
        )

    def descriptor(self) -> ModelDescriptor:
        return ModelDescriptor(
            name=self.name,
            backend=self.backend,
            family=self.family,
            parameters_b=self.parameters_b,
            quantization=self.quantization,
            size_bytes=self.size_bytes,
            context_length=self.context_length,
            capabilities=set(self.capabilities),
            architecture=self.architecture,
            specialties=set(self.specialties),
        )

    @property
    def is_chat_model(self) -> bool:
        return "completion" in self.capabilities or "embedding" not in self.capabilities

    def supports_tools(self) -> bool:
        return "tools" in self.capabilities

    def supports_thinking(self) -> bool:
        return "thinking" in self.capabilities

    def estimate_memory(self, num_ctx: int, parallel: int = 1) -> int:
        """Best estimate for resident memory at ``num_ctx``, preferring observations."""
        est = self.descriptor().estimate_memory(num_ctx, parallel)
        if self.observed_memory and self.observed_ctx:
            # Scale the observed KV portion to the requested context.
            kv_per_token = self.descriptor().kv_bytes_per_token() * max(1, parallel)
            weights_observed = self.observed_memory - kv_per_token * self.observed_ctx
            scaled = weights_observed + kv_per_token * num_ctx
            # Blend: trust observation but keep a bit of the model-based estimate.
            return int(0.8 * scaled + 0.2 * est)
        return est

    def expected_seconds(self, prompt_tokens: int, completion_tokens: int) -> float:
        tps = self.tokens_per_s or (400.0 / max(self.parameters_b, 1.0))  # crude prior
        ptps = self.prompt_tokens_per_s or tps * 8
        return prompt_tokens / max(ptps, 1) + completion_tokens / max(tps, 1)

    def stats(self, capability: str) -> CapabilityStats:
        return self.per_capability.setdefault(capability, CapabilityStats())

    def record_call(
        self,
        capability: str,
        *,
        ok: bool,
        duration_s: float,
        tokens_per_s: float = 0.0,
        prompt_tokens_per_s: float = 0.0,
        load_time_s: float = 0.0,
        confidence: float | None = None,
        evidence: float | None = None,
        json_failed: bool = False,
        tool_failed: bool = False,
        ts: float = 0.0,
    ) -> None:
        self.calls += 1
        self.last_used = ts or self.last_used
        if not ok:
            self.failures += 1
        if tokens_per_s > 0:
            self.tokens_per_s = _ema(self.tokens_per_s, tokens_per_s)
        if prompt_tokens_per_s > 0:
            self.prompt_tokens_per_s = _ema(self.prompt_tokens_per_s, prompt_tokens_per_s)
        if load_time_s > 0.5:
            self.load_time_s = _ema(self.load_time_s, load_time_s)
        self.avg_latency_s = _ema(self.avg_latency_s, duration_s)
        s = self.stats(capability)
        s.calls += 1
        if ok:
            s.successes += 1
        else:
            s.failures += 1
        s.avg_time_s = _ema(s.avg_time_s, duration_s)
        if confidence is not None:
            s.avg_confidence = _ema(s.avg_confidence, confidence)
        if evidence is not None:
            s.avg_evidence = _ema(s.avg_evidence, evidence)
        if json_failed:
            s.json_failures += 1
        if tool_failed:
            s.tool_failures += 1

    def observe_memory(self, resident: int, num_ctx: int) -> None:
        if resident <= 0:
            return
        if self.observed_memory is None or self.observed_ctx != num_ctx:
            self.observed_memory, self.observed_ctx = resident, num_ctx
        else:
            self.observed_memory = int(_ema(self.observed_memory, resident))

    def reliability(self, capability: str | None = None) -> float:
        if capability and capability in self.per_capability:
            return self.per_capability[capability].success_rate
        return (self.calls - self.failures + 2) / (self.calls + 3)


def assign_tiers(profiles: list[ModelProfile], pins: dict[Tier, str | None]) -> None:
    """Assign FAST/STANDARD/DEEP by parameter count, guaranteeing every tier
    has at least one usable model when any chat model exists."""
    chat = [p for p in profiles if p.is_chat_model and not p.disabled]
    for p in chat:
        if p.pinned_tier:
            continue
        if p.parameters_b <= 9:
            p.tier = Tier.FAST
        elif p.parameters_b <= 40:
            p.tier = Tier.STANDARD
        else:
            p.tier = Tier.DEEP
    for tier, name in pins.items():
        if name:
            for p in chat:
                if p.name == name:
                    p.tier, p.pinned_tier = tier, True
    if not chat:
        return
    by_size = sorted(chat, key=lambda p: p.parameters_b)
    pinned = {p.name for p in chat if p.pinned_tier}
    present = {p.tier for p in chat}
    # Largest model doubles as DEEP and smallest as FAST when those tiers are
    # empty. STANDARD stays empty with fewer than three models; the router
    # then borrows from neighbouring tiers.
    if Tier.DEEP not in present and by_size[-1].name not in pinned and len(by_size) > 1:
        by_size[-1].tier = Tier.DEEP
    if Tier.FAST not in present and by_size[0].name not in pinned:
        by_size[0].tier = Tier.FAST
    if Tier.STANDARD not in {p.tier for p in chat} and len(by_size) >= 3:
        mid = by_size[len(by_size) // 2]
        if mid.name not in pinned and mid.tier != Tier.FAST or mid is not by_size[0] and mid is not by_size[-1]:
            mid.tier = Tier.STANDARD


class ProfileStore:
    KIND = "model_profile"

    def __init__(self, db: Database) -> None:
        self.db = db
        self._cache: dict[str, ModelProfile] = {
            k: ModelProfile.model_validate(v) for k, v in db.list_docs(self.KIND).items()
        }

    def all(self) -> list[ModelProfile]:
        return list(self._cache.values())

    def get(self, name: str) -> ModelProfile | None:
        return self._cache.get(name)

    def save(self, profile: ModelProfile) -> None:
        self._cache[profile.name] = profile
        self.db.put_doc(self.KIND, profile.name, profile.model_dump(mode="json"))

    def sync(self, descriptors: list[ModelDescriptor], pins: dict[Tier, str | None]) -> list[ModelProfile]:
        """Merge freshly discovered models with stored observations."""
        seen = set()
        for d in descriptors:
            seen.add(d.name)
            existing = self._cache.get(d.name)
            if existing is None:
                self._cache[d.name] = ModelProfile.from_descriptor(d)
            else:
                fresh = ModelProfile.from_descriptor(d)
                for f in ("family", "parameters_b", "quantization", "size_bytes", "context_length",
                          "capabilities", "specialties", "architecture"):
                    setattr(existing, f, getattr(fresh, f))
                existing.disabled = False
        for name, p in self._cache.items():
            if name not in seen and p.backend == (descriptors[0].backend if descriptors else p.backend):
                p.disabled = True  # not installed any more; keep observations
        assign_tiers(self.all(), pins)
        for p in self._cache.values():
            self.save(p)
        return self.all()
