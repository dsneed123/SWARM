from __future__ import annotations

from swarm.artifacts.model import Evidence, ModelInfo
from swarm.core.types import Tier
from swarm.orchestrator.consensus import HeuristicGrouper, evaluate_consensus
from swarm.orchestrator.recovery import FailureContext, diagnose
from swarm.workflow.dag import ConsensusRule
from tests.conftest import make_artifact


def art(conclusion, confidence=0.8, model="m1", web=0, quality=0.8, **kw):
    ev = [Evidence(claim=f"fact {i}", source=f"https://s{i}.example", source_type="web", quality=quality) for i in range(web)]
    return make_artifact(conclusion=conclusion, confidence=confidence, evidence=ev,
                         model=ModelInfo(model=model, tier="fast"), reasoning_summary="x" * 100, **kw)


async def test_single_artifact():
    r = await evaluate_consensus([art("Paris is the capital of France")], ConsensusRule(), task_id="t", node_id="n")
    assert r.status == "single" and not r.needs_more and r.merged.kind == "consensus"
    r = await evaluate_consensus([art("Paris", confidence=0.3)], ConsensusRule(), task_id="t", node_id="n")
    assert r.needs_more


async def test_agreement_converges_and_boosts_confidence():
    arts = [art("The capital of France is Paris", 0.8, "m1", web=2),
            art("Paris is France's capital city", 0.75, "m2", web=1),
            art("France capital: Paris", 0.7, "m3", web=2)]
    r = await evaluate_consensus(arts, ConsensusRule(), task_id="t", node_id="n")
    assert r.status == "converged" and len(r.positions) == 1 and not r.needs_more
    assert r.merged.confidence >= 0.7 and set(r.merged.parents) == {a.id for a in arts}
    assert r.merged.provenance.inputs == [a.id for a in arts]


async def test_majority_without_evidence_loses_to_strong_minority():
    # Three confident guesses from model knowledge vs one careful sourced answer.
    arts = [art("The bridge opened in 1937", 0.9, "m1"),
            art("The bridge opened in 1937", 0.9, "m2"),
            art("The bridge opened in 1937", 0.85, "m3"),
            art("The bridge opened in 1936 according to the archive", 0.7, "m4", web=3, quality=0.9)]
    r = await evaluate_consensus(arts, ConsensusRule(), task_id="t", node_id="n")
    assert r.status == "disagreement" and r.needs_more and r.minority_stronger
    assert any("alternative position" in c for c in r.merged.contradictions)


async def test_same_model_repeats_count_less():
    arts = [art("answer A", 0.8, "m1"), art("answer A", 0.8, "m1"), art("answer A", 0.8, "m1"),
            art("answer B totally different words here", 0.8, "m2")]
    r = await evaluate_consensus(arts, ConsensusRule(), task_id="t", node_id="n")
    a_pos = next(p for p in r.positions if p.label.startswith("answer A"))
    assert a_pos.support < 3 * 0.8 * 0.35 + 3 * 0.15 * 0.45 + 3 * 0.5 * 0.2  # discounted


async def test_split_disagreement_requests_more():
    arts = [art("The answer is red apples", 0.8, "m1", web=1), art("The answer is blue oranges", 0.8, "m2", web=1)]
    r = await evaluate_consensus(arts, ConsensusRule(), task_id="t", node_id="n")
    assert r.status == "disagreement" and r.needs_more and len(r.positions) == 2


async def test_heuristic_grouper():
    g = HeuristicGrouper()
    groups = await g.group([art("the cat sat on the mat"), art("cat sat on mat"), art("dogs bark loudly at night")])
    assert groups == [[0, 1], [2]]


def fc(kind, attempt=1, **kw):
    base = dict(kind=kind, message="", attempt=attempt, max_attempts=3, critical=True, model="m1",
                tier=Tier.STANDARD, alternatives=1)
    base.update(kw)
    return FailureContext(**base)


def test_diagnosis_strategies():
    assert diagnose(fc("resource")).action == "wait"
    d = diagnose(fc("resource", attempt=2))
    assert d.action == "switch_model" and d.changes["tier"] == Tier.FAST
    assert diagnose(fc("resource", attempt=2, tier=Tier.FAST)).action == "give_up"
    assert diagnose(fc("resource", attempt=2, tier=Tier.FAST, critical=False)).action == "skip"
    assert diagnose(fc("format")).action == "switch_model"
    assert diagnose(fc("format", pinned_model=True)).action == "escalate_tier"
    assert diagnose(fc("format", pinned_model=True, tier=Tier.DEEP)).action == "give_up"
    assert diagnose(fc("timeout", context_chars=20000)).action == "reduce_context"
    assert diagnose(fc("timeout")).action == "switch_model"
    assert diagnose(fc("model", tools_used=4, tools_failed=3)).action == "drop_tools"
    assert diagnose(fc("backend")).action == "wait"
    assert diagnose(fc("backend", attempt=2)).action == "give_up"
    assert diagnose(fc("cancelled")).action == "give_up"
    d = diagnose(fc("model", alternatives=0))
    assert d.action == "escalate_tier" and d.changes["tier"] == Tier.DEEP
    assert diagnose(fc("context", attempt=2)).action == "split"
