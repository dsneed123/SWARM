"""Reusable workflows: list, run, create, edit, delete."""

from __future__ import annotations

from typing import Any

from rich.markup import escape
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Input,
    Label,
    Select,
    Static,
    TextArea,
)

from swarm.tui.format import trunc
from swarm.tui.screens.dashboard import _refill

CAPS = ["research", "reasoning", "analysis", "planning", "criticism", "verification", "coding", "writing", "data", "tool_use", "synthesis", "general"]
TIERS = [("auto", ""), ("fast", "fast"), ("standard", "standard"), ("deep", "deep")]


class WorkflowsScreen(Screen):
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("enter", "run", "Run"),
        Binding("n", "new", "New"),
        Binding("e", "edit", "Edit"),
        Binding("x", "delete", "Delete"),
    ]

    def compose(self) -> ComposeResult:
        yield Static("workflows", classes="title")
        with Vertical(classes="panel"):
            yield Static("Workflows  (Enter runs with an objective · n new · e edit · x delete)", classes="panel-title")
            yield DataTable(id="wf", cursor_type="row")
        yield Static(id="wf_detail", classes="detail")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#wf", DataTable).add_columns("slug", "name", "class", "nodes", "source", "description")
        self.query_one("#wf", DataTable).focus()
        self.run_worker(self.reload())

    def update_from(self, snap: dict[str, Any]) -> None:
        return None

    async def reload(self) -> None:
        self.items = await self.app.call("workflows") or []
        _refill(self.query_one("#wf", DataTable), [
            (w["slug"], trunc(w.get("name"), 28), w.get("objective_class") or "-", str(w.get("nodes", "?")),
             "example" if w.get("example") else "mine", trunc(w.get("error") or w.get("description"), 60))
            for w in self.items
        ], 0)
        await self._render_detail()

    def _selected(self) -> str | None:
        t = self.query_one("#wf", DataTable)
        if not t.row_count:
            return None
        try:
            return str(t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value)
        except Exception:  # noqa: BLE001
            return None

    def on_data_table_row_highlighted(self, ev: DataTable.RowHighlighted) -> None:
        self.run_worker(self._render_detail())

    async def _render_detail(self) -> None:
        slug = self._selected()
        w = self.query_one("#wf_detail", Static)
        if not slug:
            w.update("")
            return
        spec = await self.app.call("workflow", slug=slug)
        if not spec:
            return
        lines = [f"[b]{escape(spec['name'])}[/]  {escape(spec.get('description') or '')}",
                 f"class {spec.get('objective_class') or '-'}  mode {spec.get('mode') or 'default'}  permissions {escape(str(spec.get('permissions') or {}))}"]
        for n in spec["nodes"]:
            lines.append(f"  {n['id']:<14} {n['capability']:<12} {n.get('tier') or 'auto':<8} x{n.get('redundancy', 1)}"
                         f"{' persistent' if n.get('persistent') else ''}  deps {', '.join(n.get('depends_on') or []) or '-'}  "
                         f"[dim]{trunc(n.get('instruction'), 70)}[/]")
        w.update("\n".join(lines))

    async def on_data_table_row_selected(self, ev: DataTable.RowSelected) -> None:
        if ev.data_table.id == "wf":
            await self.action_run()

    async def action_run(self) -> None:
        slug = self._selected()
        if not slug:
            return
        from swarm.tui.screens.modals import PromptModal

        async def done(objective: str | None) -> None:
            if objective and objective.strip():
                brief = await self.app.call("submit", objective=objective.strip(), workflow=slug)
                if brief:
                    self.notify(f"submitted {brief['id']} with {slug}")

        self.app.push_screen(PromptModal(f"Objective for workflow [b]{escape(slug)}[/]", multiline=True), done)

    async def action_new(self) -> None:
        self.app.push_screen(WorkflowEditor(None), lambda _: self.run_worker(self.reload()))

    async def action_edit(self) -> None:
        slug = self._selected()
        if not slug:
            return
        spec = await self.app.call("workflow", slug=slug)
        if spec:
            self.app.push_screen(WorkflowEditor(spec, slug), lambda _: self.run_worker(self.reload()))

    async def action_delete(self) -> None:
        slug = self._selected()
        if not slug:
            return
        from swarm.tui.screens.modals import ConfirmModal

        async def done(ok: bool) -> None:
            if ok:
                deleted = await self.app.call("delete_workflow", slug=slug)
                self.notify("deleted" if deleted else "examples cannot be deleted (edit and save a copy instead)")
                await self.reload()

        self.app.push_screen(ConfirmModal(f"Delete workflow {escape(slug)}?"), done)


class WorkflowEditor(Screen):
    """Form-based editor. Nodes are edited one at a time in a modal."""

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("ctrl+s", "save", "Save"),
        Binding("ctrl+n", "add_node", "Add node"),
        Binding("enter", "edit_node", "Edit node"),
        Binding("ctrl+r", "remove_node", "Remove node"),
    ]

    def __init__(self, spec: dict[str, Any] | None, slug: str | None = None) -> None:
        super().__init__()
        self.spec = spec or {"name": "", "description": "", "objective_class": "", "mode": None, "permissions": {}, "nodes": [], "final_node": None}
        self.slug = slug

    def compose(self) -> ComposeResult:
        s = self.spec
        yield Static("workflows", classes="title")
        with VerticalScroll():
            yield Label("[b]Workflow[/]  (Ctrl+S saves to workspace/workflows · Ctrl+N add node · Enter edit node · Ctrl+R remove node)")
            with Horizontal():
                yield Label("Name "); yield Input(value=s.get("name") or "", id="name")
                yield Label(" Slug "); yield Input(value=self.slug or "", placeholder="auto from name", id="slug")
            with Horizontal():
                yield Label("Description "); yield Input(value=s.get("description") or "", id="description")
            with Horizontal():
                yield Label("Class ")
                yield Select([(c, c) for c in ("", "question", "research", "analysis", "coding", "writing", "action", "mixed")],
                             value=s.get("objective_class") or "", id="class", allow_blank=False)
                yield Label(" Mode ")
                yield Select([("default", ""), ("autonomous", "autonomous"), ("interactive", "interactive")], value=s.get("mode") or "", id="mode", allow_blank=False)
                yield Label(" Final node "); yield Input(value=s.get("final_node") or "", placeholder="last sink", id="final")
            with Horizontal():
                yield Label("Permissions (tool=allow|ask|deny, comma separated) ")
                yield Input(value=", ".join(f"{k}={v}" for k, v in (s.get("permissions") or {}).items()), id="perms")
            yield Static("Nodes", classes="panel-title")
            yield DataTable(id="nodes", cursor_type="row")
            with Horizontal():
                yield Button("Save (Ctrl+S)", variant="primary", id="save")
                yield Button("Close (Esc)", id="close")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#nodes", DataTable).add_columns("id", "capability", "tier", "red", "deps", "tools", "persistent", "instruction")
        self._refresh_nodes()
        self.query_one("#name", Input).focus()

    def update_from(self, snap: dict[str, Any]) -> None:
        return None

    def _refresh_nodes(self) -> None:
        _refill(self.query_one("#nodes", DataTable), [
            (n["id"], n.get("capability", "general"), n.get("tier") or "auto", str(n.get("redundancy", 1)), ",".join(n.get("depends_on") or []),
             ",".join(n["tools"]) if n.get("tools") is not None else "default", "yes" if n.get("persistent") else "", trunc(n.get("instruction"), 40))
            for n in self.spec["nodes"]
        ], 0)

    def _selected_node(self) -> dict[str, Any] | None:
        t = self.query_one("#nodes", DataTable)
        if not t.row_count:
            return None
        try:
            key = str(t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value)
        except Exception:  # noqa: BLE001
            return None
        return next((n for n in self.spec["nodes"] if n["id"] == key), None)

    def action_add_node(self) -> None:
        existing = [n["id"] for n in self.spec["nodes"]]

        def done(node: dict[str, Any] | None) -> None:
            if node:
                self.spec["nodes"].append(node)
                self._refresh_nodes()

        self.app.push_screen(NodeEditor(None, existing), done)

    def on_data_table_row_selected(self, ev: DataTable.RowSelected) -> None:
        if ev.data_table.id == "nodes":
            self.action_edit_node()

    def action_edit_node(self) -> None:
        node = self._selected_node()
        if not node or not isinstance(self.app.focused, DataTable):
            return
        existing = [n["id"] for n in self.spec["nodes"] if n["id"] != node["id"]]

        def done(updated: dict[str, Any] | None) -> None:
            if updated:
                idx = self.spec["nodes"].index(node)
                self.spec["nodes"][idx] = updated
                self._refresh_nodes()

        self.app.push_screen(NodeEditor(node, existing), done)

    def action_remove_node(self) -> None:
        node = self._selected_node()
        if node and isinstance(self.app.focused, DataTable):
            self.spec["nodes"].remove(node)
            self._refresh_nodes()

    @on(Button.Pressed, "#save")
    async def action_save(self) -> None:
        perms: dict[str, str] = {}
        for part in self.query_one("#perms", Input).value.split(","):
            if "=" in part:
                k, v = part.split("=", 1)
                perms[k.strip()] = v.strip()
        spec = {
            "name": self.query_one("#name", Input).value.strip() or "untitled",
            "description": self.query_one("#description", Input).value.strip(),
            "objective_class": self.query_one("#class", Select).value or "",
            "mode": self.query_one("#mode", Select).value or None,
            "final_node": self.query_one("#final", Input).value.strip() or None,
            "permissions": perms,
            "immutable": True,
            "nodes": self.spec["nodes"],
        }
        if not spec["nodes"]:
            self.notify("add at least one node (Ctrl+N)", severity="warning")
            return
        slug = self.query_one("#slug", Input).value.strip() or None
        path = await self.app.call("save_workflow", spec=spec, slug=slug)
        if path:
            self.notify(f"saved {path}")
            self.dismiss(True)

    @on(Button.Pressed, "#close")
    def action_close(self) -> None:
        self.dismiss(False)


class NodeEditor(ModalScreen[dict[str, Any] | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel"), Binding("ctrl+s", "ok", "OK")]

    def __init__(self, node: dict[str, Any] | None, existing_ids: list[str]) -> None:
        super().__init__()
        self.node = node or {}
        self.existing = existing_ids

    def compose(self) -> ComposeResult:
        n = self.node
        c = n.get("consensus") or {}
        with Vertical(id="dialog"):
            yield Label("[b]Node[/]  (Ctrl+S accept · Esc cancel)")
            with Horizontal():
                yield Label("id "); yield Input(value=n.get("id", ""), id="id")
                yield Label(" name "); yield Input(value=n.get("name", ""), id="name")
                yield Label(" capability "); yield Select([(c_, c_) for c_ in CAPS], value=n.get("capability", "research"), id="cap", allow_blank=False)
            with Horizontal():
                yield Label("tier "); yield Select(TIERS, value=n.get("tier") or "", id="tier", allow_blank=False)
                yield Label(" model "); yield Input(value=n.get("model") or "", placeholder="router", id="model")
                yield Label(" redundancy "); yield Input(value=str(n.get("redundancy", 1)), id="red")
                yield Label(" attempts "); yield Input(value=str(n.get("max_attempts", 3)), id="attempts")
            with Horizontal():
                yield Label("depends on "); yield Input(value=",".join(n.get("depends_on") or []), placeholder=",".join(self.existing), id="deps")
                yield Label(" context from "); yield Input(value=",".join(n.get("context_from") or []), placeholder="deps", id="ctx")
            with Horizontal():
                yield Label("tools "); yield Input(value=",".join(n["tools"]) if n.get("tools") is not None else "", placeholder="capability defaults", id="tools")
                yield Label(" permissions "); yield Input(value=", ".join(f"{k}={v}" for k, v in (n.get("permissions") or {}).items()), placeholder="tool=deny", id="perms")
            with Horizontal():
                yield Checkbox("persistent", value=bool(n.get("persistent")), id="persistent")
                yield Checkbox("critical", value=n.get("critical", True), id="critical")
                yield Checkbox("independent models", value=n.get("independent_models", True), id="indep")
                yield Label(" consensus ")
                yield Select([("evidence", "evidence"), ("single", "single")], value=c.get("mode", "evidence"), id="cmode", allow_blank=False)
                yield Label(" extra agents "); yield Input(value=str(c.get("max_extra_agents", 2)), id="cextra")
                yield Checkbox("escalate", value=c.get("escalate", True), id="cesc")
            yield Label("instruction (use {objective} for the user's objective)")
            yield TextArea(n.get("instruction", "{objective}"), id="instruction")
            with Horizontal():
                yield Button("OK", variant="primary", id="ok")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, ev: Button.Pressed) -> None:
        if ev.button.id == "ok":
            self.action_ok()
        else:
            self.dismiss(None)

    def action_ok(self) -> None:
        def csv(v: str) -> list[str]:
            return [x.strip() for x in v.split(",") if x.strip()]

        perms: dict[str, str] = {}
        for part in self.query_one("#perms", Input).value.split(","):
            if "=" in part:
                k, v = part.split("=", 1)
                perms[k.strip()] = v.strip()
        nid = self.query_one("#id", Input).value.strip()
        if not nid:
            self.notify("node id is required", severity="warning")
            return
        if nid in self.existing:
            self.notify("node id already used", severity="warning")
            return
        tools_raw = self.query_one("#tools", Input).value.strip()
        node = {
            "id": nid,
            "name": self.query_one("#name", Input).value.strip(),
            "capability": self.query_one("#cap", Select).value,
            "instruction": self.query_one("#instruction", TextArea).text,
            "tier": self.query_one("#tier", Select).value or None,
            "model": self.query_one("#model", Input).value.strip() or None,
            "redundancy": int(self.query_one("#red", Input).value or 1),
            "max_attempts": int(self.query_one("#attempts", Input).value or 3),
            "depends_on": csv(self.query_one("#deps", Input).value),
            "context_from": csv(self.query_one("#ctx", Input).value) or None,
            "tools": csv(tools_raw) if tools_raw else None,
            "permissions": perms,
            "persistent": self.query_one("#persistent", Checkbox).value,
            "critical": self.query_one("#critical", Checkbox).value,
            "independent_models": self.query_one("#indep", Checkbox).value,
            "consensus": {"mode": self.query_one("#cmode", Select).value,
                          "max_extra_agents": int(self.query_one("#cextra", Input).value or 2),
                          "escalate": self.query_one("#cesc", Checkbox).value},
        }
        self.dismiss(node)

    def action_cancel(self) -> None:
        self.dismiss(None)
