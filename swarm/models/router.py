"""Chooses which model should serve a request.

The router only ranks; the scheduler decides what is actually loadable right
now. Ranking weighs (in order of importance) accuracy expectations, observed
reliability for the capability, specialty fit, whether the model is already
resident, memory fit, and expected execution time against the time budget.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from swarm.core.types import Tier
from swarm.models.profiles import ModelProfile, ProfileStore

REASONING_CAPABILITIES = {"reasoning", "criticism", "verification", "planning", "analysis"}


@dataclass
class ModelRequest:
    capability: str = "general"
    tier: Tier = Tier.STANDARD
    model: str | None = None  # exact pin
    needs_tools: bool = False
    needs_json: bool = False
    exclude: set[str] = field(default_factory=set)  # prefer independence from these
    min_context: int = 8192
    time_budget_s: float | None = None
    expected_prompt_tokens: int = 3000
    expected_completion_tokens: int = 800
    purpose: str = ""  # free text for the event log


@dataclass
class Candidate:
    profile: ModelProfile
    score: float
    reasons: list[str] = field(default_factory=list)
    loaded: bool = False
    fits: bool = True


class ModelRouter:
    def __init__(self, profiles: ProfileStore) -> None:
        self.profiles = profiles

    def usable(self) -> list[ModelProfile]:
        return [p for p in self.profiles.all() if p.is_chat_model and not p.disabled]

    def by_tier(self, tier: Tier) -> list[ModelProfile]:
        return [p for p in self.usable() if p.tier == tier]

    def candidates(
        self,
        req: ModelRequest,
        *,
        loaded: set[str],
        headroom: int,
        num_ctx: int,
        parallel: int = 1,
    ) -> list[Candidate]:
        pool = self.usable()
        if req.model:
            pool = [p for p in pool if p.name == req.model]
            if not pool:
                return []
        else:
            tier_pool = [p for p in pool if p.tier == req.tier]
            if not tier_pool:  # nearest tiers, higher first (accuracy first)
                order = [req.tier.up(), req.tier.down()]
                for t in order:
                    tier_pool = [p for p in pool if p.tier == t]
                    if tier_pool:
                        break
            pool = tier_pool
        if req.needs_tools:
            with_tools = [p for p in pool if p.supports_tools()]
            pool = with_tools or pool
        out = [self._score(p, req, loaded, headroom, num_ctx, parallel) for p in pool]
        out.sort(key=lambda c: c.score, reverse=True)
        return out

    def _score(
        self,
        p: ModelProfile,
        req: ModelRequest,
        loaded: set[str],
        headroom: int,
        num_ctx: int,
        parallel: int,
    ) -> Candidate:
        reasons: list[str] = []
        # Accuracy prior from size (diminishing returns).
        quality = math.log2(max(p.parameters_b, 0.5) + 1) / math.log2(81)
        score = 1.0 * quality
        rel = p.reliability(req.capability)
        score += 2.0 * (rel - 0.5)
        if p.calls:
            reasons.append(f"reliability {rel:.2f}")
        stats = p.per_capability.get(req.capability)
        if stats and stats.calls:
            score += 0.3 * stats.avg_confidence + 0.3 * stats.avg_evidence
        if req.capability in p.specialties:
            score += 0.5
            reasons.append("specialty match")
        elif "coding" in p.specialties and req.capability not in ("coding", "tool_use", "data"):
            # A code-tuned model should not win general work just because it is resident.
            score -= 0.5
            reasons.append("code-specialised model for non-code work")
        if req.capability in REASONING_CAPABILITIES and p.supports_thinking():
            score += 0.15
        if req.needs_tools and not p.supports_tools():
            score -= 1.0
            reasons.append("no tool support")
        is_loaded = p.name in loaded
        if is_loaded:
            score += 0.3
            reasons.append("resident")
        est = p.estimate_memory(num_ctx, parallel)
        fits = is_loaded or est <= headroom
        if not fits:
            score -= 0.4
            reasons.append("needs eviction to load")
        if p.name in req.exclude:
            score -= 0.8
            reasons.append("already used for this question")
        if req.time_budget_s:
            expected = p.expected_seconds(req.expected_prompt_tokens, req.expected_completion_tokens)
            if not is_loaded:
                expected += p.load_time_s or (p.size_bytes / (2 * 1024**3))  # ~2 GB/s prior
            if expected > req.time_budget_s:
                over = expected / req.time_budget_s
                score -= min(1.5, 0.5 * over)
                reasons.append(f"expected {expected:.0f}s > budget {req.time_budget_s:.0f}s")
        if p.context_length and num_ctx > p.context_length:
            score -= 0.6
            reasons.append("context too small")
        return Candidate(profile=p, score=score, reasons=reasons, loaded=is_loaded, fits=fits)
