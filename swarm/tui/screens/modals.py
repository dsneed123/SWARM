"""Modal dialogs: new task, approvals, questions, confirmations, prompts."""

from __future__ import annotations

from typing import Any

from rich.markup import escape
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, OptionList, Select, Static, TextArea
from textual.widgets.option_list import Option


class NewTaskModal(ModalScreen[dict[str, Any] | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel"), Binding("ctrl+s", "submit", "Submit")]

    def __init__(self, workflows: list[dict[str, Any]], default_mode: str, models: list[str]) -> None:
        super().__init__()
        self.workflows = workflows
        self.default_mode = default_mode
        self.models = models

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("[b]New objective[/]  (Ctrl+S to submit, Esc to cancel)")
            yield TextArea(id="objective")
            with Horizontal():
                yield Label("Mode ")
                yield Select([("autonomous", "autonomous"), ("interactive", "interactive")], value=self.default_mode, id="mode", allow_blank=False)
                yield Label(" Workflow ")
                yield Select([("automatic (orchestrator plans)", "")] + [(w["name"], w["slug"]) for w in self.workflows if not w.get("error")],
                             value="", id="workflow", allow_blank=False)
            with Horizontal():
                yield Label("Tier ")
                yield Select([("auto", ""), ("fast", "fast"), ("standard", "standard"), ("deep", "deep")], value="", id="tier", allow_blank=False)
                yield Label(" Model pin ")
                yield Select([("none", "")] + [(m, m) for m in self.models], value="", id="model", allow_blank=False)
                yield Label(" Redundancy ")
                yield Input(placeholder="auto", id="redundancy")
                yield Label(" Priority ")
                yield Input(value="0", id="priority")
            with Horizontal():
                yield Button("Submit", variant="primary", id="ok")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#objective", TextArea).focus()

    @on(Button.Pressed, "#ok")
    def action_submit(self) -> None:
        objective = self.query_one("#objective", TextArea).text.strip()
        if not objective:
            self.notify("Objective is empty", severity="warning")
            return
        overrides: dict[str, Any] = {}
        tier = self.query_one("#tier", Select).value
        model = self.query_one("#model", Select).value
        red = self.query_one("#redundancy", Input).value.strip()
        if tier:
            overrides["tier"] = tier
        if model:
            overrides["model"] = model
        if red.isdigit():
            overrides["redundancy"] = int(red)
        prio = self.query_one("#priority", Input).value.strip()
        self.dismiss({
            "objective": objective,
            "mode": self.query_one("#mode", Select).value,
            "workflow": self.query_one("#workflow", Select).value or None,
            "overrides": overrides,
            "priority": int(prio) if prio.lstrip("-").isdigit() else 0,
        })

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


class ApprovalModal(ModalScreen[str]):
    BINDINGS = [
        Binding("a", "decide('allow')", "Allow"), Binding("t", "decide('allow_task')", "Allow for task"),
        Binding("d", "decide('deny')", "Deny"), Binding("x", "decide('deny_task')", "Deny for task"),
        Binding("escape", "decide('later')", "Later"),
    ]

    def __init__(self, req: dict[str, Any]) -> None:
        super().__init__()
        self.req = req

    def compose(self) -> ComposeResult:
        r = self.req
        args = "\n".join(f"  {k}: {escape(str(v))[:300]}" for k, v in (r.get("arguments") or {}).items())
        with Vertical(id="dialog"):
            yield Label(f"[b yellow]Tool approval requested[/]   task {escape(str(r.get('task_id')))}  agent {escape(str(r.get('agent_id')))}")
            yield Static(f"[b]{escape(str(r.get('tool')))}[/]\n{args}")
            yield Static("[dim]a allow once · t allow for this task · d deny · x deny for this task · Esc decide later[/]")
            with Horizontal():
                yield Button("Allow (a)", variant="success", id="allow")
                yield Button("Allow for task (t)", id="allow_task")
                yield Button("Deny (d)", variant="error", id="deny")
                yield Button("Later (Esc)", id="later")

    def on_button_pressed(self, ev: Button.Pressed) -> None:
        self.dismiss(ev.button.id or "later")

    def action_decide(self, decision: str) -> None:
        self.dismiss(decision)


class QuestionModal(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "later", "Later")]

    def __init__(self, task_id: str, question: dict[str, Any]) -> None:
        super().__init__()
        self.task_id = task_id
        self.question = question

    def compose(self) -> ComposeResult:
        q = self.question
        with Vertical(id="dialog"):
            yield Label(f"[b yellow]The swarm needs your input[/]  (task {escape(self.task_id)})")
            yield Static(escape(str(q.get("text", ""))))
            if q.get("options"):
                yield OptionList(*[Option(escape(o), id=f"opt{i}") for i, o in enumerate(q["options"])], id="options")
            yield Input(placeholder="or type a free-form answer and press Enter", id="free")
            yield Static("[dim]Enter selects · Esc answers later[/]")

    def on_mount(self) -> None:
        if self.question.get("options"):
            self.query_one("#options", OptionList).focus()
        else:
            self.query_one("#free", Input).focus()

    @on(OptionList.OptionSelected)
    def _picked(self, ev: OptionList.OptionSelected) -> None:
        idx = int(str(ev.option.id)[3:])
        self.dismiss(self.question["options"][idx])

    @on(Input.Submitted, "#free")
    def _typed(self, ev: Input.Submitted) -> None:
        if ev.value.strip():
            self.dismiss(ev.value.strip())

    def action_later(self) -> None:
        self.dismiss(None)


class ConfirmModal(ModalScreen[bool]):
    BINDINGS = [Binding("y", "yes", "Yes"), Binding("n", "no", "No"), Binding("escape", "no", "No")]

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(self.text)
            with Horizontal():
                yield Button("Yes (y)", variant="error", id="yes")
                yield Button("No (n)", id="no")

    def on_button_pressed(self, ev: Button.Pressed) -> None:
        self.dismiss(ev.button.id == "yes")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


class PromptModal(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, placeholder: str = "", value: str = "", multiline: bool = False) -> None:
        super().__init__()
        self.title_text = title
        self.placeholder = placeholder
        self.value = value
        self.multiline = multiline

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.title_text)
            if self.multiline:
                yield TextArea(self.value, id="ta")
                yield Static("[dim]Ctrl+S to accept · Esc to cancel[/]")
            else:
                yield Input(value=self.value, placeholder=self.placeholder, id="inp")

    def on_mount(self) -> None:
        (self.query_one("#ta", TextArea) if self.multiline else self.query_one("#inp", Input)).focus()

    @on(Input.Submitted, "#inp")
    def _submit(self, ev: Input.Submitted) -> None:
        self.dismiss(ev.value)

    def key_ctrl_s(self) -> None:
        if self.multiline:
            self.dismiss(self.query_one("#ta", TextArea).text)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ReassignModal(ModalScreen[dict[str, Any] | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, node_id: str, models: list[str]) -> None:
        super().__init__()
        self.node_id = node_id
        self.models = models

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(f"[b]Reassign node {escape(self.node_id)}[/]")
            with Horizontal():
                yield Label("Tier ")
                yield Select([("keep", ""), ("fast", "fast"), ("standard", "standard"), ("deep", "deep")], value="", id="tier", allow_blank=False)
                yield Label(" Model ")
                yield Select([("router decides", "")] + [(m, m) for m in self.models], value="", id="model", allow_blank=False)
            with Horizontal():
                yield Button("Apply", variant="primary", id="ok")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, ev: Button.Pressed) -> None:
        if ev.button.id == "ok":
            self.dismiss({"tier": self.query_one("#tier", Select).value or None,
                          "model": self.query_one("#model", Select).value or None})
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


HELP = """[b]SWARM control center[/]

[b]Global[/]
  n        new objective            d  dashboard         w  workflows
  h        hardware & settings      p  permissions        l  learning / memory
  ?        this help                q  quit               Esc back

[b]Dashboard[/]
  Enter    open selected task/agent   space  pause / resume task     c  cancel task
  Tab      move between panels        r      refresh models          A  pending approvals

[b]Task view[/]
  Enter    open agent / artifact      r  retry failed node          m  reassign model / tier
  space    pause / resume             c  cancel                     f  final result
  a        artifacts                  Tab  switch panel

[b]Hardware & settings[/]
  + / -    memory ceiling ±5%         u  unload selected model      r  refresh models
  m        toggle default mode

[b]Workflow editor[/]
  Ctrl+N   add node                 Enter  edit selected node       Ctrl+R  remove node
  Ctrl+S   save                     Esc    close

[b]Permissions[/]
  1 / 2 / 3   profile safe / normal / autonomous
  a / s / d   set allow / ask / deny for the selected tool    x  clear override
"""


class HelpModal(ModalScreen[None]):
    BINDINGS = [Binding("escape", "close", "Close"), Binding("question_mark", "close", "Close")]

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(HELP)

    def action_close(self) -> None:
        self.dismiss(None)
