from __future__ import annotations

import asyncio
import os

import pytest

from swarm.core.events import EventBus
from swarm.core.types import Policy
from swarm.paths import Workspace
from swarm.permissions import ApprovalBroker, PermissionResolver, PermissionScope
from swarm.tools.builtin import builtin_tools
from swarm.tools.builtin.web import html_to_text, parse_ddg
from swarm.tools.registry import ToolContext, ToolRegistry


def test_profiles_resolve_by_tool_then_category():
    r = PermissionResolver("safe")
    assert r.resolve("web_search", "network") == Policy.ALLOW
    assert r.resolve("shell", "execute") == Policy.DENY
    assert r.resolve("some_new_tool", "external") == Policy.DENY
    r = PermissionResolver("normal")
    assert r.resolve("shell", "execute") == Policy.ASK
    assert r.resolve("write_file", "write") == Policy.ALLOW
    r = PermissionResolver("autonomous")
    assert r.resolve("shell", "execute") == Policy.ALLOW
    assert r.resolve("email", "external") == Policy.ASK


def test_overrides_layer_and_workflow_cannot_loosen_global_deny():
    r = PermissionResolver("normal", overrides={"github": Policy.ALLOW, "shell": Policy.DENY})
    assert r.resolve("github", "external") == Policy.ALLOW
    scope = PermissionScope(workflow_overrides={"shell": Policy.ALLOW})
    assert r.resolve("shell", "execute", scope) == Policy.DENY  # global deny wins
    scope = PermissionScope(workflow_overrides={"python": Policy.DENY})
    assert r.resolve("python", "execute", scope) == Policy.DENY  # workflows may tighten
    scope = PermissionScope(node_overrides={"github": Policy.DENY}, workflow_overrides={"github": Policy.ALLOW})
    assert r.resolve("github", "external", scope) == Policy.DENY  # node is most specific
    scope = PermissionScope(node_allowed_tools={"web_search"})
    assert r.resolve("python", "execute", scope) == Policy.DENY
    with pytest.raises(ValueError):
        PermissionResolver("yolo")


def make_registry(tmp_path, profile="normal", overrides=None):
    bus = EventBus()
    approvals = ApprovalBroker(bus, timeout_s=0.5)
    reg = ToolRegistry(PermissionResolver(profile, overrides), approvals, bus)
    for t in builtin_tools():
        reg.register(t)
    ws = Workspace(tmp_path / "ws").ensure()
    return reg, approvals, bus, ToolContext(workspace=ws, task_id="task_1", agent_id="agent_1")


async def test_registry_denies_and_asks(tmp_path):
    reg, approvals, bus, ctx = make_registry(tmp_path, "safe")
    r = await reg.invoke("shell", {"command": "echo hi"}, ctx)
    assert not r.ok and "not permitted" in r.error
    # ask -> nobody answers -> denied on timeout
    r = await reg.invoke("write_file", {"path": "a.txt", "content": "x"}, ctx)
    assert not r.ok and "declined" in r.error
    assert not (ctx.workspace.files / "a.txt").exists()


async def test_registry_ask_flow_with_task_grant(tmp_path):
    reg, approvals, bus, ctx = make_registry(tmp_path, "safe")
    events = []
    bus.subscribe(lambda e: events.append(e) if e["type"].startswith("approval") else None)

    async def answer():
        while not approvals.pending:
            await asyncio.sleep(0.01)
        req = next(iter(approvals.pending.values()))
        assert req.tool == "write_file"
        approvals.respond(req.id, "allow_task")

    r, _ = await asyncio.gather(reg.invoke("write_file", {"path": "a.txt", "content": "x"}, ctx), answer())
    assert r.ok and (ctx.workspace.files / "a.txt").read_text() == "x"
    # remembered for the task: no second prompt
    r = await reg.invoke("write_file", {"path": "b.txt", "content": "y"}, ctx)
    assert r.ok
    assert [e["type"] for e in events] == ["approval.requested", "approval.decided"]
    approvals.forget_task("task_1")
    assert approvals.remembered("task_1", "write_file") is None


async def test_file_tools_sandboxed(tmp_path):
    reg, approvals, bus, ctx = make_registry(tmp_path, "normal")
    r = await reg.invoke("write_file", {"path": "sub/x.txt", "content": "hello"}, ctx)
    assert r.ok
    r = await reg.invoke("read_file", {"path": "sub/x.txt"}, ctx)
    assert r.ok and r.output == "hello" and r.sources[0]["path"] == "sub/x.txt"
    r = await reg.invoke("read_file", {"path": "../../etc/passwd"}, ctx)
    assert not r.ok and "permission" in r.error
    r = await reg.invoke("list_files", {}, ctx)
    assert r.ok and "sub" in r.output


async def test_python_tool_runs_in_workspace(tmp_path):
    reg, approvals, bus, ctx = make_registry(tmp_path, "normal")
    r = await reg.invoke("python", {"code": "import os; print(os.getcwd()); print(6*7)"}, ctx)
    assert r.ok and "42" in r.output and str(ctx.workspace.files.resolve()) in r.output
    r = await reg.invoke("python", {"code": "raise SystemExit(3)"}, ctx)
    assert not r.ok and r.data["returncode"] == 3


async def test_unknown_tool(tmp_path):
    reg, *_ , ctx = make_registry(tmp_path)
    r = await reg.invoke("teleport", {}, ctx)
    assert not r.ok


def test_html_to_text_and_ddg_parser():
    text, title = html_to_text("<html><head><title>T</title><script>x()</script></head><body><h1>Hi</h1><p>a &amp; b</p></body></html>")
    assert title == "T" and "Hi" in text and "a & b" in text and "x()" not in text
    page = ('<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fp&amp;rut=1">Example <b>page</b></a>'
            '<a class="result__snippet" href="#">A snippet</a>')
    res = parse_ddg(page, 5)
    assert res == [{"title": "Example page", "url": "https://example.com/p", "snippet": "A snippet"}]


@pytest.mark.skipif(os.environ.get("SWARM_NET_TESTS") != "1", reason="set SWARM_NET_TESTS=1")
async def test_web_tools_live(tmp_path):
    reg, *_ , ctx = make_registry(tmp_path, "normal")
    r = await reg.invoke("web_search", {"query": "python asyncio documentation"}, ctx)
    assert r.ok and r.data["results"]
    r = await reg.invoke("web_fetch", {"url": "https://docs.python.org/3/library/asyncio.html"}, ctx)
    assert r.ok and "asyncio" in r.output.lower() and r.sources[0]["retrieved_at"]


async def test_file_tools_use_project_directory(tmp_path):
    from pathlib import Path

    reg, approvals, bus, ctx = make_registry(tmp_path, "normal")
    project = tmp_path / "project"
    project.mkdir()
    (project / "notes.md").write_text("hello project")
    ctx.project_dir = project
    r = await reg.invoke("read_file", {"path": "notes.md"}, ctx)
    assert r.ok and r.output == "hello project"
    r = await reg.invoke("write_file", {"path": "out/x.txt", "content": "y"}, ctx)
    assert r.ok and (project / "out" / "x.txt").read_text() == "y"
    r = await reg.invoke("read_file", {"path": "../../etc/passwd"}, ctx)
    assert not r.ok
    r = await reg.invoke("python", {"code": "import os; print(os.getcwd())"}, ctx)
    assert r.ok and str(Path(project).resolve()) in r.output
