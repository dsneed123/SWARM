from __future__ import annotations

from swarm.artifacts.model import Evidence
from swarm.artifacts.store import ArtifactStore
from swarm.orchestrator.context import ContextBuilder, approx_tokens
from tests.conftest import make_artifact


def many(store, n, node="n"):
    out = []
    for i in range(n):
        a = make_artifact(conclusion=f"conclusion {i} " + "detail " * 60, node_id=f"{node}{i}",
                          evidence=[Evidence(claim="c" * 100, source=f"https://x/{i}", source_type="web", quality=0.8)] * 4,
                          unresolved=[f"open question {i}"])
        store.put(a)
        out.append(a.id)
    return out


async def test_context_fits_untouched(tmp_path):
    store = ArtifactStore(tmp_path / "a", tmp_path / "db")
    ids = many(store, 2)
    views, used = await ContextBuilder(store).build(ids, token_budget=5000, task_id="t", node_id="n")
    assert used == ids and len(views) == 2 and len(views[0]["evidence"]) == 4


async def test_context_compresses_when_over_budget(tmp_path):
    store = ArtifactStore(tmp_path / "a", tmp_path / "db")
    ids = many(store, 8)
    full = [store.get(i).compact() for i in ids]
    budget = approx_tokens(full) // 3
    views, used = await ContextBuilder(store).build(ids, token_budget=budget, task_id="t", node_id="n")
    assert approx_tokens(views) <= budget * 1.2
    digests = [a for a in store.for_task("t", kind="digest")]
    assert digests, "a digest artifact should have been created"
    d = digests[-1]
    assert d.parents and "open question 0" in d.unresolved  # preserved
    assert ids[-1] in used  # newest artifact kept intact


async def test_model_digester_is_used_with_fallback(tmp_path):
    store = ArtifactStore(tmp_path / "a", tmp_path / "db")
    ids = many(store, 8)
    calls = []

    async def digester(arts, objective):
        calls.append(len(arts))
        return {"conclusion": "model digest", "confidence": 0.6, "evidence": [], "unresolved": ["q"]}

    views, used = await ContextBuilder(store, digester).build(ids, token_budget=300, task_id="t", node_id="n")
    assert calls and any(v["conclusion"] == "model digest" for v in views)

    async def broken(arts, objective):
        raise RuntimeError("no model")

    views, used = await ContextBuilder(store, broken).build(ids, token_budget=300, task_id="t", node_id="n")
    assert views  # heuristic fallback
