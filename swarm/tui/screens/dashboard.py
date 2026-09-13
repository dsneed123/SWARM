"""Live swarm dashboard: hardware, tasks, agents, models, events."""

from __future__ import annotations

from typing import Any

from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Static

from swarm.tui.format import conf, dur, status, trunc
from swarm.tui.widgets.panels import EventLog, HardwarePanel


class DashboardScreen(Screen):
    BINDINGS = [
        Binding("enter", "open", "Open", show=True),
        Binding("space", "toggle_pause", "Pause/Resume"),
        Binding("c", "cancel", "Cancel task"),
                Binding("tab", "focus_next", "Next panel", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield Static("swarm", classes="title")
        yield HardwarePanel(id="hw")
        with Vertical(classes="panel"):
            yield Static("tasks   (n new · Enter open · space pause/resume · c cancel)", classes="panel-title")
            yield DataTable(id="tasks", cursor_type="row")
        with Horizontal(classes="row"):
            with Vertical(classes="half panel"):
                yield Static("agents", classes="panel-title")
                yield DataTable(id="agents", cursor_type="row")
            with Vertical(classes="half panel"):
                yield Static("events", classes="panel-title")
                yield EventLog(id="log")
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one("#tasks", DataTable)
        t.add_columns("id", "status", "mode", "objective", "progress", "elapsed", "conf")
        a = self.query_one("#agents", DataTable)
        a.add_columns("role", "status", "model", "task", "tools", "elapsed")
        t.focus()
        if self.app.snapshot:
            self.update_from(self.app.snapshot)
        for e in list(self.app.events):
            self.on_swarm_event(e)

    def on_swarm_event(self, e: dict[str, Any]) -> None:
        self.query_one("#log", EventLog).add_event(e)

    def update_from(self, snap: dict[str, Any]) -> None:
        self.query_one("#hw", HardwarePanel).update_from(snap)
        runs = snap.get("runs") or {}
        _refill(self.query_one("#tasks", DataTable), [
            (
                t["id"],
                status(t["status"]),
                t["mode"][:5],
                trunc(t["objective"], 48),
                _progress(runs.get(t["id"])),
                dur(t["elapsed_s"]) if t["status"] not in ("queued",) else "-",
                _task_conf(t, snap),
            )
            for t in (snap.get("tasks") or [])[:40]
        ], key_index=0)
        live_first = sorted(snap.get("agents") or [], key=lambda a: (a["status"] in ("completed", "failed", "cancelled"), -(a.get("elapsed_s") or 0)))
        _refill(self.query_one("#agents", DataTable), [
            (
                a["id"], trunc(a["role"], 22), status(a["status"]), trunc(a.get("model") or "-", 18),
                a["task_id"][-8:], ",".join(dict.fromkeys(a.get("tools") or []))[:14] or "-", dur(a.get("elapsed_s")),
            )
            for a in live_first[:30]
        ], key_index=0, hide_key=True)

    # --- actions ------------------------------------------------------------

    def _selected(self, table_id: str) -> str | None:
        t = self.query_one(f"#{table_id}", DataTable)
        if t.row_count == 0 or t.cursor_row is None:
            return None
        try:
            return str(t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value)
        except Exception:  # noqa: BLE001
            return None

    def on_data_table_row_selected(self, ev: DataTable.RowSelected) -> None:
        # DataTable owns Enter while focused; it reports the selection here.
        key = str(ev.row_key.value)
        if ev.data_table.id == "agents":
            self.app.open_agent(key)
        elif ev.data_table.id == "tasks":
            self.app.open_task(key)

    def action_open(self) -> None:
        focused = self.app.focused
        if isinstance(focused, DataTable) and focused.id == "agents":
            aid = self._selected("agents")
            if aid:
                self.app.open_agent(aid)
            return
        tid = self._selected("tasks")
        if tid:
            self.app.open_task(tid)

    async def action_toggle_pause(self) -> None:
        tid = self._selected("tasks")
        if not tid:
            return
        task = next((t for t in self.app.snapshot.get("tasks", []) if t["id"] == tid), None)
        if not task:
            return
        if task["status"] == "paused":
            await self.app.call("resume", task_id=tid)
        else:
            await self.app.call("pause", task_id=tid)

    async def action_cancel(self) -> None:
        tid = self._selected("tasks")
        if tid:
            from swarm.tui.screens.modals import ConfirmModal

            async def done(ok: bool) -> None:
                if ok:
                    await self.app.call("cancel", task_id=tid)

            self.app.push_screen(ConfirmModal(f"Cancel task {escape(tid)}?"), done)

    async def action_refresh_models(self) -> None:
        n = await self.app.call("refresh_models")
        self.notify(f"{n} models known")


def _progress(run: dict[str, Any] | None) -> str:
    if not run:
        return "-"
    done, total = run["dag"]["progress"]
    running = sum(1 for n in run["dag"]["nodes"] if n["state"]["status"] in ("running", "consensus"))
    extra = " [magenta]paused[/]" if run.get("paused") else (" [yellow]hurry[/]" if run.get("hurry") else "")
    return f"{done}/{total}" + (f" (+{running} live)" if running else "") + extra


def _task_conf(t: dict[str, Any], snap: dict[str, Any]) -> str:
    run = (snap.get("runs") or {}).get(t["id"])
    if run:
        confs = [n["state"]["consensus"].get("confidence") for n in run["dag"]["nodes"] if n["state"].get("consensus")]
        return conf(confs[-1]) if confs else "-"
    return "-"


def _refill(table: DataTable, rows: list[tuple], key_index: int, hide_key: bool = False) -> None:
    """Replace all rows, keeping the cursor on the same key when possible.
    With hide_key the key column is used for identity only and not displayed."""
    prev_key = None
    if table.row_count and table.cursor_row is not None:
        try:
            prev_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        except Exception:  # noqa: BLE001
            prev_key = None
    table.clear()
    for r in rows:
        cells = tuple(c for i, c in enumerate(r) if not (hide_key and i == key_index))
        table.add_row(*cells, key=str(r[key_index]))
    if prev_key is not None:
        for idx, r in enumerate(rows):
            if str(r[key_index]) == prev_key:
                table.move_cursor(row=idx, animate=False)
                break
