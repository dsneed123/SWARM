"""Failure diagnosis and recovery strategy selection.

A failed agent carries an error kind (model, tool, format, timeout, resource,
backend, context, cancelled). Combined with the attempt number and what has
already been tried, the diagnosis picks a concrete change: another model,
fewer tools, a smaller context, a deeper tier, waiting for resources, or
giving up. Every decision is recorded on the node so the reasoning is
inspectable and so the learning system can bias future routing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from swarm.core.types import Tier


@dataclass
class Diagnosis:
    cause: str  # model | tool | context | resource | decomposition | external | unknown
    action: str  # retry | switch_model | escalate_tier | reduce_context | drop_tools | wait | split | give_up | skip
    note: str
    changes: dict[str, Any] = field(default_factory=dict)  # applied to the next AgentSpec
    wait_s: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {"cause": self.cause, "action": self.action, "note": self.note, "changes": self.changes, "wait_s": self.wait_s}


@dataclass
class FailureContext:
    kind: str
    message: str
    attempt: int
    max_attempts: int
    critical: bool
    model: str | None
    tier: Tier
    tools_used: int = 0
    tools_failed: int = 0
    context_chars: int = 0
    tried_models: set[str] = field(default_factory=set)
    alternatives: int = 1  # other models available in the tier
    pinned_model: bool = False


def diagnose(f: FailureContext) -> Diagnosis:
    last = f.attempt >= f.max_attempts
    msg = f.message.lower()

    def give_up(cause: str, note: str) -> Diagnosis:
        return Diagnosis(cause, "skip" if not f.critical else "give_up", note)

    if f.kind == "cancelled":
        return Diagnosis("external", "give_up", "cancelled by user")

    if f.kind == "resource" or "out of memory" in msg or "cuda" in msg:
        if f.attempt == 1:
            return Diagnosis("resource", "wait", "resource pressure; wait for memory then retry", wait_s=10.0)
        if f.tier != Tier.FAST:
            return Diagnosis("resource", "switch_model", "still failing on resources; use a smaller tier",
                             changes={"tier": f.tier.down(), "model": None})
        return give_up("resource", "no smaller model to fall back to")

    if f.kind == "backend":
        if f.attempt == 1:
            return Diagnosis("external", "wait", "backend error; wait and retry once", wait_s=5.0)
        return give_up("external", f"backend keeps failing: {f.message[:120]}")

    if f.kind == "timeout":
        if f.context_chars > 8000 and f.attempt == 1:
            return Diagnosis("context", "reduce_context", "timed out with a large context; halve it",
                             changes={"context_scale": 0.5})
        if not f.pinned_model and f.alternatives > 0 and f.model:
            return Diagnosis("model", "switch_model", "timed out; try another model in the tier",
                             changes={"exclude": {f.model}})
        if last:
            return give_up("model", "timed out repeatedly")
        return Diagnosis("model", "retry", "timed out; retry with a longer budget", changes={"time_scale": 1.5})

    if f.kind == "format":
        if not f.pinned_model and f.alternatives > 0 and f.model:
            return Diagnosis("model", "switch_model", "model could not produce structured output",
                             changes={"exclude": {f.model}})
        if f.tier != Tier.DEEP:
            return Diagnosis("model", "escalate_tier", "format failures; use a more capable tier",
                             changes={"tier": f.tier.up(), "model": None})
        return give_up("model", "no model produced structured output")

    if f.kind == "tool" or (f.tools_used and f.tools_failed >= max(2, f.tools_used // 2)):
        if f.attempt == 1:
            return Diagnosis("tool", "drop_tools", "tool calls kept failing; retry without tools and rely on context",
                             changes={"tools": []})
        return give_up("tool", "task depends on tools that are failing")

    if f.kind == "context":
        if f.attempt == 1:
            return Diagnosis("context", "reduce_context", "output suggests context confusion; tighten context",
                             changes={"context_scale": 0.6})
        return Diagnosis("decomposition", "split", "still confused; the task is probably too broad")

    # generic model failure
    if not f.pinned_model and f.alternatives > 0 and f.model and f.model not in f.tried_models - {f.model}:
        return Diagnosis("model", "switch_model", f"model failure ({f.message[:80]}); try another model",
                         changes={"exclude": {f.model}})
    if f.tier != Tier.DEEP and not f.pinned_model:
        return Diagnosis("model", "escalate_tier", "repeated failure; escalate tier",
                         changes={"tier": f.tier.up(), "model": None})
    if last:
        return give_up("unknown", f"exhausted attempts: {f.message[:120]}")
    return Diagnosis("unknown", "retry", "retry once more", changes={})
