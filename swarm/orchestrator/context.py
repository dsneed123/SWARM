"""Context selection and compression for agents.

The orchestrator decides what each agent sees. Inputs are the result
artifacts of the node's dependencies (or an explicit ``context_from`` list),
rendered as compact views. When those exceed the agent's token budget the
oldest/largest views are compressed: first by trimming evidence, then by
producing a digest artifact that preserves conclusions, evidence, decisions,
contradictions, unresolved questions and provenance while dropping prose.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from swarm.artifacts.model import Artifact, Evidence, Provenance
from swarm.artifacts.store import ArtifactStore

Digester = Callable[[list[Artifact], str], Awaitable[dict[str, Any] | None]]


def approx_tokens(obj: Any) -> int:
    text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    return len(text) // 4 + 1


def heuristic_digest(artifacts: list[Artifact]) -> dict[str, Any]:
    """Lossy but structure-preserving merge without a model call."""
    per = max(120, 2400 // max(1, len(artifacts)))
    conclusions = [f"[{a.provenance.capability or a.provenance.node_id}] {a.conclusion[:per]}" for a in artifacts]
    evidence: list[Evidence] = []
    seen: set[str] = set()
    for a in artifacts:
        for e in sorted(a.evidence, key=lambda e: e.quality, reverse=True)[:3]:
            if e.source and e.source in seen:
                continue
            seen.add(e.source or e.claim)
            evidence.append(e)
    return {
        "conclusion": "\n".join(conclusions)[:4000],
        "confidence": min(a.confidence for a in artifacts),
        "evidence": [e.model_dump() for e in evidence[:12]],
        "contradictions": list(dict.fromkeys(c for a in artifacts for c in a.contradictions))[:10],
        "unresolved": list(dict.fromkeys(u for a in artifacts for u in a.unresolved))[:10],
        "reasoning_summary": "heuristic digest of " + ", ".join(a.id for a in artifacts),
    }


class ContextBuilder:
    def __init__(self, store: ArtifactStore, digester: Digester | None = None) -> None:
        self.store = store
        self.digester = digester  # model-backed; falls back to the heuristic when None or failing

    async def build(
        self,
        artifact_ids: list[str],
        *,
        token_budget: int,
        task_id: str,
        node_id: str,
        objective: str = "",
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Return (compact views, artifact ids actually used)."""
        artifacts = self.store.get_many(artifact_ids)
        if not artifacts:
            return [], []
        views = [a.compact() for a in artifacts]
        if approx_tokens(views) <= token_budget:
            return views, [a.id for a in artifacts]
        # Step 1: shrink evidence per view.
        views = [a.compact(max_evidence=2, max_chars=600) for a in artifacts]
        if approx_tokens(views) <= token_budget:
            return views, [a.id for a in artifacts]
        # Step 2: digest the oldest artifacts into one until it fits, keeping the newest intact.
        # Each pass folds the previous digest plus the oldest half of what remains into a new digest.
        keep = list(artifacts)
        digested: list[Artifact] = []

        def size() -> int:
            return approx_tokens([d.compact(4, 900) for d in digested] + [a.compact(2, 600) for a in keep])

        while len(keep) > 1 and size() > token_budget:
            half = max(1, len(keep) // 2)
            batch = digested + keep[:half]
            keep = keep[half:]
            digested = [await self._digest(batch, task_id, node_id, objective)]
        result_arts = digested + keep
        views = [a.compact(4 if a.kind == "digest" else 2, 900 if a.kind == "digest" else 600) for a in result_arts]
        # Last resort: hard trim.
        while approx_tokens(views) > token_budget and len(views) > 1:
            views.pop(1 if len(views) > 1 else 0)
        return views, [a.id for a in result_arts]

    async def _digest(self, batch: list[Artifact], task_id: str, node_id: str, objective: str) -> Artifact:
        data = None
        if self.digester is not None:
            try:
                data = await self.digester(batch, objective)
            except Exception:  # noqa: BLE001 - fall back to the heuristic
                data = None
        if not data or not data.get("conclusion"):
            data = heuristic_digest(batch)
        evidence = []
        for raw in data.get("evidence") or []:
            try:
                evidence.append(Evidence.model_validate(raw))
            except Exception:  # noqa: BLE001
                continue
        art = Artifact(
            kind="digest",
            title=f"digest of {len(batch)} artifacts",
            conclusion=str(data["conclusion"])[:4000],
            confidence=float(data.get("confidence", 0.5)),
            evidence=evidence[:20],
            reasoning_summary=str(data.get("reasoning_summary", ""))[:1500],
            contradictions=[str(c) for c in data.get("contradictions") or []][:15],
            unresolved=[str(u) for u in data.get("unresolved") or []][:15],
            provenance=Provenance(task_id=task_id, node_id=node_id, capability="digest", inputs=[a.id for a in batch]),
            parents=[a.id for a in batch],
            tags=["digest"],
        )
        self.store.put(art)
        return art
