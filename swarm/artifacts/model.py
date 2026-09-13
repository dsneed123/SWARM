"""Artifacts: structured, immutable, content-addressed outputs.

An agent never hands another agent a transcript. It produces an ``Artifact``
and the orchestrator decides what (compact) view of it downstream agents see.
The ``id`` is the sha256 of the artifact's content fields so identical results
dedupe and any reference can be verified. Versioning is expressed through
``parents`` (a revised artifact points at the one it supersedes).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from swarm.core.types import now_iso

ArtifactKind = Literal[
    "result",  # a normal agent output
    "consensus",  # merged view produced by evidence-weighted consensus
    "digest",  # compressed summary of several artifacts
    "plan",  # orchestrator plan for a task
    "final",  # final answer for a task
    "failure",  # structured failure record
    "input",  # user provided input / files
]


class Evidence(BaseModel):
    claim: str
    source: str | None = None  # URL, file path, tool name, or "model knowledge"
    source_type: Literal["web", "file", "tool", "model", "user", "artifact"] = "model"
    quote: str | None = None
    retrieved_at: str | None = None
    quality: float = Field(0.5, ge=0.0, le=1.0)  # agent's own assessment of the evidence
    supports: bool = True  # False when it contradicts the conclusion


class ToolUse(BaseModel):
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    ok: bool = True
    duration_s: float = 0.0
    summary: str = ""


class ModelInfo(BaseModel):
    backend: str = "ollama"
    model: str
    tier: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    tokens_per_s: float = 0.0


class Provenance(BaseModel):
    task_id: str
    node_id: str
    agent_id: str | None = None
    capability: str | None = None
    attempt: int = 1
    inputs: list[str] = Field(default_factory=list)  # artifact ids consumed
    created_at: str = Field(default_factory=now_iso)


class Artifact(BaseModel):
    id: str = ""
    kind: ArtifactKind = "result"
    title: str = ""
    conclusion: str = ""
    confidence: float = Field(0.5, ge=0.0, le=1.0)
    evidence: list[Evidence] = Field(default_factory=list)
    reasoning_summary: str = ""
    contradictions: list[str] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    next_action: str = ""
    content: str = ""  # long-form body when the capability produces one (writing, code)
    data: dict[str, Any] = Field(default_factory=dict)  # structured extras
    model: ModelInfo | None = None
    execution_time_s: float = 0.0
    tools: list[ToolUse] = Field(default_factory=list)
    provenance: Provenance
    parents: list[str] = Field(default_factory=list)  # artifact ids this supersedes/merges
    version: int = 1
    tags: list[str] = Field(default_factory=list)

    _CONTENT_FIELDS = (
        "kind",
        "title",
        "conclusion",
        "confidence",
        "evidence",
        "reasoning_summary",
        "contradictions",
        "unresolved",
        "next_action",
        "content",
        "data",
        "model",
        "tools",
        "provenance",
        "parents",
        "version",
        "tags",
    )

    @model_validator(mode="after")
    def _assign_id(self) -> Artifact:
        computed = self.compute_id()
        if self.id and self.id != computed:
            raise ValueError("artifact id does not match content hash")
        object.__setattr__(self, "id", computed)
        return self

    def compute_id(self) -> str:
        payload = self.model_dump(mode="json", include=set(self._CONTENT_FIELDS))
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return "art_" + hashlib.sha256(blob.encode()).hexdigest()[:16]

    def revise(self, **changes: Any) -> Artifact:
        """Return a new artifact that supersedes this one."""
        data = self.model_dump()
        data.pop("id")
        data.update(changes)
        data["parents"] = [self.id]
        data["version"] = self.version + 1
        data["provenance"]["created_at"] = now_iso()
        return Artifact.model_validate(data)

    # --- views -----------------------------------------------------------

    def compact(self, max_evidence: int = 5, max_chars: int = 1200) -> dict[str, Any]:
        """Information-dense view for downstream agents.

        Keeps conclusion, confidence, best evidence with sources, contradictions,
        unresolved questions and provenance. Drops long prose.
        """
        ev = sorted(self.evidence, key=lambda e: e.quality, reverse=True)[:max_evidence]
        view: dict[str, Any] = {
            "artifact": self.id,
            "from": self.provenance.capability or self.provenance.node_id,
            "conclusion": _trim(self.conclusion, max_chars),
            "confidence": round(self.confidence, 2),
        }
        if ev:
            view["evidence"] = [
                {
                    k: v
                    for k, v in {
                        "claim": _trim(e.claim, 300),
                        "source": e.source,
                        "quality": round(e.quality, 2),
                        "contradicts": (not e.supports) or None,
                    }.items()
                    if v is not None
                }
                for e in ev
            ]
        if self.contradictions:
            view["contradictions"] = [_trim(c, 300) for c in self.contradictions[:5]]
        if self.unresolved:
            view["unresolved"] = [_trim(u, 300) for u in self.unresolved[:5]]
        if self.next_action:
            view["next_action"] = _trim(self.next_action, 300)
        return view

    def sources(self) -> list[Evidence]:
        return [e for e in self.evidence if e.source and e.source_type in ("web", "file", "tool")]

    def evidence_score(self) -> float:
        """Rough quality of the supporting evidence base in [0, 1]."""
        supporting = [e for e in self.evidence if e.supports]
        if not supporting:
            return 0.15
        external = [e for e in supporting if e.source_type in ("web", "file", "tool")]
        base = sum(e.quality for e in supporting) / len(supporting)
        breadth = min(len(supporting), 5) / 5
        externality = 0.6 + 0.4 * (len(external) / len(supporting))
        return max(0.0, min(1.0, 0.5 * base + 0.3 * breadth + 0.2 * externality))


def _trim(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"
