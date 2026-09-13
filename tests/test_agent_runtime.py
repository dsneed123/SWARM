from __future__ import annotations

import pytest

from swarm.agents.runtime import AgentError, AgentSpec, parse_json_object
from swarm.core.types import AgentStatus, Tier
from tests.helpers import Harness, structured, tool_call


async def test_agent_runs_without_tools_and_stores_artifact(tmp_path):
    def responder(model, messages, tools, schema):
        assert schema is not None and tools is None
        return structured("42 is the answer", 0.9, evidence=[{"claim": "math", "source_type": "model", "quality": 0.6}])

    h = await Harness(tmp_path, responder).start()
    art = await h.runtime.run(AgentSpec(task_id="t1", node_id="n1", capability="reasoning", instruction="what is 6*7"))
    assert art.conclusion == "42 is the answer" and art.confidence == 0.9
    assert art.model and art.model.model == "medium:14b"  # STANDARD tier default
    assert art.provenance.capability == "reasoning"
    assert h.artifacts.get(art.id) == art
    agent = next(iter(h.runtime.agents.values()))
    assert agent.status == AgentStatus.COMPLETED and agent.artifact_id == art.id
    await h.stop()


async def test_agent_tool_loop_and_source_provenance(tmp_path):
    calls = {"n": 0}

    def responder(model, messages, tools, schema):
        calls["n"] += 1
        if tools and messages[-1].role != "tool":
            return tool_call("write_file", path="notes.txt", content="hello world")
        if tools:
            return "done writing"
        return structured("wrote the file", 0.8,
                          evidence=[{"claim": "file written", "source": "notes.txt", "source_type": "file", "quality": 0.9}])

    h = await Harness(tmp_path, responder).start()
    art = await h.runtime.run(AgentSpec(task_id="t1", node_id="n1", capability="coding", instruction="write hello",
                                        tier=Tier.FAST))
    assert (h.workspace.files / "notes.txt").read_text() == "hello world"
    assert [t.tool for t in art.tools] == ["write_file"] and art.tools[0].ok
    assert art.evidence[0].source == "notes.txt"
    assert calls["n"] == 3  # tool call, tool-free answer, structured
    await h.stop()


async def test_agent_denied_tool_is_reported_not_fatal(tmp_path):
    def responder(model, messages, tools, schema):
        if tools and messages[-1].role != "tool":
            return tool_call("shell", command="rm -rf /")
        if tools:
            assert "not available" in messages[-1].content  # denied tools are never offered
            return "ok"
        return structured("could not run shell", 0.3)

    h = await Harness(tmp_path, responder, profile="safe").start()
    spec = AgentSpec(task_id="t1", node_id="n1", capability="tool_use", instruction="x", tools=["shell", "read_file"])
    art = await h.runtime.run(spec)
    assert art.confidence <= 0.4 and not art.tools[0].ok
    await h.stop()


async def test_agent_salvages_unstructured_output(tmp_path):
    def responder(model, messages, tools, schema):
        return "I think the answer is probably 7 but I can't format JSON"

    h = await Harness(tmp_path, responder).start()
    art = await h.runtime.run(AgentSpec(task_id="t1", node_id="n1", capability="reasoning", instruction="x"))
    assert "probably 7" in art.conclusion and art.confidence == 0.3
    assert h.profiles.get("medium:14b").per_capability["reasoning"].json_failures == 1
    await h.stop()


async def test_agent_failure_kinds(tmp_path):
    def responder(model, messages, tools, schema):
        raise RuntimeError("CUDA out of memory")

    h = await Harness(tmp_path, responder).start()
    with pytest.raises(AgentError) as ei:
        await h.runtime.run(AgentSpec(task_id="t1", node_id="n1", capability="reasoning", instruction="x"))
    assert ei.value.kind == "model"
    agent = next(iter(h.runtime.agents.values()))
    assert agent.status == AgentStatus.FAILED and agent.error_kind == "model"
    await h.stop()


async def test_persistent_agent_keeps_notes(tmp_path):
    seen = []

    def responder(model, messages, tools, schema):
        seen.append(messages[-1].content)
        return structured("ok", 0.8, notes_for_self="remember the cake")

    h = await Harness(tmp_path, responder).start()
    for _ in range(2):
        await h.runtime.run(AgentSpec(task_id="t1", node_id="n1", capability="general", instruction="x",
                                      persistent_key="wf:n1", tools=[]))
    assert "remember the cake" in seen[1] and "remember the cake" not in seen[0]
    assert h.states.get("wf:n1").runs == 2
    await h.stop()


async def test_context_is_included_and_truncated(tmp_path):
    seen = []

    def responder(model, messages, tools, schema):
        seen.append(messages[-1].content)
        return structured("ok")

    h = await Harness(tmp_path, responder).start()
    h.settings.orchestrator.agent_context_tokens = 100
    await h.runtime.run(AgentSpec(task_id="t1", node_id="n1", capability="reasoning", instruction="x",
                                  context=[{"conclusion": "prior"}], context_text="z" * 5000))
    assert '"conclusion": "prior"' in seen[0] and "truncated" in seen[0]
    await h.stop()


def test_parse_json_object_variants():
    assert parse_json_object('{"a": 1}') == {"a": 1}
    assert parse_json_object('text\n```json\n{"a": 2}\n```') == {"a": 2}
    assert parse_json_object('prefix {"a": 3} suffix') == {"a": 3}
    assert parse_json_object("nope") is None
