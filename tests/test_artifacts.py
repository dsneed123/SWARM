from __future__ import annotations

import pytest

from swarm.artifacts import Artifact, ArtifactStore, Evidence, Provenance
from tests.conftest import make_artifact


def test_id_is_content_hash():
    a = make_artifact()
    b = make_artifact()
    assert a.id == b.id
    c = make_artifact(conclusion="the sky is green")
    assert c.id != a.id


def test_id_tampering_rejected():
    a = make_artifact()
    data = a.model_dump()
    data["conclusion"] = "changed"
    with pytest.raises(ValueError):
        Artifact.model_validate(data)


def test_revise_links_parent():
    a = make_artifact()
    b = a.revise(conclusion="revised")
    assert b.parents == [a.id]
    assert b.version == 2
    assert b.id != a.id


def test_compact_view_keeps_essentials():
    a = make_artifact(
        evidence=[
            Evidence(claim="x", source="https://a", source_type="web", quality=0.9),
            Evidence(claim="y", source="https://b", source_type="web", quality=0.3),
        ],
        contradictions=["z disagrees"],
        unresolved=["what about w"],
        content="a" * 5000,
    )
    v = a.compact(max_evidence=1)
    assert v["conclusion"] == "the sky is blue"
    assert len(v["evidence"]) == 1 and v["evidence"][0]["source"] == "https://a"
    assert v["contradictions"] == ["z disagrees"]
    assert "content" not in v


def test_evidence_score_prefers_external_quality():
    weak = make_artifact(evidence=[Evidence(claim="c", quality=0.2)])
    strong = make_artifact(
        evidence=[
            Evidence(claim="c", source="https://x", source_type="web", quality=0.9),
            Evidence(claim="d", source="https://y", source_type="web", quality=0.8),
        ]
    )
    assert make_artifact().evidence_score() < weak.evidence_score() < strong.evidence_score()


def test_store_roundtrip_and_queries(workspace):
    store = ArtifactStore(workspace / "artifacts", workspace / "state.db")
    a = make_artifact(node_id="n1")
    b = make_artifact(conclusion="other", node_id="n2")
    store.put(a)
    store.put(b)
    store.put(a)  # idempotent
    assert store.count("task_1") == 2
    assert store.get(a.id) == a
    assert [x.id for x in store.for_node("task_1", "n2")] == [b.id]
    c = b.revise(conclusion="other v2")
    store.put(c)
    assert [x.id for x in store.lineage(c.id)] == [c.id, b.id]
    assert store.get("art_missing") is None
    store.close()


def test_provenance_carries_inputs():
    a = make_artifact()
    b = Artifact(
        conclusion="derived",
        provenance=Provenance(task_id="task_1", node_id="n2", inputs=[a.id]),
    )
    assert b.provenance.inputs == [a.id]
