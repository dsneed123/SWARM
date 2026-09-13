"""What the swarm has learned: compositions per objective class, recent failures."""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static

from swarm.tui.format import dur, trunc
from swarm.tui.screens.dashboard import _refill


class LearningScreen(Screen):
    BINDINGS = [Binding("escape", "app.pop_screen", "Back")]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(classes="row"):
            with Vertical(classes="half panel"):
                yield Static("Workflow knowledge (best compositions per objective class)", classes="panel-title")
                yield DataTable(id="wk", cursor_type="row", zebra_stripes=True)
            with Vertical(classes="half panel"):
                yield Static("Recent failures and the recovery chosen", classes="panel-title")
                yield DataTable(id="fail", cursor_type="row", zebra_stripes=True)
        yield Static(id="note", classes="detail")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#wk", DataTable).add_columns("class", "composition", "runs", "ok", "score", "conf", "duration", "models")
        self.query_one("#fail", DataTable).add_columns("when", "task", "node", "model", "capability", "kind", "action", "message")
        self.run_worker(self.reload())
        self.set_interval(3.0, self.reload)

    def update_from(self, snap: dict[str, Any]) -> None:
        mem = snap.get("memory") or {}
        self.query_one("#note", Static).update(
            f"[b]Memory[/] {mem.get('facts', 0)} world facts · {mem.get('artifacts', 0)} artifacts · {mem.get('failures', 0)} failure records\n"
            "[dim]Model performance lives in the model profiles (see Hardware). Compositions are recorded only for automatically "
            "planned tasks and used as starting points for similar objectives.[/]"
        )

    async def reload(self) -> None:
        data = await self.app.call("learning") or {}
        _refill(self.query_one("#wk", DataTable), [
            (w["class"], trunc(w["signature"], 50), str(w["runs"]), str(w["successes"]), f"{w['score']:.2f}", f"{w['avg_confidence']:.2f}",
             dur(w["avg_duration_s"]), ",".join(w.get("models") or [])[:30])
            for w in data.get("workflows") or []
        ], 1)
        import time

        _refill(self.query_one("#fail", DataTable), [
            (dur(time.time() - f["ts"]) + " ago", (f.get("task_id") or "")[-8:], f.get("node_id") or "", trunc(f.get("model"), 16), f.get("capability") or "",
             f["kind"], f["action"], trunc(f.get("message"), 40))
            for f in data.get("failures") or []
        ], 0)
        if self.app.snapshot:
            self.update_from(self.app.snapshot)
