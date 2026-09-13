"""Reusable panels: hardware summary, event log."""

from __future__ import annotations

from collections import deque
from typing import Any

from rich.markup import escape
from textual.widgets import RichLog, Static

from swarm.tui.format import gb, pct


class HardwarePanel(Static):
    """Two plain lines: machine state and swarm state."""

    def update_from(self, snap: dict[str, Any]) -> None:
        hw = snap.get("hardware") or {}
        sched = snap.get("scheduler") or {}
        budget = sched.get("budget") or {}
        q = snap.get("queue") or {}
        total = hw.get("mem_total") or 1
        used = hw.get("mem_used") or 0
        gpu = f"gpu {pct(hw.get('gpu_util'))}" if hw.get("gpu_util") is not None else "gpu n/a"
        temp = f" {hw.get('gpu_temp_c'):.0f}°C" if hw.get("gpu_temp_c") is not None else ""
        backend = "backend ok" if snap.get("backend_ok") else "[red]backend unreachable[/]"
        line1 = (f"memory {gb(used)} / {gb(total)} ({100 * used / total:.0f}%)   {gpu}{temp}   cpu {pct(hw.get('cpu_percent'))}   {backend}")
        line2 = (f"swarm  {gb(budget.get('allocated'))} of {gb(budget.get('target'))} target ({budget.get('ceiling_percent', 0):.0f}%), "
                 f"{gb(budget.get('usable'))} usable   {len(sched.get('instances') or [])} models loaded, {sched.get('active', 0)} inferring, "
                 f"{sched.get('waiting', 0)} waiting   tasks {q.get('active', 0)} active, {q.get('queued', 0)} queued")
        self.update(line1 + "\n" + line2)


INTERESTING = {
    "task.submitted", "task.planned", "task.started", "task.finished", "task.question", "task.hurry", "task.cancelled",
    "task.paused", "task.resumed", "node.started", "node.finished", "node.consensus", "node.more_agents",
    "node.escalated", "node.recovery", "node.skipped", "dag.mutated", "model.loaded", "model.unloaded",
    "model.load_failed", "model.tier_fallback", "approval.requested", "approval.decided", "tool.denied",
    "backend.health", "settings.changed", "settings.ceiling", "orchestrator.interrupted_tasks", "task.hard_timeout",
}


def describe_event(e: dict[str, Any]) -> str | None:
    t = e.get("type", "")
    if t not in INTERESTING:
        return None
    ts = e.get("ts", "")[11:19]
    tid = e.get("task_id", "")
    tid_s = f"[dim]{tid[-8:]}[/] " if tid else ""
    body = {
        "task.submitted": lambda: f"submitted: {escape(str(e.get('objective', ''))[:70])}",
        "task.planned": lambda: f"planned ({e.get('source')}, {e.get('objective_class')}): {escape(', '.join(e.get('nodes', [])))}",
        "task.started": lambda: f"started {e.get('nodes')} nodes ({'dynamic' if e.get('mutable') else 'fixed'} DAG)",
        "task.finished": lambda: f"[b]{e.get('status')}[/] after {e.get('elapsed_s')}s" + (f": {escape(str(e.get('error'))[:80])}" if e.get("error") else ""),
        "task.question": lambda: f"[bold yellow]question for you:[/] {escape(str(e.get('text'))[:80])}",
        "task.hurry": lambda: "past soft timeout: cutting optional work",
        "node.started": lambda: f"node {e.get('node_id')} ({e.get('capability')}) started",
        "node.finished": lambda: f"node {e.get('node_id')} {e.get('status')} in {e.get('elapsed_s')}s",
        "node.consensus": lambda: f"node {e.get('node_id')} consensus r{e.get('round')}: {e.get('status')} ({e.get('positions')} positions, conf {e.get('confidence')}) {escape(str(e.get('reason')))}",
        "node.more_agents": lambda: f"node {e.get('node_id')}: +{e.get('count')} agents on {e.get('tier')} ({escape(str(e.get('reason')))})",
        "node.escalated": lambda: f"node {e.get('node_id')}: escalated to {e.get('tier')} arbiter",
        "node.recovery": lambda: f"node {e.get('node_id')} attempt {e.get('attempt')} failed ({e.get('kind')}) -> {e.get('action')}: {escape(str(e.get('note')))}",
        "node.skipped": lambda: f"node {e.get('node_id')} skipped: {e.get('reason')}",
        "dag.mutated": lambda: f"DAG changed: {e.get('op')} {escape(str(e.get('node') or e.get('reason') or ''))}",
        "model.loaded": lambda: f"model {e.get('model')} loaded in {e.get('load_s')}s",
        "model.unloaded": lambda: f"model {e.get('model')} unloaded ({e.get('reason')})",
        "model.load_failed": lambda: f"[red]model {e.get('model')} failed to load: {escape(str(e.get('error'))[:60])}[/]",
        "model.tier_fallback": lambda: f"[yellow]{e.get('requested')} tier does not fit ({e.get('usable_gb')} GB usable); using {e.get('used')}[/]",
        "approval.requested": lambda: f"[bold yellow]approval needed:[/] {e.get('tool')} {escape(str(e.get('reason'))[:60])}",
        "approval.decided": lambda: f"approval {e.get('decision')}: {e.get('tool')}",
        "tool.denied": lambda: f"tool denied: {e.get('tool')}",
        "backend.health": lambda: f"backend {e.get('backend')}: {'ok' if e.get('healthy') else '[red]unreachable[/]'}",
        "settings.changed": lambda: f"settings changed: {escape(str(e.get('changes')))}",
        "settings.ceiling": lambda: f"memory ceiling set to {e.get('percent')}%",
        "orchestrator.interrupted_tasks": lambda: f"marked interrupted tasks failed: {len(e.get('tasks', []))}",
        "task.hard_timeout": lambda: "[red]hard timeout: cancelling task[/]",
        "task.cancelled": lambda: "cancelled",
        "task.paused": lambda: "paused",
        "task.resumed": lambda: "resumed",
    }.get(t, lambda: t)()
    return f"[dim]{ts}[/] {tid_s}{body}"


class EventLog(RichLog):
    def __init__(self, **kw: Any) -> None:
        super().__init__(markup=True, wrap=True, max_lines=500, **kw)
        self.seen: deque[str] = deque(maxlen=2000)

    def add_event(self, e: dict[str, Any]) -> None:
        key = f"{e.get('ts')}|{e.get('type')}|{e.get('task_id')}|{e.get('node_id')}|{e.get('id')}"
        if key in self.seen:
            return
        self.seen.append(key)
        line = describe_event(e)
        if line:
            self.write(line)
