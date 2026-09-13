from __future__ import annotations

from swarm.core.db import Database
from swarm.core.types import NodeStatus, TaskStatus
from swarm.memory.learning import WorkflowKnowledge, signature_of
from swarm.memory.store import FailureLog, WorldKnowledge
from swarm.tasks.queue import Task, TaskQueue, TaskStore
from swarm.workflow.dag import NodeSpec, WorkflowSpec


def test_world_knowledge_conservative_add_and_search(tmp_path):
    wk = WorldKnowledge(Database(tmp_path / "db"))
    assert not wk.add("too short", source=None, retrieved_at=None, confidence=0.9)
    assert not wk.add("The Golden Gate Bridge opened in 1937 to pedestrians", source="https://a", retrieved_at="t", confidence=0.4)
    assert wk.add("The Golden Gate Bridge opened in 1937 to pedestrians", source="https://a", retrieved_at="2026-01-01", confidence=0.9)
    assert not wk.add("The Golden Gate Bridge opened in 1937 to pedestrians", source="https://a", retrieved_at="t", confidence=0.9)  # dedupe
    wk.add("Python 3.12 removed distutils from the standard library", source="https://b", retrieved_at="t", confidence=0.8)
    hits = wk.search("when did the golden gate bridge open")
    assert hits and "Golden Gate" in hits[0].statement and hits[0].retrieved_at == "2026-01-01"
    assert wk.count() == 2
    wk.MAX_FACTS = 1
    assert wk.prune() == 1 and wk.count() == 1


def test_failure_log(tmp_path):
    fl = FailureLog(Database(tmp_path / "db"))
    fl.record(task_id="t", node_id="n", model="m", capability="research", kind="format", action="switch_model", message="bad json")
    fl.record(task_id="t", node_id="n", model="m", capability="coding", kind="timeout", action="retry", message="slow")
    assert fl.count_for("m") == 2 and fl.count_for("m", "coding") == 1
    assert fl.recent(1)[0]["kind"] == "timeout"


def spec():
    return WorkflowSpec(name="w", immutable=False, objective_class="research", nodes=[
        NodeSpec(id="r", capability="research", redundancy=3),
        NodeSpec(id="s", capability="synthesis", depends_on=["r"]),
    ])


def test_workflow_knowledge_learns_best_composition(tmp_path):
    wk = WorkflowKnowledge(Database(tmp_path / "db"))
    assert wk.suggest("research") is None
    sig, _ = signature_of(spec())
    assert sig == "researchx3(auto) -> synthesisx1(auto)"
    wk.record_outcome(objective_class="research", spec=spec(), models=["a", "b"], success=True, confidence=0.9, duration_s=100, task_id="t1")
    other = spec()
    other.nodes[0].redundancy = 1
    wk.record_outcome(objective_class="research", spec=other, models=["a"], success=True, confidence=0.5, duration_s=50, task_id="t2")
    wk.record_outcome(objective_class="research", spec=other, models=["a"], success=False, confidence=0.0, duration_s=50, task_id="t3")
    best = wk.suggest("research")
    assert best.signature == sig and best.runs == 1 and best.models == {"a": 1, "b": 1}
    # slow runs are penalised
    slow = spec()
    slow.nodes[1].redundancy = 2
    wk.record_outcome(objective_class="research", spec=slow, models=[], success=True, confidence=0.95, duration_s=6000, task_id="t4", expected_duration_s=600)
    assert wk.suggest("research").signature == sig
    assert any(s["class"] == "research" for s in wk.summary())


def test_task_store_and_queue(tmp_path):
    store = TaskStore(Database(tmp_path / "db"))
    q = TaskQueue(store)
    t1 = q.add(Task(objective="first"))
    t2 = q.add(Task(objective="urgent", priority=5))
    assert [t.objective for t in q.queued()] == ["urgent", "first"]
    t2.status = TaskStatus.RUNNING
    q.save(t2)
    assert q.active() == [t2] and q.queued() == [t1]
    t2.node_states = {"n": {"status": NodeStatus.RUNNING.value}}
    q.save(t2)
    assert store.get(t2.id).node_states["n"]["status"] == "running"
    # simulate restart
    interrupted = TaskStore(Database(tmp_path / "db")).mark_interrupted()
    assert set(interrupted) == {t1.id, t2.id}
    assert store.get(t2.id).status == TaskStatus.FAILED
    assert q.recent()[0].objective == "urgent"
