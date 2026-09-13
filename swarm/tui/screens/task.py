"""One task: DAG progress, node states, consensus, agents, artifacts, result."""

from __future__ import annotations

from typing import Any

from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static

from swarm.tui.format import conf, dur, status, trunc
from swarm.tui.screens.dashboard import _refill


class TaskScreen(Screen):
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("enter", "open", "Open"),
        Binding("r", "retry", "Retry node"),
        Binding("m", "reassign", "Reassign"),
        Binding("space", "toggle_pause", "Pause/Resume"),
        Binding("c", "cancel", "Cancel"),
        Binding("f", "final", "Final result"),
        Binding("tab", "focus_next", "Next panel", show=False),
    ]

    def __init__(self, task_id: str) -> None:
        super().__init__()
        self.task_id = task_id
        self.detail: dict[str, Any] = {}

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(id="summary", classes="detail")
        with Horizontal(classes="row"):
            with Vertical(classes="half panel"):
                yield Static("DAG nodes (Enter opens the node's latest artifact)", classes="panel-title")
                yield DataTable(id="nodes", cursor_type="row", zebra_stripes=True)
            with Vertical(classes="half panel"):
                yield Static("Agents (Enter inspects)", classes="panel-title")
                yield DataTable(id="agents", cursor_type="row", zebra_stripes=True)
        with Horizontal(classes="row"):
            with Vertical(classes="half panel"):
                yield Static("Artifacts (Enter opens)", classes="panel-title")
                yield DataTable(id="artifacts", cursor_type="row", zebra_stripes=True)
            with VerticalScroll(classes="half panel"):
                yield Static("Selected node", classes="panel-title")
                yield Static(id="node_detail")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#nodes", DataTable).add_columns("node", "capability", "tier", "status", "att", "agents", "consensus", "conf", "elapsed")
        self.query_one("#agents", DataTable).add_columns("id", "role", "status", "model", "tools", "elapsed", "error")
        self.query_one("#artifacts", DataTable).add_columns("id", "kind", "node", "conf", "title")
        self.query_one("#nodes", DataTable).focus()
        self.set_interval(1.0, self.reload)
        self.run_worker(self.reload())

    async def reload(self) -> None:
        d = await self.app.call("task", task_id=self.task_id)
        if d:
            self.detail = d
            self.render_detail()

    def update_from(self, snap: dict[str, Any]) -> None:
        return None  # this screen polls its own detail

    def render_detail(self) -> None:
        d = self.detail
        dag = d.get("dag") or {}
        q = d.get("pending_question")
        head = (
            f"[b]{escape(d['objective'])}[/]\n"
            f"{d['id']}  {status(d['status'])}  mode {d['mode']}  class {d.get('objective_class') or '-'}  "
            f"workflow {escape(str(d.get('workflow') or 'auto'))} ({'dynamic' if dag.get('mutable') else 'fixed'})  "
            f"elapsed {dur(d.get('elapsed_s'))}" + ("  [yellow]hurry[/]" if d.get("hurry") else "")
            + (f"\n[bold yellow]Waiting for you:[/] {escape(q['text'])}" if q else "")
            + (f"\n[red]error:[/] {escape(str(d.get('error')))}" if d.get("error") else "")
            + (f"\n[green]result:[/] {trunc(d.get('summary'), 200)}" if d.get("summary") else "")
        )
        if dag.get("mutations"):
            head += "\n[dim]DAG changes: " + "; ".join(f"{m['op']} {m['node']} ({m.get('reason', '')})" for m in dag["mutations"][-4:]) + "[/]"
        self.query_one("#summary", Static).update(head)
        rows = []
        for n in dag.get("nodes") or []:
            st = n.get("state") or {}
            cs = st.get("consensus") or {}
            rows.append((
                n["id"], n["capability"], n.get("tier") or "auto", status(st.get("status")), str(st.get("attempts", 0)),
                f"{len(st.get('artifact_ids', []))}/{n.get('redundancy', 1)}",
                status(cs.get("status")) if cs else "-", conf(cs.get("confidence")) if cs else "-", dur(n.get("elapsed_s") or st.get("elapsed_s")),
            ))
        _refill(self.query_one("#nodes", DataTable), rows, 0)
        _refill(self.query_one("#agents", DataTable), [
            (a["id"], trunc(a["role"], 20), status(a["status"]), trunc(a.get("model") or "-", 16),
             ",".join(dict.fromkeys(a.get("tools") or []))[:12] or "-", dur(a.get("elapsed_s")), trunc(a.get("error"), 30) or "-")
            for a in sorted(d.get("agents") or [], key=lambda a: a["status"] in ("completed", "failed", "cancelled"))
        ], 0)
        _refill(self.query_one("#artifacts", DataTable), [
            (a["id"], a["kind"], a["node"], conf(a.get("confidence")), trunc(a.get("title"), 30)) for a in reversed(d.get("artifacts") or [])
        ], 0)
        self._render_node()

    def _selected_key(self, table_id: str) -> str | None:
        t = self.query_one(f"#{table_id}", DataTable)
        if t.row_count == 0:
            return None
        try:
            return str(t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value)
        except Exception:  # noqa: BLE001
            return None

    def on_data_table_row_highlighted(self, ev: DataTable.RowHighlighted) -> None:
        if ev.data_table.id == "nodes":
            self._render_node()

    def _node(self, nid: str | None) -> dict[str, Any] | None:
        for n in (self.detail.get("dag") or {}).get("nodes") or []:
            if n["id"] == nid:
                return n
        return None

    def _render_node(self) -> None:
        n = self._node(self._selected_key("nodes"))
        w = self.query_one("#node_detail", Static)
        if not n:
            w.update("")
            return
        st = n.get("state") or {}
        cs = st.get("consensus") or {}
        parts = [
            f"[b]{escape(n['id'])}[/] {escape(n.get('name') or '')}  capability {n['capability']}  tier {n.get('tier') or 'auto'}  model {escape(str(n.get('model') or 'router'))}",
            f"redundancy {n.get('redundancy')}  depends on {', '.join(n.get('depends_on') or []) or '-'}  tools {', '.join(n.get('tools') or []) if n.get('tools') is not None else 'capability defaults'}",
            f"persistent {n.get('persistent')}  critical {n.get('critical')}  attempts {st.get('attempts')}/{n.get('max_attempts')}",
            f"[dim]{trunc(n.get('instruction'), 300)}[/]",
        ]
        if cs:
            parts.append(f"[b]consensus[/] {status(cs.get('status'))}: {escape(str(cs.get('reason')))}  winner conf {conf(cs.get('confidence'))}"
                         + ("  [yellow]minority had stronger evidence[/]" if cs.get("minority_stronger") else "")
                         + ("  arbitrated" if cs.get("arbiter") else ""))
            for p in cs.get("positions") or []:
                parts.append(f"  · support {p['support']:.2f} evidence {p['evidence_score']:.2f} ({', '.join(p.get('models') or [])}): {trunc(p['label'], 110)}")
        for note in st.get("notes") or []:
            parts.append(f"[yellow]note:[/] {escape(note)}")
        if st.get("error"):
            parts.append(f"[red]error:[/] {escape(str(st['error']))}")
        w.update("\n".join(parts))

    # --- actions ------------------------------------------------------------

    def on_data_table_row_selected(self, ev: DataTable.RowSelected) -> None:
        self._open_from(ev.data_table, str(ev.row_key.value))

    def action_open(self) -> None:
        focused = self.app.focused
        if isinstance(focused, DataTable):
            self._open_from(focused, self._selected_key(focused.id or ""))

    def _open_from(self, focused: DataTable, key: str | None) -> None:
        if True:
            if focused.id == "agents" and key:
                self.app.open_agent(key)
            elif focused.id == "artifacts" and key:
                self.app.open_artifact(key)
            elif focused.id == "nodes" and key:
                n = self._node(key)
                st = (n or {}).get("state") or {}
                aid = st.get("result_artifact_id") or (st.get("artifact_ids") or [None])[-1]
                if aid:
                    self.app.open_artifact(aid)
                else:
                    self.notify("no artifact for this node yet")

    async def action_retry(self) -> None:
        nid = self._selected_key("nodes")
        if nid:
            ok = await self.app.call("retry_node", task_id=self.task_id, node_id=nid)
            self.notify("retry scheduled" if ok else "node is not in a retryable state (task must be running)")

    async def action_reassign(self) -> None:
        nid = self._selected_key("nodes")
        if not nid:
            return
        from swarm.tui.screens.modals import ReassignModal

        models = [m["name"] for m in (self.app.snapshot.get("scheduler") or {}).get("models", []) if not m.get("disabled")]

        async def done(res: dict[str, Any] | None) -> None:
            if res:
                ok = await self.app.call("reassign", task_id=self.task_id, node_id=nid, model=res.get("model"), tier=res.get("tier"))
                self.notify("reassigned" if ok else "could not reassign (task not running)")

        self.app.push_screen(ReassignModal(nid, models), done)

    async def action_toggle_pause(self) -> None:
        if self.detail.get("status") == "paused":
            await self.app.call("resume", task_id=self.task_id)
        else:
            await self.app.call("pause", task_id=self.task_id)

    async def action_cancel(self) -> None:
        from swarm.tui.screens.modals import ConfirmModal

        async def done(ok: bool) -> None:
            if ok:
                await self.app.call("cancel", task_id=self.task_id)

        self.app.push_screen(ConfirmModal(f"Cancel task {escape(self.task_id)}?"), done)

    def action_final(self) -> None:
        rid = self.detail.get("result_artifact_id")
        if rid:
            self.app.open_artifact(rid)
        else:
            self.notify("no final result yet")
