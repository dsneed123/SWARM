"""End-to-end orchestration on the fake backend.

The responder dispatches on the system prompt so planner, grouper, digester
and agents all get realistic structured answers without a GPU.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from swarm.core.types import ExecutionMode, TaskStatus
from swarm.models.backend import ChatMessage
from swarm.orchestrator.orchestrator import Orchestrator
from tests.helpers import Harness, structured


def role_of(messages: list[ChatMessage]) -> str:
    sys_ = messages[0].content if messages and messages[0].role == "system" else ""
    if "You are the planner" in sys_:
        return "planner"
    if "You compare conclusions" in sys_:
        return "grouper"
    if "compress artifacts" in sys_:
        return "digester"
    if "too ambiguous" in sys_:
        return "clarify"
    if "Split a task" in sys_:
        return "split"
    for line in sys_.splitlines():
        if line.startswith("ROLE: "):
            return line[6:].strip()
    return "unknown"


PLAN = {
    "objective_class": "research", "complexity": "moderate", "needs_external_info": True, "accuracy_critical": True,
    "nodes": [
        {"id": "research", "capability": "research", "instruction": "Research: {objective}", "tier": "fast", "redundancy": 3, "depends_on": []},
        {"id": "critique", "capability": "criticism", "instruction": "Critique the research on {objective}", "tier": "standard", "redundancy": 1, "depends_on": ["research"]},
        {"id": "final", "capability": "synthesis", "instruction": "Answer {objective}", "tier": "deep", "redundancy": 1, "depends_on": ["critique"]},
    ],
}


class Scripted:
    """Configurable responder recording what each role saw."""

    def __init__(self, plan: dict[str, Any] | None = PLAN, research_conclusions: list[str] | None = None,
                 groups: list[list[int]] | None = None, fail_roles: dict[str, int] | None = None):
        self.plan = plan
        self.research = research_conclusions or ["Paris is the capital of France"] * 3
        self.groups = groups
        self.fail_roles = dict(fail_roles or {})  # role -> number of times to fail
        self.seen: list[tuple[str, str]] = []
        self.i = 0

    def __call__(self, model, messages, tools, schema):
        role = role_of(messages)
        self.seen.append((role, model))
        if role in self.fail_roles and self.fail_roles[role] > 0:
            self.fail_roles[role] -= 1
            raise RuntimeError(f"{role} exploded")
        if role == "planner":
            return self.plan if self.plan else "not json at all"
        if role == "grouper":
            n = messages[-1].content.count("\n[")
            n = max(n, 1)
            return {"groups": self.groups or [list(range(n))]}
        if role == "digester":
            return {"conclusion": "digest", "confidence": 0.7, "evidence": []}
        if role == "clarify":
            return {"needs_clarification": False}
        if role == "split":
            return {"subtasks": ["part one", "part two"]}
        if role == "research":
            c = self.research[self.i % len(self.research)]
            self.i += 1
            return structured(c, 0.85, evidence=[{"claim": c, "source": f"https://example.org/{self.i}", "source_type": "web", "quality": 0.9}])
        if role == "criticism":
            return structured("The research holds up", 0.8)
        if role == "synthesis":
            ctx = messages[-1].content
            assert "CONTEXT ARTIFACTS" in ctx
            return structured("Final: Paris is the capital of France", 0.9, content="Paris is the capital of France.")
        if role == "reasoning":
            return structured("Arbiter: position with sources wins", 0.8)
        return structured(f"{role} done", 0.75)


async def make(tmp_path, responder, **kw) -> tuple[Harness, Orchestrator]:
    h = await Harness(tmp_path, responder, **kw).start()
    h.settings.orchestrator.max_consensus_rounds = 2
    orch = Orchestrator(h.settings, h.bus, h.scheduler, h.runtime, h.tools, h.approvals, h.artifacts, h.db, h.workspace, h.monitor)
    await orch.start()
    return h, orch


async def wait_done(orch: Orchestrator, task_id: str, timeout: float = 20.0):
    for _ in range(int(timeout / 0.05)):
        t = orch.tasks.get(task_id)
        if t and t.status.terminal:
            return t
        await asyncio.sleep(0.05)
    raise AssertionError(f"task did not finish: {orch.tasks.get(task_id).status}")


async def test_full_research_task_with_consensus_and_citations(tmp_path):
    r = Scripted()
    h, orch = await make(tmp_path, r)
    task = orch.submit("What is the capital of France?")
    t = await wait_done(orch, task.id)
    assert t.status == TaskStatus.COMPLETED, t.error
    detail = orch.task_detail(t.id)
    states = {n["id"]: n["state"]["status"] for n in detail["dag"]["nodes"]}
    assert states == {"research": "completed", "critique": "completed", "final": "completed"}
    research_state = next(n["state"] for n in detail["dag"]["nodes"] if n["id"] == "research")
    assert research_state["consensus"]["status"] == "converged" and len(research_state["artifact_ids"]) == 3
    final = h.artifacts.get(t.result_artifact_id)
    assert final.kind == "final" and "Sources:" in final.content and "https://example.org/" in final.content
    assert final.data["sources"] and final.confidence >= 0.8
    # models: three research agents on the FAST tier share the single fast model (only one fast in the fake set)
    assert h.backend.load_events.count(("load", "small:7b")) == 1
    # learning recorded the composition
    assert orch.learning.suggest("research") is not None
    # world knowledge harvested the sourced claim
    assert orch.world.count() >= 1
    ev = [e["type"] for e in h.events]
    assert "task.planned" in ev and "node.consensus" in ev and "task.finished" in ev
    await orch.stop()
    await h.stop()


async def test_disagreement_spawns_more_agents_then_arbitrates(tmp_path):
    class R(Scripted):
        def __call__(self, model, messages, tools, schema):
            role = role_of(messages)
            if role == "grouper":
                n = len(re.findall(r"^\[\d+\]", messages[-1].content, re.M))
                self.seen.append((role, model))
                return {"groups": [[i] for i in range(n)]}  # every conclusion is its own position
            return super().__call__(model, messages, tools, schema)

    r = R(research_conclusions=["Paris", "Lyon", "Marseille"])
    h, orch = await make(tmp_path, r)
    task = orch.submit("capital?")
    t = await wait_done(orch, task.id)
    assert t.status == TaskStatus.COMPLETED, t.error
    st = orch.tasks.get(t.id).node_states["research"]
    assert st["consensus"]["status"] == "disagreement" and "arbiter" in st["consensus"]
    assert len(st["artifact_ids"]) > 3  # extra agents were deployed
    roles = [x[0] for x in r.seen]
    assert "reasoning" in roles  # arbiter ran on the deep tier
    assert any(e["type"] == "node.more_agents" for e in h.events)
    assert any(e["type"] == "node.escalated" for e in h.events)
    await orch.stop()
    await h.stop()


async def test_planner_falls_back_to_template_when_model_cannot_plan(tmp_path):
    r = Scripted(plan=None)
    h, orch = await make(tmp_path, r)
    task = orch.submit("Write a short poem about the sea")
    t = await wait_done(orch, task.id)
    assert t.status == TaskStatus.COMPLETED, t.error
    assert t.objective_class == "writing"
    planned = next(e for e in h.events if e["type"] == "task.planned")
    assert planned["source"] == "template" and planned["escalated"]
    await orch.stop()
    await h.stop()


async def test_failure_recovery_switches_model_then_succeeds(tmp_path):
    # criticism runs on STANDARD: medium:14b and medium-coder:32b are both there. Fail it twice.
    r = Scripted(fail_roles={"criticism": 2})
    h, orch = await make(tmp_path, r)
    task = orch.submit("capital?")
    t = await wait_done(orch, task.id)
    assert t.status == TaskStatus.COMPLETED, t.error
    st = t.node_states["critique"]
    assert st["attempts"] >= 2 and any("switch_model" in n or "escalate" in n for n in st["notes"])
    crit_models = [m for role, m in r.seen if role == "criticism"]
    assert len(set(crit_models)) >= 2  # did not blindly retry the same model
    assert orch.failures.recent(5)
    await orch.stop()
    await h.stop()


async def test_critical_failure_fails_task_and_records_reason(tmp_path):
    r = Scripted(fail_roles={"synthesis": 99})
    h, orch = await make(tmp_path, r)
    task = orch.submit("capital?")
    t = await wait_done(orch, task.id)
    assert t.status == TaskStatus.FAILED and "final" in t.error
    assert t.node_states["final"]["status"] == "failed"
    await orch.stop()
    await h.stop()


async def test_user_workflow_is_immutable_and_uses_overrides(tmp_path):
    r = Scripted()
    h, orch = await make(tmp_path, r)
    task = orch.submit("capital?", workflow="verified-research")
    t = await wait_done(orch, task.id, timeout=30)
    assert t.status == TaskStatus.COMPLETED, t.error
    assert t.spec.immutable and set(t.node_states) == {"research", "critique", "verify", "synthesis"}
    assert orch.learning.suggest("research") is None  # user workflows are not "learned" as compositions
    await orch.stop()
    await h.stop()


async def test_pause_resume_cancel_and_interventions(tmp_path):
    from swarm.models.fake import FakeModel

    slow = [FakeModel("small:7b", 7, 4.5, latency_s=0.3), FakeModel("medium:14b", 14, 9.0, latency_s=0.3), FakeModel("large:70b", 70, 42.0, latency_s=0.3)]
    r = Scripted()
    h, orch = await make(tmp_path, r, models=slow)
    task = orch.submit("capital?")
    await asyncio.sleep(0.6)
    assert orch.pause(task.id)
    await asyncio.sleep(1.0)
    detail = orch.task_detail(task.id)
    assert detail["status"] in ("paused", "running")
    assert orch.resume(task.id)
    await asyncio.sleep(0.2)
    assert orch.cancel(task.id)
    t = await wait_done(orch, task.id)
    assert t.status == TaskStatus.CANCELLED
    assert [i["action"] for i in t.interventions] == ["pause", "resume", "cancel"]
    await orch.stop()
    await h.stop()


async def test_interactive_mode_asks_and_waits(tmp_path):
    class R(Scripted):
        def __call__(self, model, messages, tools, schema):
            role = role_of(messages)
            if role == "clarify":
                self.seen.append((role, model))
                return {"needs_clarification": True, "question": "Which France?", "options": ["the country", "a village"]}
            if role == "planner":
                assert "the country" in messages[-1].content
            return super().__call__(model, messages, tools, schema)

    h, orch = await make(tmp_path, R())
    task = orch.submit("capital?", mode=ExecutionMode.INTERACTIVE)
    for _ in range(100):
        t = orch.tasks.get(task.id)
        if t.pending_question():
            break
        await asyncio.sleep(0.05)
    q = t.pending_question()
    assert q and q.text == "Which France?" and t.status == TaskStatus.WAITING_USER
    assert orch.answer(task.id, q.id, "the country")
    t = await wait_done(orch, task.id)
    assert t.status == TaskStatus.COMPLETED, t.error
    await orch.stop()
    await h.stop()


async def test_queue_admits_multiple_tasks_concurrently(tmp_path):
    from swarm.models.fake import FakeModel

    slow = [FakeModel("small:7b", 7, 4.5, latency_s=0.2), FakeModel("medium:14b", 14, 9.0, latency_s=0.2), FakeModel("large:70b", 70, 42.0, latency_s=0.2)]
    r = Scripted()
    h, orch = await make(tmp_path, r, models=slow)
    ids = [orch.submit(f"question {i}").id for i in range(3)]
    await asyncio.sleep(0.5)
    assert len(orch.runs) >= 2  # admitted concurrently when hardware allows
    for tid in ids:
        t = await wait_done(orch, tid, timeout=30)
        assert t.status == TaskStatus.COMPLETED, t.error
    await orch.stop()
    await h.stop()


async def test_snapshot_shape(tmp_path):
    h, orch = await make(tmp_path, Scripted())
    task = orch.submit("capital?")
    await wait_done(orch, task.id)
    snap = orch.snapshot()
    for key in ("tasks", "runs", "agents", "scheduler", "hardware", "approvals", "queue", "permissions", "memory"):
        assert key in snap
    assert snap["tasks"][0]["status"] == "completed"
    assert json.dumps(snap)  # serialisable
    agent = snap["agents"][0]
    full = h.runtime.get(agent["id"]).as_dict(full=True)
    assert "transcript" in full and "instruction" in full
    await orch.stop()
    await h.stop()


async def test_reassign_records_intervention_against_previous_model(tmp_path):
    from swarm.models.fake import FakeModel

    slow = [FakeModel("small:7b", 7, 4.5, latency_s=0.4), FakeModel("medium:14b", 14, 9.0, latency_s=0.4), FakeModel("large:70b", 70, 42.0, latency_s=0.4)]
    h, orch = await make(tmp_path, Scripted(), models=slow)
    task = orch.submit("capital?")
    for _ in range(100):
        await asyncio.sleep(0.05)
        run = orch.runs.get(task.id)
        if run and any(a.model for a in h.runtime.agents.values() if a.spec.node_id == "research"):
            break
    assert orch.reassign_model(task.id, "research", "medium:14b")
    t = await wait_done(orch, task.id)
    assert t.status == TaskStatus.COMPLETED, t.error
    assert any(f["kind"] == "user_reassign" and f["model"] == "small:7b" for f in orch.failures.recent(20))
    assert h.profiles.get("small:7b").per_capability["research"].failures >= 1
    assert any(i["action"] == "reassign" for i in t.interventions)
    await orch.stop()
    await h.stop()
