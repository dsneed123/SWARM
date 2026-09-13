"""Small model-backed helpers used by the orchestrator itself.

These run on the orchestrator's own (FAST) model through the scheduler:
grouping redundant conclusions into positions, digesting artifacts, and
judging whether a question needs the user. Each has a non-model fallback so
the control plane never blocks on inference.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from swarm.agents.runtime import parse_json_object
from swarm.artifacts.model import Artifact
from swarm.core.types import Tier
from swarm.models.backend import ChatMessage
from swarm.models.router import ModelRequest
from swarm.models.scheduler import ModelScheduler
from swarm.orchestrator.consensus import HeuristicGrouper

log = logging.getLogger(__name__)


class Assistant:
    def __init__(self, scheduler: ModelScheduler, tier: Tier = Tier.FAST, model: str | None = None) -> None:
        self.scheduler = scheduler
        self.tier = tier
        self.model = model
        self.calls = 0
        self.failures = 0

    async def ask_json(self, system: str, user: str, schema: dict[str, Any], *, tier: Tier | None = None,
                       purpose: str = "orchestrator", max_tokens: int = 1500, time_budget_s: float = 120.0) -> dict[str, Any] | None:
        req = ModelRequest(capability="planning", tier=tier or self.tier, model=self.model if tier is None else None,
                           needs_json=True, purpose=purpose, time_budget_s=time_budget_s,
                           expected_completion_tokens=max_tokens)
        self.calls += 1
        try:
            async with await self.scheduler.acquire(req, timeout_s=time_budget_s) as lease:
                think = False if lease.profile.supports_thinking() else None
                r = await lease.chat([ChatMessage("system", system), ChatMessage("user", user)],
                                     json_schema=schema, temperature=0.1, max_tokens=max_tokens, think=think)
            return parse_json_object(r.content)
        except Exception as e:  # noqa: BLE001
            self.failures += 1
            log.warning("assistant call failed (%s): %s", purpose, e)
            return None


class LLMGrouper:
    """Groups artifacts by whether their conclusions agree in substance."""

    SCHEMA = {
        "type": "object",
        "properties": {
            "groups": {"type": "array", "items": {"type": "array", "items": {"type": "integer"}}},
        },
        "required": ["groups"],
    }

    def __init__(self, assistant: Assistant) -> None:
        self.assistant = assistant
        self.fallback = HeuristicGrouper()

    async def group(self, artifacts: list[Artifact]) -> list[list[int]]:
        if len(artifacts) < 2:
            return [list(range(len(artifacts)))]
        listing = "\n".join(f"[{i}] {a.conclusion[:700]}" for i, a in enumerate(artifacts))
        data = await self.assistant.ask_json(
            "You compare conclusions from independent agents answering the same question. Group the indices of "
            "conclusions that state the same substantive answer (same facts, numbers, recommendation). Different "
            "wording is the same group; different facts or contradicting numbers are different groups. "
            "Every index must appear exactly once.",
            f"CONCLUSIONS:\n{listing}\n\nReturn {{\"groups\": [[...], ...]}}.",
            self.SCHEMA, purpose="consensus-grouping", max_tokens=300,
        )
        groups = _valid_groups(data, len(artifacts))
        if groups is None:
            return await self.fallback.group(artifacts)
        return groups


def _valid_groups(data: dict[str, Any] | None, n: int) -> list[list[int]] | None:
    if not data or not isinstance(data.get("groups"), list):
        return None
    seen: list[int] = []
    groups: list[list[int]] = []
    for g in data["groups"]:
        if not isinstance(g, list):
            return None
        clean = [int(i) for i in g if isinstance(i, int | float) and 0 <= int(i) < n]
        if not clean:
            continue
        groups.append(clean)
        seen.extend(clean)
    if sorted(seen) != list(range(n)):
        return None
    return groups


DIGEST_SCHEMA = {
    "type": "object",
    "properties": {
        "conclusion": {"type": "string"},
        "confidence": {"type": "number"},
        "evidence": {"type": "array", "items": {"type": "object", "properties": {
            "claim": {"type": "string"}, "source": {"type": "string"}, "source_type": {"type": "string"},
            "quality": {"type": "number"}}, "required": ["claim"]}},
        "contradictions": {"type": "array", "items": {"type": "string"}},
        "unresolved": {"type": "array", "items": {"type": "string"}},
        "reasoning_summary": {"type": "string"},
    },
    "required": ["conclusion", "confidence", "evidence"],
}


def make_digester(assistant: Assistant):
    async def digest(artifacts: list[Artifact], objective: str) -> dict[str, Any] | None:
        views = [a.compact(max_evidence=6, max_chars=900) for a in artifacts]
        data = await assistant.ask_json(
            "You compress artifacts produced by agents into one dense digest for downstream agents. Preserve every "
            "conclusion, decision, number, contradiction, unresolved question and every source URL/path with its "
            "claim. Remove prose, repetition and hedging. Never invent content.",
            f"OBJECTIVE: {objective}\n\nARTIFACTS:\n{json.dumps(views, ensure_ascii=False)}",
            DIGEST_SCHEMA, purpose="context-digest", max_tokens=1200,
        )
        if data:
            for e in data.get("evidence") or []:
                if isinstance(e, dict):
                    e.setdefault("source_type", "artifact")
                    if e.get("source_type") not in ("web", "file", "tool", "model", "user", "artifact"):
                        e["source_type"] = "artifact"
        return data

    return digest


CLARIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "needs_clarification": {"type": "boolean"},
        "question": {"type": "string"},
        "options": {"type": "array", "items": {"type": "string"}},
        "assumption": {"type": "string"},
    },
    "required": ["needs_clarification"],
}
