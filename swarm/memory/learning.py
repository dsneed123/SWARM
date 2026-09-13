"""Workflow learning: which swarm compositions work for which objectives.

Every finished automatic task records a compact summary: objective class,
composition signature (capabilities, tiers, redundancy, models), outcome
(success, final confidence, duration). ``suggest`` returns the best-scoring
composition for a class, which the planner uses as a starting point, not a
rule. Records per class are capped and scores are moving averages so one
lucky run does not dominate.
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, Field

from swarm.core.db import Database
from swarm.workflow.dag import WorkflowSpec


class CompositionRecord(BaseModel):
    signature: str
    nodes: list[dict[str, Any]]  # capability, tier, redundancy, depends_on
    models: dict[str, int] = Field(default_factory=dict)  # model -> times used
    runs: int = 0
    successes: int = 0
    score: float = 0.0  # EMA of per-run score
    avg_duration_s: float = 0.0
    avg_confidence: float = 0.0
    last_task_id: str | None = None
    updated_at: float = Field(default_factory=time.time)


class ClassKnowledge(BaseModel):
    objective_class: str
    records: list[CompositionRecord] = Field(default_factory=list)


def signature_of(spec: WorkflowSpec) -> tuple[str, list[dict[str, Any]]]:
    nodes = []
    parts = []
    for n in spec.nodes:
        tier = n.tier.value if n.tier else "auto"
        nodes.append({"capability": n.capability, "tier": tier, "redundancy": n.redundancy, "depends_on": list(n.depends_on)})
        parts.append(f"{n.capability}x{n.redundancy}({tier})")
    return " -> ".join(parts), nodes


class WorkflowKnowledge:
    KIND = "workflow_knowledge"
    MAX_RECORDS = 30
    ALPHA = 0.3

    def __init__(self, db: Database) -> None:
        self.db = db

    def _load(self, objective_class: str) -> ClassKnowledge:
        doc = self.db.get_doc(self.KIND, objective_class)
        return ClassKnowledge.model_validate(doc) if doc else ClassKnowledge(objective_class=objective_class)

    def _save(self, ck: ClassKnowledge) -> None:
        ck.records.sort(key=lambda r: r.score, reverse=True)
        del ck.records[self.MAX_RECORDS :]
        self.db.put_doc(self.KIND, ck.objective_class, ck.model_dump(mode="json"))

    def record_outcome(
        self, *, objective_class: str, spec: WorkflowSpec, models: list[str], success: bool,
        confidence: float, duration_s: float, task_id: str, expected_duration_s: float = 600.0,
    ) -> CompositionRecord:
        sig, nodes = signature_of(spec)
        ck = self._load(objective_class or "mixed")
        rec = next((r for r in ck.records if r.signature == sig), None)
        if rec is None:
            rec = CompositionRecord(signature=sig, nodes=nodes)
            ck.records.append(rec)
        # Accuracy first, but unreasonable duration costs points.
        time_penalty = min(0.4, max(0.0, (duration_s - expected_duration_s) / (4 * expected_duration_s)))
        run_score = (confidence if success else 0.0) - time_penalty
        rec.runs += 1
        rec.successes += int(success)
        rec.score = run_score if rec.runs == 1 else (1 - self.ALPHA) * rec.score + self.ALPHA * run_score
        rec.avg_duration_s = duration_s if rec.runs == 1 else (1 - self.ALPHA) * rec.avg_duration_s + self.ALPHA * duration_s
        rec.avg_confidence = confidence if rec.runs == 1 else (1 - self.ALPHA) * rec.avg_confidence + self.ALPHA * confidence
        for m in models:
            rec.models[m] = rec.models.get(m, 0) + 1
        rec.last_task_id = task_id
        rec.updated_at = time.time()
        self._save(ck)
        return rec

    def suggest(self, objective_class: str) -> CompositionRecord | None:
        ck = self._load(objective_class or "mixed")
        good = [r for r in ck.records if r.successes > 0]
        if not good:
            return None
        # Prefer proven compositions: score weighted by a mild confidence in the sample size.
        return max(good, key=lambda r: r.score * (r.runs / (r.runs + 1)))

    def summary(self) -> list[dict[str, Any]]:
        out = []
        for cls, doc in self.db.list_docs(self.KIND).items():
            ck = ClassKnowledge.model_validate(doc)
            for r in ck.records[:5]:
                out.append({"class": cls, "signature": r.signature, "runs": r.runs, "successes": r.successes,
                            "score": round(r.score, 3), "avg_duration_s": round(r.avg_duration_s, 1),
                            "avg_confidence": round(r.avg_confidence, 3),
                            "models": sorted(r.models, key=r.models.get, reverse=True)[:4]})
        return out
