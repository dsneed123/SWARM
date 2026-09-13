"""Agent inspector and artifact viewer."""

from __future__ import annotations

import json
from typing import Any

from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Header, Static

from swarm.tui.format import conf, dur, gb, status


class AgentScreen(Screen):
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("a", "artifact", "Artifact"),
        Binding("x", "cancel_agent", "Cancel agent"),
        Binding("t", "toggle_transcript", "Transcript"),
    ]

    def __init__(self, agent_id: str) -> None:
        super().__init__()
        self.agent_id = agent_id
        self.data: dict[str, Any] = {}
        self.show_transcript = True

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with VerticalScroll():
            yield Static(id="body")
        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(1.5, self.reload)
        self.run_worker(self.reload())

    def update_from(self, snap: dict[str, Any]) -> None:
        return None

    async def reload(self) -> None:
        d = await self.app.call("agent", agent_id=self.agent_id)
        if d:
            self.data = d
            self.render_body()

    def render_body(self) -> None:
        a = self.data
        parts = [
            f"[b]{escape(a['role'])}[/]  {a['id']}  {status(a['status'])}",
            f"task {a['task_id']}  node {a['node_id']}  capability {a['capability']}  attempt {a.get('attempt')}  persistent {a.get('persistent')}",
            f"model {escape(str(a.get('model') or 'waiting'))}  tier {a.get('tier') or 'auto'}  elapsed {dur(a.get('elapsed_s'))}  "
            f"tokens {a.get('prompt_tokens', 0)} in / {a.get('completion_tokens', 0)} out  memory share ≈ {gb(a.get('memory_share'))}",
            f"allowed tools: {', '.join(a.get('allowed_tools') or []) if a.get('allowed_tools') is not None else 'capability defaults'}  "
            "calls: " + (", ".join(t["tool"] + ("" if t["ok"] else "(failed)") for t in a.get("tool_uses") or []) or "none"),
        ]
        if a.get("error"):
            parts.append(f"[red]error ({a.get('error_kind')}):[/] {escape(str(a['error']))}")
        if a.get("artifact_id"):
            parts.append(f"artifact {a['artifact_id']}  (press a to open)")
        parts.append("")
        parts.append(f"[b]Instruction[/]\n{escape(str(a.get('instruction') or ''))}")
        if a.get("context"):
            parts.append(f"\n[b]Context artifacts given[/] ({len(a['context'])}, inputs {', '.join(a.get('input_artifact_ids') or [])})\n"
                         + escape(json.dumps(a["context"], indent=1, ensure_ascii=False)[:6000]))
        if a.get("context_text"):
            parts.append(f"\n[b]Additional context[/]\n{escape(str(a['context_text'])[:3000])}")
        if self.show_transcript and a.get("transcript"):
            parts.append("\n[b]Transcript[/] (t hides)")
            for m in a["transcript"]:
                role = m.get("role")
                if role == "system":
                    continue
                head = f"[cyan]{role}[/]" + (f" ({m.get('name')})" if m.get("name") else "") + (" [dim]structured[/]" if m.get("structured") else "")
                body = escape(str(m.get("content") or "")[:4000])
                if m.get("tool_calls"):
                    body += "\n" + escape(json.dumps(m["tool_calls"], ensure_ascii=False)[:2000])
                parts.append(f"{head}\n{body}\n")
        self.query_one("#body", Static).update("\n".join(parts))

    def action_artifact(self) -> None:
        if self.data.get("artifact_id"):
            self.app.open_artifact(self.data["artifact_id"])

    async def action_cancel_agent(self) -> None:
        ok = await self.app.call("cancel_agent", agent_id=self.agent_id)
        self.notify("cancel requested" if ok else "agent is not running")

    def action_toggle_transcript(self) -> None:
        self.show_transcript = not self.show_transcript
        self.render_body()


class ArtifactScreen(Screen):
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("p", "parent", "Parent"),
        Binding("j", "toggle_json", "Raw JSON"),
    ]

    def __init__(self, artifact_id: str) -> None:
        super().__init__()
        self.artifact_id = artifact_id
        self.data: dict[str, Any] = {}
        self.raw = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with VerticalScroll():
            yield Static(id="body")
        yield Footer()

    def on_mount(self) -> None:
        self.run_worker(self.reload())

    def update_from(self, snap: dict[str, Any]) -> None:
        return None

    async def reload(self) -> None:
        d = await self.app.call("artifact", artifact_id=self.artifact_id)
        if d:
            self.data = d
            self.render_body()
        else:
            self.query_one("#body", Static).update(f"artifact {self.artifact_id} not found")

    def render_body(self) -> None:
        a = self.data
        if self.raw:
            self.query_one("#body", Static).update(escape(json.dumps(a, indent=1, ensure_ascii=False)))
            return
        p = a.get("provenance") or {}
        m = a.get("model") or {}
        parts = [
            f"[b]{escape(a.get('title') or a['kind'])}[/]  {a['id']}  kind {a['kind']}  v{a.get('version')}  confidence {conf(a.get('confidence'))}",
            f"task {p.get('task_id')}  node {p.get('node_id')}  agent {p.get('agent_id') or '-'}  capability {p.get('capability')}  "
            f"attempt {p.get('attempt')}  created {p.get('created_at', '')[:19]}",
            f"model {escape(str(m.get('model') or '-'))} ({m.get('tier', '-')}, {m.get('prompt_tokens', 0)} in / {m.get('completion_tokens', 0)} out, {m.get('tokens_per_s', 0)} tok/s)  "
            f"time {dur(a.get('execution_time_s'))}  tools {', '.join(t['tool'] for t in a.get('tools') or []) or 'none'}",
            f"inputs {', '.join(p.get('inputs') or []) or '-'}   parents {', '.join(a.get('parents') or []) or '-'}   tags {', '.join(a.get('tags') or [])}",
            "",
            f"[b]Conclusion[/]\n{escape(a.get('conclusion') or '')}",
        ]
        if a.get("reasoning_summary"):
            parts.append(f"\n[b]Reasoning[/]\n{escape(a['reasoning_summary'])}")
        if a.get("evidence"):
            parts.append("\n[b]Evidence[/]")
            for e in a["evidence"]:
                mark = "[green]+[/]" if e.get("supports", True) else "[red]−[/]"
                src = f" [dim]({e.get('source_type')}: {escape(str(e.get('source')))}" + (f", retrieved {e['retrieved_at'][:19]}" if e.get("retrieved_at") else "") + ")[/]" if e.get("source") else f" [dim]({e.get('source_type')})[/]"
                parts.append(f" {mark} q{e.get('quality', 0):.1f} {escape(e.get('claim', ''))}{src}")
                if e.get("quote"):
                    parts.append(f"      [italic]{escape(e['quote'][:300])}[/]")
        if a.get("contradictions"):
            parts.append("\n[b]Contradictions[/]\n" + "\n".join(f" · {escape(c)}" for c in a["contradictions"]))
        if a.get("unresolved"):
            parts.append("\n[b]Unresolved[/]\n" + "\n".join(f" · {escape(u)}" for u in a["unresolved"]))
        if a.get("next_action"):
            parts.append(f"\n[b]Next action[/]\n{escape(a['next_action'])}")
        if a.get("content"):
            parts.append(f"\n[b]Content[/]\n{escape(a['content'])}")
        if a.get("data"):
            parts.append(f"\n[b]Data[/]\n{escape(json.dumps(a['data'], indent=1, ensure_ascii=False)[:4000])}")
        self.query_one("#body", Static).update("\n".join(parts))

    def action_parent(self) -> None:
        parents = self.data.get("parents") or (self.data.get("provenance") or {}).get("inputs") or []
        if parents:
            self.app.open_artifact(parents[0])
        else:
            self.notify("no parent artifact")

    def action_toggle_json(self) -> None:
        self.raw = not self.raw
        self.render_body()
