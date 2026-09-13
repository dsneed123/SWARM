"""Evidence-weighted consensus over redundant artifacts.

Redundant agents answer the same question. Instead of counting votes, each
artifact gets a weight from its confidence, the quality of its evidence and
the quality of its reasoning, discounted when two artifacts came from the
same model (less independent). Artifacts are grouped into positions by a
``Grouper`` (an LLM at runtime, a lexical heuristic in tests). The position
with the most weighted support wins, unless a minority position rests on
significantly stronger evidence, in which case the node is flagged for more
investigation rather than resolved by majority.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from swarm.artifacts.model import Artifact, Evidence, Provenance
from swarm.workflow.dag import ConsensusRule


class Grouper(Protocol):
    async def group(self, artifacts: list[Artifact]) -> list[list[int]]:
        """Partition artifact indices into positions (same conclusion)."""


_WORD = re.compile(r"[a-z0-9]+")
_NUM = re.compile(r"\d+(?:[.,]\d+)?")
_STOP = {"the", "a", "an", "is", "are", "of", "to", "and", "in", "on", "for", "that", "this", "it", "as", "with", "be", "by", "or", "was", "were", "at", "from"}


def _tokens(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP and len(w) > 2}


class HeuristicGrouper:
    """Lexical similarity on conclusions. Cheap, deterministic, used as fallback."""

    def __init__(self, threshold: float = 0.35) -> None:
        self.threshold = threshold

    async def group(self, artifacts: list[Artifact]) -> list[list[int]]:
        groups: list[list[int]] = []
        toks = [_tokens(a.conclusion) for a in artifacts]
        nums = [set(_NUM.findall(a.conclusion)) for a in artifacts]
        for i, t in enumerate(toks):
            for g in groups:
                ref = toks[g[0]]
                inter = len(t & ref)
                union = len(t | ref) or 1
                # Different numbers in otherwise similar conclusions are a disagreement.
                if nums[i] and nums[g[0]] and nums[i] != nums[g[0]]:
                    continue
                if inter / union >= self.threshold:
                    g.append(i)
                    break
            else:
                groups.append([i])
        return groups


def reasoning_quality(a: Artifact) -> float:
    q = 0.2
    if len(a.reasoning_summary) > 80:
        q += 0.3
    if a.contradictions or a.unresolved:
        q += 0.2  # acknowledges limits: a calibration signal
    if any(e.source_type in ("web", "file", "tool") for e in a.evidence):
        q += 0.3
    return min(1.0, q)


def artifact_weight(a: Artifact) -> float:
    return 0.35 * a.confidence + 0.45 * a.evidence_score() + 0.20 * reasoning_quality(a)


@dataclass
class Position:
    label: str
    members: list[Artifact]
    support: float = 0.0
    evidence_score: float = 0.0
    confidence: float = 0.0
    models: list[str] = field(default_factory=list)

    @property
    def best(self) -> Artifact:
        return max(self.members, key=artifact_weight)

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "artifacts": [m.id for m in self.members],
            "support": round(self.support, 3),
            "evidence_score": round(self.evidence_score, 3),
            "confidence": round(self.confidence, 3),
            "models": self.models,
        }


@dataclass
class ConsensusResult:
    status: str  # single | converged | weak | disagreement
    positions: list[Position]
    winner: Position
    merged: Artifact
    needs_more: bool
    reason: str
    minority_stronger: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "needs_more": self.needs_more,
            "minority_stronger": self.minority_stronger,
            "winner": self.winner.label,
            "positions": [p.as_dict() for p in self.positions],
            "merged": self.merged.id,
            "confidence": self.merged.confidence,
        }


async def evaluate_consensus(
    artifacts: list[Artifact],
    rule: ConsensusRule,
    grouper: Grouper | None = None,
    *,
    task_id: str,
    node_id: str,
    round_: int = 1,
) -> ConsensusResult:
    if not artifacts:
        raise ValueError("no artifacts to evaluate")
    grouper = grouper or HeuristicGrouper()
    if len(artifacts) == 1:
        groups = [[0]]
    else:
        groups = await grouper.group(artifacts)
    positions: list[Position] = []
    for idxs in groups:
        members = [artifacts[i] for i in idxs]
        seen_models: dict[str, int] = {}
        support = 0.0
        for m in members:
            model = m.model.model if m.model else "?"
            n = seen_models.get(model, 0)
            support += artifact_weight(m) * (0.6**n)  # same model again counts less
            seen_models[model] = n + 1
        pos = Position(
            label=members[0].conclusion[:120],
            members=members,
            support=support,
            evidence_score=max(m.evidence_score() for m in members),
            confidence=sum(m.confidence for m in members) / len(members),
            models=list(seen_models),
        )
        positions.append(pos)
    positions.sort(key=lambda p: p.support, reverse=True)
    winner = positions[0]
    total = sum(p.support for p in positions) or 1.0
    share = winner.support / total
    minority_stronger = any(
        p is not winner and p.evidence_score >= winner.evidence_score + 0.15 and p.evidence_score >= 0.5
        for p in positions
    )
    if len(artifacts) == 1:
        status = "single"
        needs_more = winner.confidence < rule.min_confidence
        reason = "single artifact" + (" with low confidence" if needs_more else "")
    elif len(positions) == 1:
        status = "converged" if winner.confidence >= rule.min_confidence else "weak"
        needs_more = status == "weak"
        reason = f"{len(artifacts)} artifacts agree" + ("" if status == "converged" else " but confidence is low")
    elif minority_stronger:
        status = "disagreement"
        needs_more = True
        reason = "a minority position has significantly stronger evidence"
    elif share < 0.6:
        status = "disagreement"
        needs_more = True
        reason = f"no dominant position (top share {share:.0%})"
    else:
        status = "converged"
        needs_more = winner.confidence < rule.min_confidence
        reason = f"dominant position with {share:.0%} of weighted support"
    merged = _merge(artifacts, positions, winner, share, status, task_id, node_id, round_)
    return ConsensusResult(status=status, positions=positions, winner=winner, merged=merged,
                           needs_more=needs_more, reason=reason, minority_stronger=minority_stronger)


def _merge(
    artifacts: list[Artifact], positions: list[Position], winner: Position, share: float,
    status: str, task_id: str, node_id: str, round_: int,
) -> Artifact:
    best = winner.best
    independent = len(set(winner.models))
    if status in ("converged",) and len(artifacts) > 1:
        conf = min(0.98, winner.confidence * (0.85 + 0.05 * independent) * (0.7 + 0.3 * share))
    elif status == "single":
        conf = best.confidence
    else:
        conf = min(best.confidence, 0.5) * share
    evidence: list[Evidence] = []
    seen: set[tuple[str, str | None]] = set()
    for m in winner.members:
        for e in sorted(m.evidence, key=lambda e: e.quality, reverse=True):
            key = (e.claim[:80].lower(), e.source)
            if key not in seen:
                seen.add(key)
                evidence.append(e)
    contradictions = list(dict.fromkeys(c for m in winner.members for c in m.contradictions))
    for p in positions[1:]:
        contradictions.append(f"alternative position ({len(p.members)} agent(s), evidence {p.evidence_score:.2f}): {p.best.conclusion[:300]}")
        for e in p.best.evidence[:3]:
            if e.source:
                evidence.append(Evidence(claim=e.claim, source=e.source, source_type=e.source_type,
                                         quote=e.quote, quality=e.quality, supports=False, retrieved_at=e.retrieved_at))
    unresolved = list(dict.fromkeys(u for m in artifacts for u in m.unresolved))
    content = best.content
    if not content:
        for m in winner.members:
            if m.content:
                content = m.content
                break
    return Artifact(
        kind="consensus",
        title=f"consensus r{round_}: {best.title}"[:80],
        conclusion=best.conclusion,
        confidence=round(conf, 3),
        evidence=evidence[:40],
        reasoning_summary=(f"{status}: {len(artifacts)} artifacts, {len(positions)} position(s), "
                           f"winner share {share:.0%}, models {', '.join(winner.models)}. " + best.reasoning_summary)[:3000],
        contradictions=contradictions[:20],
        unresolved=unresolved[:20],
        next_action=best.next_action,
        content=content,
        model=best.model,
        execution_time_s=max(a.execution_time_s for a in artifacts),
        tools=[t for a in winner.members for t in a.tools][:30],
        provenance=Provenance(task_id=task_id, node_id=node_id, capability="consensus",
                              attempt=round_, inputs=[a.id for a in artifacts]),
        parents=[a.id for a in artifacts],
        tags=["consensus", status],
    )
