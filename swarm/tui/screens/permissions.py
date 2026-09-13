"""Tool permissions: profile and per-tool overrides."""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Static

from swarm.tui.format import trunc
from swarm.tui.screens.dashboard import _refill

POLICY_STYLE = {"allow": "green", "ask": "yellow", "deny": "red"}


class PermissionsScreen(Screen):
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("1", "profile('safe')", "SAFE"),
        Binding("2", "profile('normal')", "NORMAL"),
        Binding("3", "profile('autonomous')", "AUTONOMOUS"),
        Binding("a", "set('allow')", "Allow"),
        Binding("s", "set('ask')", "Ask"),
        Binding("d", "set('deny')", "Deny"),
        Binding("x", "set('')", "Clear override"),
    ]

    def compose(self) -> ComposeResult:
        yield Static("permissions", classes="title")
        yield Static(id="profile", classes="detail")
        with Vertical(classes="panel"):
            yield Static("Tools  (a allow · s ask · d deny · x clear override · 1/2/3 profile)", classes="panel-title")
            yield DataTable(id="tools", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#tools", DataTable).add_columns("tool", "category", "side effects", "policy", "source", "calls", "denied", "description")
        self.query_one("#tools", DataTable).focus()
        self.run_worker(self.reload())
        self.set_interval(2.0, self.reload)

    def update_from(self, snap: dict[str, Any]) -> None:
        p = snap.get("permissions") or {}
        self.query_one("#profile", Static).update(
            f"[b]Profile[/] {p.get('profile', '?').upper()}    overrides: {p.get('overrides') or 'none'}\n"
            "[dim]SAFE: read/network allowed, writes ask, execution and external services denied. "
            "NORMAL: files and python allowed, shell/github/email ask. AUTONOMOUS: everything allowed except email (ask). "
            "Workflows and nodes can tighten these, never loosen them. Filesystem tools are confined to the workspace.[/]"
        )

    async def reload(self) -> None:
        tools = await self.app.call("tools") or []
        _refill(self.query_one("#tools", DataTable), [
            (t["name"], t["category"], "yes" if t["side_effects"] else "", f"[{POLICY_STYLE.get(t['policy'], 'white')}]{t['policy']}[/]",
             t["source"], str(t.get("calls", 0)), str(t.get("denied", 0)), trunc(t.get("description"), 60))
            for t in tools
        ], 0)
        if self.app.snapshot:
            self.update_from(self.app.snapshot)

    def _selected(self) -> str | None:
        t = self.query_one("#tools", DataTable)
        if not t.row_count:
            return None
        try:
            return str(t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value)
        except Exception:  # noqa: BLE001
            return None

    async def action_profile(self, profile: str) -> None:
        await self.app.call("settings", changes={"permission_profile": profile})
        await self.app.refresh_snapshot()
        await self.reload()

    async def action_set(self, policy: str) -> None:
        name = self._selected()
        if name:
            await self.app.call("settings", changes={"tool_overrides": {name: policy or None}})
            await self.app.refresh_snapshot()
            await self.reload()
