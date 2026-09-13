"""Drives the TUI headlessly with Textual's pilot against the fake backend."""

from __future__ import annotations

import asyncio

import pytest

from swarm.app import build_app
from swarm.config import Settings
from swarm.models.fake import FakeBackend, small_models
from swarm.service.api import Api
from swarm.service.protocol import LocalClient
from swarm.tui.app import SwarmApp
from swarm.tui.screens.agent import AgentScreen, ArtifactScreen
from swarm.tui.screens.dashboard import DashboardScreen
from swarm.tui.screens.learning import LearningScreen
from swarm.tui.screens.modals import HelpModal, NewTaskModal
from swarm.tui.screens.permissions import PermissionsScreen
from swarm.tui.screens.settings import SettingsScreen
from swarm.tui.screens.task import TaskScreen
from swarm.tui.screens.workflows import WorkflowEditor, WorkflowsScreen
from tests.helpers import structured


def responder(model, messages, tools, schema):
    return structured("demo answer", 0.8, content="long body")


@pytest.fixture
async def tui(tmp_path):
    settings = Settings(workspace=tmp_path / "ws")
    app_core = build_app(settings, backend=FakeBackend(small_models(), responder=responder))
    await app_core.start()
    tui = SwarmApp(LocalClient(Api(app_core)), "test", app_core.stop)
    yield tui, app_core


async def test_dashboard_and_navigation(tui):
    app, core = tui
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause(0.3)
        assert isinstance(app.screen, DashboardScreen)
        assert app.snapshot.get("scheduler")
        # submit a task through the API and let it finish
        task = core.orchestrator.submit("What is a test?")
        for _ in range(100):
            await pilot.pause(0.1)
            if core.orchestrator.tasks.get(task.id).status.terminal:
                break
        await app.refresh_snapshot()
        await pilot.pause(0.2)
        table = app.screen.query_one("#tasks")
        assert table.row_count >= 1
        # open the task
        await pilot.press("enter")
        await pilot.pause(0.5)
        assert isinstance(app.screen, TaskScreen)
        assert app.screen.query_one("#nodes").row_count >= 1
        await pilot.press("f")
        await pilot.pause(0.3)
        assert isinstance(app.screen, ArtifactScreen)
        assert "Sources" in str(app.screen.query_one("#body").render()) or "Conclusion" in str(app.screen.query_one("#body").render())
        await pilot.press("j")  # raw json
        await pilot.press("escape")
        await pilot.pause(0.2)
        assert isinstance(app.screen, TaskScreen)
        # agents table -> agent screen
        app.screen.query_one("#agents").focus()
        await pilot.press("enter")
        await pilot.pause(0.4)
        assert isinstance(app.screen, AgentScreen)
        assert "Instruction" in str(app.screen.query_one("#body").render())
        await pilot.press("escape")
        await pilot.press("escape")
        await pilot.pause(0.2)
        assert isinstance(app.screen, DashboardScreen)
        # other screens
        for key, cls in (("w", WorkflowsScreen), ("h", SettingsScreen), ("p", PermissionsScreen), ("l", LearningScreen)):
            await pilot.press(key)
            await pilot.pause(0.4)
            assert isinstance(app.screen, cls), key
        await pilot.press("d")
        await pilot.pause(0.2)
        assert isinstance(app.screen, DashboardScreen)
        await pilot.press("question_mark")
        await pilot.pause(0.2)
        assert isinstance(app.screen, HelpModal)
        await pilot.press("escape")


async def test_new_task_modal_submits(tui):
    app, core = tui
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause(0.3)
        await pilot.press("n")
        await pilot.pause(0.4)
        assert isinstance(app.screen, NewTaskModal)
        await pilot.press(*"hello swarm")
        await pilot.press("ctrl+s")
        await pilot.pause(0.5)
        assert core.orchestrator.tasks.recent()[0].objective == "hello swarm"


async def test_settings_ceiling_and_permissions(tui):
    app, core = tui
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause(0.3)
        await pilot.press("h")
        await pilot.pause(0.3)
        before = core.settings.hardware.memory_ceiling_percent
        await pilot.press("minus")
        await pilot.pause(0.3)
        assert core.settings.hardware.memory_ceiling_percent == before - 5
        assert core.scheduler.budget.ceiling_percent == before - 5
        await pilot.press("escape")
        await pilot.press("p")
        await pilot.pause(0.4)
        await pilot.press("1")
        await pilot.pause(0.3)
        assert core.tools.resolver.profile == "safe"
        await pilot.press("d")
        await pilot.pause(0.3)
        assert core.tools.resolver.overrides  # an override was set on the selected tool


async def test_workflow_editor_saves(tui):
    app, core = tui
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause(0.3)
        await pilot.press("w")
        await pilot.pause(0.4)
        await pilot.press("n")
        await pilot.pause(0.4)
        assert isinstance(app.screen, WorkflowEditor)
        await pilot.press(*"My flow")
        await pilot.press("ctrl+n")
        await pilot.pause(0.4)
        await pilot.press(*"step1")
        await pilot.press("ctrl+s")
        await pilot.pause(0.3)
        assert isinstance(app.screen, WorkflowEditor)
        assert app.screen.spec["nodes"][0]["id"] == "step1"
        await pilot.press("ctrl+s")
        await pilot.pause(0.4)
        assert any(w["slug"] == "my-flow" for w in core.orchestrator.workflows.list())


async def test_approval_modal_flow(tui):
    app, core = tui
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause(0.3)
        fut = asyncio.ensure_future(core.approvals.request("shell", {"command": "ls"}, task_id="t", agent_id="a", reason="ls"))
        await pilot.pause(0.3)
        await app.refresh_snapshot()
        await pilot.pause(0.3)
        from swarm.tui.screens.modals import ApprovalModal

        assert isinstance(app.screen, ApprovalModal)
        await pilot.press("a")
        await pilot.pause(0.3)
        assert await fut is True
