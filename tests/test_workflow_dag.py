from __future__ import annotations

import pytest

from swarm.core.types import NodeStatus
from swarm.workflow.dag import DAG, NodeSpec, WorkflowSpec, topo_order
from swarm.workflow.library import EXAMPLES_DIR, WorkflowLibrary


def spec(immutable=False) -> WorkflowSpec:
    return WorkflowSpec(
        name="t", immutable=immutable,
        nodes=[
            NodeSpec(id="a", capability="research"),
            NodeSpec(id="b", capability="research"),
            NodeSpec(id="c", capability="criticism", depends_on=["a", "b"]),
            NodeSpec(id="d", capability="synthesis", depends_on=["c"], context_from=["a", "b", "c"]),
        ],
    )


def test_validation_rejects_cycles_and_unknown_deps():
    with pytest.raises(ValueError):
        WorkflowSpec(name="x", nodes=[NodeSpec(id="a", depends_on=["b"]), NodeSpec(id="b", depends_on=["a"])])
    with pytest.raises(ValueError):
        WorkflowSpec(name="x", nodes=[NodeSpec(id="a", depends_on=["zzz"])])
    with pytest.raises(ValueError):
        WorkflowSpec(name="x", nodes=[NodeSpec(id="a"), NodeSpec(id="a")])
    assert topo_order(spec().nodes) == ["a", "b", "c", "d"]
    assert spec().result_node() == "d"


def test_ready_and_progress():
    dag = DAG(spec())
    assert [n.id for n in dag.ready()] == ["a", "b"]
    dag.states["a"].status = NodeStatus.RUNNING
    assert [n.id for n in dag.ready()] == ["b"]
    dag.states["a"].status = NodeStatus.COMPLETED
    dag.states["a"].result_artifact_id = "art_a"
    dag.states["b"].status = NodeStatus.COMPLETED
    dag.states["b"].result_artifact_id = "art_b"
    assert [n.id for n in dag.ready()] == ["c"]
    assert dag.inputs_for("c") == ["art_a", "art_b"]
    assert dag.progress() == (2, 4)
    assert not dag.is_done()
    dag.states["c"].status = NodeStatus.FAILED
    assert dag.blocked() == ["d"]
    assert dag.is_done() and not dag.succeeded()


def test_non_critical_skip_does_not_block():
    s = spec()
    s.node("a").critical = False
    dag = DAG(s)
    dag.states["a"].status = NodeStatus.SKIPPED
    dag.states["b"].status = NodeStatus.COMPLETED
    dag.states["b"].result_artifact_id = "art_b"
    assert [n.id for n in dag.ready()] == ["c"]
    assert dag.inputs_for("c") == ["art_b"]


def test_mutable_dag_can_grow_but_immutable_cannot():
    dag = DAG(spec())
    dag.add_node(NodeSpec(id="e", capability="verification", depends_on=["a"]), reason="check claim")
    assert "e" in dag.nodes and dag.mutations[0]["op"] == "add"
    with pytest.raises(ValueError):
        dag.add_node(NodeSpec(id="e", capability="x"))
    with pytest.raises(ValueError):
        dag.add_node(NodeSpec(id="f", depends_on=["nope"]))
    dag.remove_node("e")
    assert "e" not in dag.nodes
    with pytest.raises(ValueError):
        dag.remove_node("a")  # others depend on it
    user = DAG(spec(immutable=True))
    with pytest.raises(PermissionError):
        user.add_node(NodeSpec(id="e"))
    with pytest.raises(PermissionError):
        user.remove_node("d")


def test_library_roundtrip_and_examples(tmp_path):
    lib = WorkflowLibrary(tmp_path / "wf")
    names = {w["slug"] for w in lib.list()}
    assert {"verified-research", "code-and-review", "daily-briefing"} <= names
    ex = lib.load("verified-research")
    assert ex.immutable and ex.node("research").redundancy == 3
    path = lib.save(spec(immutable=True), "mine")
    assert path.exists()
    again = lib.load("mine")
    assert [n.id for n in again.nodes] == ["a", "b", "c", "d"] and again.node("d").context_from == ["a", "b", "c"]
    assert lib.delete("mine") and not lib.delete("mine")
    assert EXAMPLES_DIR.exists()
