"""The terminal control center.

Connects to the running service over its Unix socket; if there is none, it
runs the engine in-process (embedded mode) so the app is always usable.
Screens pull a snapshot once a second and stream events for the log and
for approval/question prompts.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections import deque
from typing import Any

from textual.app import App
from textual.binding import Binding

from swarm.config import load_settings
from swarm.service.protocol import LocalClient, SocketClient
from swarm.tui.screens.agent import AgentScreen, ArtifactScreen
from swarm.tui.screens.dashboard import DashboardScreen
from swarm.tui.screens.learning import LearningScreen
from swarm.tui.screens.modals import ApprovalModal, HelpModal, NewTaskModal, QuestionModal
from swarm.tui.screens.permissions import PermissionsScreen
from swarm.tui.screens.settings import SettingsScreen
from swarm.tui.screens.task import TaskScreen
from swarm.tui.screens.workflows import WorkflowsScreen

log = logging.getLogger(__name__)


class SwarmApp(App[None]):
    TITLE = "SWARM"
    CSS = """
    Screen { layout: vertical; }
    #dialog { width: 90%; max-width: 120; height: auto; max-height: 90%; border: thick $accent; background: $surface; padding: 1 2; }
    #dialog TextArea { height: 8; }
    #dialog Horizontal { height: auto; margin-top: 1; }
    #dialog Input { width: 12; }
    #dialog Select { width: 32; }
    .panel { border: round $primary; padding: 0 1; height: 1fr; }
    .panel-title { text-style: bold; color: $accent; }
    DataTable { height: 1fr; }
    HardwarePanel { height: 6; border: round $secondary; padding: 0 1; }
    EventLog { height: 1fr; border: round $secondary; padding: 0 1; }
    .row { height: 1fr; }
    .half { width: 1fr; }
    .detail { height: auto; max-height: 40%; border: round $secondary; padding: 0 1; }
    """
    BINDINGS = [
        Binding("n", "new_task", "New"),
        Binding("d", "go('dashboard')", "Dashboard"),
        Binding("w", "go('workflows')", "Workflows"),
        Binding("h", "go('settings')", "Hardware"),
        Binding("p", "go('permissions')", "Permissions"),
        Binding("l", "go('learning')", "Learning"),
        Binding("A", "approvals", "Approvals"),
        Binding("question_mark", "help", "Help"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, client: LocalClient | SocketClient, mode_label: str, on_exit=None) -> None:
        super().__init__()
        self.client = client
        self.mode_label = mode_label
        self.snapshot: dict[str, Any] = {}
        self.events: deque[dict[str, Any]] = deque(maxlen=1000)
        self._on_exit = on_exit
        self._shown_approvals: set[str] = set()
        self._shown_questions: set[str] = set()
        self._modal_open = False

    async def on_mount(self) -> None:
        self.sub_title = self.mode_label
        self.push_screen(DashboardScreen())
        await self.refresh_snapshot()
        self.set_interval(1.0, self.refresh_snapshot)
        self.run_worker(self._event_pump(), exclusive=True, name="events")
        try:
            for e in await self.client.call("events", {"limit": 60}):
                self.events.append(e)
            self._push_events(list(self.events))
        except Exception:  # noqa: BLE001
            pass

    async def on_unmount(self) -> None:
        try:
            await self.client.close()
        finally:
            if self._on_exit:
                await self._on_exit()

    # --- data -------------------------------------------------------------

    async def refresh_snapshot(self) -> None:
        try:
            self.snapshot = await self.client.call("snapshot")
        except Exception as e:  # noqa: BLE001
            self.sub_title = f"{self.mode_label} · [disconnected: {e}]"
            return
        screen = self.screen
        if hasattr(screen, "update_from") and screen.is_mounted:
            try:
                screen.update_from(self.snapshot)
            except Exception:  # noqa: BLE001
                log.exception("screen update failed")
        self._check_prompts()

    async def _event_pump(self) -> None:
        while True:
            e = await self.client.events.get()
            self.events.append(e)
            self._push_events([e])
            if e.get("type") in ("approval.requested", "task.question"):
                self._check_prompts()

    def _push_events(self, events: list[dict[str, Any]]) -> None:
        screen = self.screen
        if hasattr(screen, "on_swarm_event") and screen.is_mounted:
            for e in events:
                try:
                    screen.on_swarm_event(e)
                except Exception:  # noqa: BLE001
                    log.exception("event handler failed")

    def _check_prompts(self) -> None:
        if self._modal_open:
            return
        for req in self.snapshot.get("approvals") or []:
            if req["id"] not in self._shown_approvals:
                self._shown_approvals.add(req["id"])
                self._open_approval(req)
                return
        for t in self.snapshot.get("tasks") or []:
            q = t.get("pending_question")
            if q and q["id"] not in self._shown_questions:
                self._shown_questions.add(q["id"])
                self._open_question(t["id"], q)
                return

    def _open_approval(self, req: dict[str, Any]) -> None:
        self._modal_open = True

        async def done(decision: str | None) -> None:
            self._modal_open = False
            if decision and decision != "later":
                await self.call("approve", request_id=req["id"], decision=decision)
            else:
                self._shown_approvals.discard(req["id"])

        self.push_screen(ApprovalModal(req), done)

    def _open_question(self, task_id: str, q: dict[str, Any]) -> None:
        self._modal_open = True

        async def done(answer: str | None) -> None:
            self._modal_open = False
            if answer:
                await self.call("answer", task_id=task_id, question_id=q["id"], answer=answer)
            else:
                self._shown_questions.discard(q["id"])

        self.push_screen(QuestionModal(task_id, q), done)

    async def call(self, method: str, **params: Any) -> Any:
        try:
            result = await self.client.call(method, params)
        except Exception as e:  # noqa: BLE001
            self.notify(f"{method} failed: {e}", severity="error", timeout=6)
            return None
        return result

    # --- actions ------------------------------------------------------------

    def action_go(self, where: str) -> None:
        screens = {"dashboard": DashboardScreen, "workflows": WorkflowsScreen, "settings": SettingsScreen,
                   "permissions": PermissionsScreen, "learning": LearningScreen}
        while len(self.screen_stack) > 2:
            self.pop_screen()
        if isinstance(self.screen, screens[where]):
            return
        if where == "dashboard":
            if len(self.screen_stack) > 1:
                self.pop_screen()
            return
        self.push_screen(screens[where]())

    def open_task(self, task_id: str) -> None:
        self.push_screen(TaskScreen(task_id))

    def open_agent(self, agent_id: str) -> None:
        self.push_screen(AgentScreen(agent_id))

    def open_artifact(self, artifact_id: str) -> None:
        self.push_screen(ArtifactScreen(artifact_id))

    async def action_new_task(self) -> None:
        workflows = await self.call("workflows") or []
        models = [m["name"] for m in (self.snapshot.get("scheduler") or {}).get("models", []) if not m.get("disabled")]
        default_mode = (self.snapshot.get("settings") or {}).get("default_mode", "autonomous")

        async def done(result: dict[str, Any] | None) -> None:
            if not result:
                return
            brief = await self.call("submit", **result)
            if brief:
                self.notify(f"submitted {brief['id']}")
                await self.refresh_snapshot()

        self.push_screen(NewTaskModal(workflows, default_mode, models), done)

    def action_approvals(self) -> None:
        pending = self.snapshot.get("approvals") or []
        if not pending:
            self.notify("no pending approvals")
            return
        self._shown_approvals.discard(pending[0]["id"])
        self._check_prompts()

    def action_help(self) -> None:
        self.push_screen(HelpModal())


async def _connect(workspace: str | None, embedded: bool, demo: bool):
    """Return (client, label, on_exit)."""
    settings = load_settings(workspace)
    sock = str(settings.socket_path)
    if not embedded and not demo and os.path.exists(sock):
        client = SocketClient(sock)
        try:
            await client.connect()
            return client, f"service @ {sock}", None
        except (ConnectionRefusedError, FileNotFoundError, OSError):
            pass
    from swarm.app import build_app
    from swarm.service.api import Api

    backend = None
    if demo:
        from swarm.models.fake import FakeBackend, small_models

        def responder(model, messages, tools, schema):
            last = messages[-1].content if messages else ""
            return {"conclusion": f"[{model}] demo answer to: {last[:60]}", "confidence": 0.7, "evidence": [],
                    "reasoning_summary": "demo backend", "content": "This is a demo answer produced without a model."}

        backend = FakeBackend(small_models(), responder=responder)
    app = build_app(settings, backend=backend)
    await app.start()
    client = LocalClient(Api(app))
    label = "embedded (no service running)" if not demo else "demo (fake backend)"
    return client, label, app.stop


def run_tui(workspace: str | None = None, embedded: bool = False, demo: bool = False) -> None:
    async def main() -> None:
        client, label, on_exit = await _connect(workspace, embedded, demo)
        app = SwarmApp(client, label, on_exit)
        await app.run_async()

    asyncio.run(main())
