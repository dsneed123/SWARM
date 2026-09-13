"""Plain command-line interface.

`swarm` opens a prompt: type an objective, see a few progress lines, get the
answer with its sources. Slash commands cover everything else. One-shot
forms (`swarm ask "..."`, `swarm tasks`) do the same without the prompt.
No full-screen UI, no colours beyond bold and dim, works over ssh.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from typing import Any

from swarm.service.protocol import LocalClient, SocketClient

GB = 1024**3


def _tty() -> bool:
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def bold(s: str) -> str:
    return f"\033[1m{s}\033[0m" if _tty() else s


def dim(s: str) -> str:
    return f"\033[2m{s}\033[0m" if _tty() else s


def gb(n: int | float | None) -> str:
    return f"{(n or 0) / GB:.1f} GB"


def dur(seconds: float | None) -> str:
    s = int(seconds or 0)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    return f"{m}m{s:02d}s" if m < 60 else f"{m // 60}h{m % 60:02d}m"


def out(line: str = "") -> None:
    print(line, flush=True)


# --- connection --------------------------------------------------------------


async def connect(workspace: str | None = None, *, embedded: bool = False, demo: bool = False, quiet: bool = False):
    """Return (client, on_exit). Starts the background service when needed."""
    from swarm.tui.app import _connect  # shares the auto-start logic

    if quiet:
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            client, _label, on_exit = await _connect(workspace, embedded, demo)
    else:
        client, _label, on_exit = await _connect(workspace, embedded, demo)
    return client, on_exit


# --- running one objective -----------------------------------------------------


class Runner:
    """Submits an objective and prints progress from the event stream until it finishes."""

    def __init__(self, client: LocalClient | SocketClient, *, verbose: bool = False) -> None:
        self.client = client
        self.verbose = verbose

    async def run(self, objective: str, *, mode: str | None = None, workflow: str | None = None,
                  overrides: dict[str, Any] | None = None) -> dict[str, Any] | None:
        brief = await self.client.call("submit", {"objective": objective, "mode": mode, "workflow": workflow,
                                                  "overrides": overrides or {}})
        tid = brief["id"]
        out(dim(f"task {tid} queued"))
        t0 = time.time()
        node_models: dict[str, set[str]] = {}
        try:
            while True:
                try:
                    e = await asyncio.wait_for(self.client.events.get(), timeout=30)
                except TimeoutError:
                    snap = await self.client.call("task", {"task_id": tid})
                    if snap and snap["status"] in ("completed", "failed", "cancelled"):
                        break
                    out(dim(f"  … still working ({dur(time.time() - t0)})"))
                    continue
                if e.get("task_id") != tid and e.get("type") not in ("approval.requested", "model.loaded", "model.tier_fallback"):
                    continue
                await self._handle(e, tid, node_models)
                if e.get("type") == "task.finished":
                    break
        except KeyboardInterrupt:
            out("\ncancelling…")
            await self.client.call("cancel", {"task_id": tid})
            return None
        detail = await self.client.call("task", {"task_id": tid})
        self._print_result(detail, time.time() - t0)
        return detail

    async def _handle(self, e: dict[str, Any], tid: str, node_models: dict[str, set[str]]) -> None:
        t = e["type"]
        if t == "task.planned":
            nodes = ", ".join(n.replace(":", " ") for n in e.get("nodes", []))
            out(f"  plan ({e.get('objective_class')}): {nodes}")
        elif t == "task.started" and e.get("workflow", "").startswith("auto") is False and e.get("workflow"):
            out(f"  workflow: {e['workflow']}")
        elif t == "node.started":
            out(f"  {e['node_id']:<22} {e.get('capability', '')}")
        elif t == "agent.status" and e.get("status") == "running" and e.get("model"):
            pass
        elif t == "model.lease" and self.verbose:
            out(dim(f"    model {e['model']} for {e.get('purpose', '')}"))
        elif t == "model.loaded":
            out(dim(f"    loaded {e['model']} ({e.get('load_s')}s)"))
        elif t == "model.tier_fallback":
            out(dim(f"    {e['requested']} tier does not fit in memory ({e.get('usable_gb')} GB usable); using {e['used']}"))
        elif t == "node.consensus":
            st = e.get("status")
            label = {"converged": "agree", "single": "done", "weak": "weak", "disagreement": "disagree"}.get(st, st)
            out(f"  {e['node_id']:<22} {label} {e.get('confidence', 0):.2f}" + (f"  {dim(e.get('reason', ''))}" if st in ("weak", "disagreement") else ""))
        elif t == "node.more_agents":
            out(f"  {e['node_id']:<22} +{e['count']} more agents ({e.get('tier')})")
        elif t == "node.escalated":
            out(f"  {e['node_id']:<22} escalated to {e.get('tier')} arbiter")
        elif t == "node.recovery":
            out(f"  {e['node_id']:<22} attempt {e.get('attempt')} failed ({e.get('kind')}) → {e.get('action')}")
        elif t == "node.finished" and e.get("status") not in ("completed",):
            out(f"  {e['node_id']:<22} {e.get('status')}" + (f": {e.get('error')}" if e.get("error") else ""))
        elif t == "dag.mutated":
            out(dim(f"  plan changed: {e.get('op')} {e.get('node') or e.get('reason') or ''}"))
        elif t == "task.hurry":
            out(dim("  past the soft time limit: cutting optional work"))
        elif t == "task.question":
            answer = await self._ask(e.get("text", ""), e.get("options") or [])
            if answer:
                await self.client.call("answer", {"task_id": tid, "question_id": e["question_id"], "answer": answer})
        elif t == "approval.requested":
            decision = await self._approve(e)
            await self.client.call("approve", {"request_id": e["id"], "decision": decision})

    async def _ask(self, text: str, options: list[str]) -> str | None:
        out(bold(f"\n? {text}"))
        for i, o in enumerate(options, 1):
            out(f"  {i}. {o}")
        raw = (await asyncio.to_thread(input, "  answer (number or text): ")).strip()
        if raw.isdigit() and options and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1]
        return raw or None

    async def _approve(self, e: dict[str, Any]) -> str:
        args = ", ".join(f"{k}={str(v)[:80]!r}" for k, v in (e.get("arguments") or {}).items())
        out(bold(f"\n! {e['tool']} wants to run: {args}"))
        raw = (await asyncio.to_thread(input, "  allow? [y]es / [n]o / [t]his task / [x] never this task: ")).strip().lower()
        return {"y": "allow", "t": "allow_task", "x": "deny_task"}.get(raw[:1], "deny")

    def _print_result(self, detail: dict[str, Any] | None, elapsed: float) -> None:
        if not detail:
            return
        status = detail["status"]
        result = detail.get("result")
        out()
        if status == "completed" and result:
            text = result.get("content") or result.get("conclusion") or ""
            if result.get("conclusion") and result.get("content") and result["conclusion"] not in result["content"]:
                out(bold(result["conclusion"]))
                out()
            out(text.strip())
            if result.get("unresolved"):
                out()
                out("Open questions:")
                for u in result["unresolved"][:5]:
                    out(f"  - {u}")
            models = ", ".join(detail.get("models_used") or [])
            out()
            out(dim(f"confidence {result.get('confidence', 0):.2f} · {dur(elapsed)} · {len(detail.get('agents') or [])} agents · {models}"))
        elif status == "cancelled":
            out("cancelled")
        else:
            out(bold(f"failed: {detail.get('error')}"))


# --- slash commands and one-shot subcommands --------------------------------------


async def show_status(client) -> None:
    s = await client.call("snapshot")
    hw, sched, q = s["hardware"], s["scheduler"], s["queue"]
    b = sched["budget"]
    out(f"backend   {'ok' if s.get('backend_ok') else 'unreachable'} ({s['settings']['backend']} @ {s['settings']['ollama_host']})")
    out(f"memory    {gb(hw['mem_used'])} / {gb(hw['mem_total'])} used · swarm {gb(b['allocated'])} of {gb(b['target'])} target ({b['ceiling_percent']:.0f}%), {gb(b['usable'])} usable now")
    gpu = f"gpu {hw['gpu_util']:.0f}%" if hw.get("gpu_util") is not None else "gpu n/a"
    out(f"load      {gpu} · cpu {hw['cpu_percent']:.0f}% · {len(sched['instances'])} models loaded · {sched['active']} inferring · {sched['waiting']} waiting")
    out(f"tasks     {q['active']} active · {q['queued']} queued · permissions {s['permissions']['profile']} · mode {s['settings']['default_mode']}")


async def show_models(client) -> None:
    s = await client.call("snapshot")
    inst = {i["name"]: i for i in s["scheduler"]["instances"]}
    out(f"{'model':<26}{'tier':<10}{'size':<8}{'state':<10}{'memory':<10}{'tok/s':<7}{'calls':<7}fail")
    for m in s["scheduler"]["models"]:
        if m.get("disabled"):
            continue
        i = inst.get(m["name"])
        state = i["state"] if i else "-"
        mem = gb(i["memory"]) if i else "-"
        out(f"{m['name']:<26}{m['tier']:<10}{m['params_b']:>3.0f}B    {state:<10}{mem:<10}{m['tps'] or '-':<7}{m['calls']:<7}{m['failures']}")


async def show_tasks(client, limit: int = 15) -> None:
    s = await client.call("snapshot")
    if not s["tasks"]:
        out("no tasks yet")
        return
    for t in s["tasks"][:limit]:
        when = time.strftime("%m-%d %H:%M", time.localtime(t["created_at"]))
        out(f"{t['id']}  {when}  {t['status']:<10} {dur(t['elapsed_s']):>7}  {t['objective'][:70]}")


async def show_task(client, task_id: str) -> None:
    d = await client.call("task", {"task_id": task_id})
    if not d:
        out("no such task")
        return
    out(bold(d["objective"]))
    out(f"{d['status']} · {dur(d['elapsed_s'])} · class {d.get('objective_class') or '-'} · workflow {d.get('workflow') or 'auto'}")
    for n in (d.get("dag") or {}).get("nodes", []):
        st = n.get("state") or {}
        cs = st.get("consensus") or {}
        extra = f"{cs.get('status')} {cs.get('confidence', 0):.2f}" if cs else ""
        out(f"  {n['id']:<22} {n['capability']:<13} {st.get('status', '-'):<10} {extra}")
        for note in st.get("notes") or []:
            out(dim(f"      {note}"))
    if d.get("result"):
        out()
        out((d["result"].get("content") or d["result"].get("conclusion") or "").strip())


async def show_workflows(client) -> None:
    for w in await client.call("workflows"):
        out(f"{w['slug']:<22} {w.get('nodes', '?'):>2} nodes  {w.get('description') or w.get('error') or ''}")


async def show_permissions(client) -> None:
    s = await client.call("snapshot")
    out(f"profile {s['permissions']['profile']}")
    for t in await client.call("tools"):
        src = "" if t["source"] != "override" else " (override)"
        out(f"  {t['name']:<14}{t['policy']:<7}{src}")


HELP = """commands
  <objective>            run it and print the answer
  /tasks                 recent tasks          /task <id>      details of one task
  /status                hardware and queue    /models         installed models and their state
  /memory <percent>      set the swarm memory ceiling
  /permissions [safe|normal|autonomous]   show or set the profile
  /allow|/ask|/deny <tool>                per-tool override
  /workflows             saved workflows       /run <slug> <objective>
  /mode [autonomous|interactive]          default execution mode
  /cancel <id>  /pause <id>  /resume <id>
  /quit                  leave (the service keeps running; `swarm stop` stops it)
Ctrl+C cancels the running objective."""


async def handle_command(client, line: str, runner: Runner) -> bool:
    """Returns False when the prompt should exit."""
    parts = line.split()
    cmd, args = parts[0].lower(), parts[1:]
    if cmd in ("/quit", "/exit", "/q"):
        return False
    if cmd in ("/help", "/?"):
        out(HELP)
    elif cmd == "/tasks":
        await show_tasks(client)
    elif cmd == "/task" and args:
        await show_task(client, args[0])
    elif cmd == "/status":
        await show_status(client)
    elif cmd == "/models":
        await show_models(client)
    elif cmd == "/memory" and args:
        s = await client.call("settings", {"changes": {"memory_ceiling_percent": float(args[0])}})
        out(f"memory ceiling {s['memory_ceiling_percent']:.0f}%")
    elif cmd == "/permissions":
        if args:
            await client.call("settings", {"changes": {"permission_profile": args[0]}})
        await show_permissions(client)
    elif cmd in ("/allow", "/ask", "/deny") and args:
        await client.call("settings", {"changes": {"tool_overrides": {args[0]: cmd[1:]}}})
        out(f"{args[0]}: {cmd[1:]}")
    elif cmd == "/workflows":
        await show_workflows(client)
    elif cmd == "/run" and len(args) >= 2:
        await runner.run(" ".join(args[1:]), workflow=args[0])
    elif cmd == "/mode":
        if args:
            await client.call("settings", {"changes": {"default_mode": args[0]}})
        s = await client.call("snapshot")
        out(f"mode {s['settings']['default_mode']}")
    elif cmd in ("/cancel", "/pause", "/resume") and args:
        ok = await client.call(cmd[1:], {"task_id": args[0]})
        out("ok" if ok else "nothing to do")
    else:
        out("unknown command; /help lists them")
    return True


async def prompt_loop(workspace: str | None, *, embedded: bool, demo: bool) -> None:
    client, on_exit = await connect(workspace, embedded=embedded, demo=demo)
    runner = Runner(client)
    try:
        s = await client.call("snapshot")
        n = len([m for m in s["scheduler"]["models"] if not m.get("disabled")])
        out(dim(f"swarm ready · {n} models · backend {'ok' if s.get('backend_ok') else 'unreachable'} · /help for commands"))
        while True:
            try:
                line = (await asyncio.to_thread(input, "> ")).strip()
            except (EOFError, KeyboardInterrupt):
                out()
                break
            if not line:
                continue
            if line.startswith("/"):
                if not await handle_command(client, line, runner):
                    break
                continue
            await runner.run(line)
    finally:
        await client.close()
        if on_exit:
            await on_exit()


async def one_shot(workspace: str | None, command: str, args: list[str], *, embedded: bool = False, demo: bool = False) -> None:
    client, on_exit = await connect(workspace, embedded=embedded, demo=demo, quiet=True)
    try:
        if command == "ask":
            await Runner(client).run(" ".join(args))
        elif command == "run":
            await Runner(client).run(" ".join(args[1:]), workflow=args[0])
        elif command == "tasks":
            await show_tasks(client)
        elif command == "task":
            await show_task(client, args[0])
        elif command == "status":
            await show_status(client)
        elif command == "models":
            await show_models(client)
        elif command == "workflows":
            await show_workflows(client)
        elif command == "permissions":
            if args:
                await client.call("settings", {"changes": {"permission_profile": args[0]}})
            await show_permissions(client)
    finally:
        await client.close()
        if on_exit:
            await on_exit()
