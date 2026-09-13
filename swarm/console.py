"""The swarm console: a command prompt in the style of msfconsole.

    swarm > help
    swarm > what is the tallest mountain in europe
    swarm > use verified-research
    swarm (verified-research) > run compare X and Y
    swarm > show models
    swarm > set memory 60

Plain text, readline history and tab completion, numbered menus where a
choice is needed. Anything typed that is not a command is an objective.
"""

from __future__ import annotations

import asyncio
import os
import shlex
from typing import Any

from swarm.cli import (
    Runner,
    connect,
    dim,
    out,
    show_models,
    show_permissions,
    show_status,
    show_task,
    show_tasks,
    show_workflows,
)

BANNER = """
  swarm  ·  local agent swarm on the GX10
"""

HELP = """
Core
    help                      this list
    <text>                    run an objective (anything that is not a command)
    ask <text>                same, explicitly
    use <workflow|number>     select a saved workflow; run <text> uses it; back clears it
    run <text>                run an objective with the selected workflow
    show tasks|models|workflows|permissions|status|task <id>|events
    set memory <percent>      swarm memory ceiling
    set mode <autonomous|interactive>
    set permissions <safe|normal|autonomous>
    set tool <name> <allow|ask|deny>
    cancel|pause|resume <task id>
    approvals                 answer pending tool approvals
    answer <task id> <text>   answer a pending question
    cd <dir>                  change the working directory agents get for file tools
    stop                      stop the background service
    dashboard                 open the full-screen view
    exit
"""

SHOW = ("tasks", "models", "workflows", "permissions", "status", "task", "events")
SET = ("memory", "mode", "permissions", "tool")
COMMANDS = ("help", "ask", "use", "back", "run", "show", "set", "cancel", "pause", "resume", "approvals", "answer",
            "cd", "stop", "dashboard", "exit", "quit")


class Console:
    def __init__(self, client, on_exit, workspace_label: str) -> None:
        self.client = client
        self.on_exit = on_exit
        self.runner = Runner(client)
        self.workflow: str | None = None
        self.cwd = os.getcwd()
        self.workflows: list[dict[str, Any]] = []
        self.workspace_label = workspace_label

    # --- readline ---------------------------------------------------------------

    def _setup_readline(self) -> None:
        try:
            import readline
        except ImportError:
            return
        histfile = os.path.expanduser("~/.swarm/console_history")
        try:
            os.makedirs(os.path.dirname(histfile), exist_ok=True)
            readline.read_history_file(histfile)
        except OSError:
            pass
        readline.set_history_length(500)
        import atexit

        atexit.register(lambda: _save_history(readline, histfile))

        def complete(text: str, state: int) -> str | None:
            buf = readline.get_line_buffer()
            words = buf.split()
            if len(words) <= 1 and not buf.endswith(" "):
                options = [c for c in COMMANDS if c.startswith(text)]
            elif words[0] == "show":
                options = [c for c in SHOW if c.startswith(text)]
            elif words[0] == "set":
                if len(words) == 1 or (len(words) == 2 and not buf.endswith(" ")):
                    options = [c for c in SET if c.startswith(text)]
                elif words[1] == "mode":
                    options = [c for c in ("autonomous", "interactive") if c.startswith(text)]
                elif words[1] == "permissions":
                    options = [c for c in ("safe", "normal", "autonomous") if c.startswith(text)]
                elif words[1] == "tool" and len(words) >= 3 and (len(words) > 3 or buf.endswith(" ")):
                    options = [c for c in ("allow", "ask", "deny") if c.startswith(text)]
                else:
                    options = []
            elif words[0] == "use":
                options = [w["slug"] for w in self.workflows if w["slug"].startswith(text)]
            else:
                options = []
            return options[state] if state < len(options) else None

        readline.set_completer(complete)
        readline.set_completer_delims(" ")
        readline.parse_and_bind("tab: complete")

    @property
    def prompt(self) -> str:
        return f"swarm ({self.workflow}) > " if self.workflow else "swarm > "

    # --- loop -------------------------------------------------------------------

    async def run(self) -> None:
        self._setup_readline()
        s = await self.client.call("snapshot")
        n = len([m for m in s["scheduler"]["models"] if not m.get("disabled")])
        self.workflows = await self.client.call("workflows") or []
        out(BANNER)
        out(f"  {n} models · backend {'ok' if s.get('backend_ok') else 'unreachable'} · "
            f"{s['scheduler']['budget']['usable'] / 1024**3:.0f} GB usable · permissions {s['permissions']['profile']} · {self.workspace_label}")
        out(f"  working directory {self.cwd}")
        out(dim("  type an objective, or 'help' for commands\n"))
        try:
            while True:
                try:
                    line = (await asyncio.to_thread(input, self.prompt)).strip()
                except EOFError:
                    out()
                    break
                except KeyboardInterrupt:
                    out()
                    continue
                if not line:
                    continue
                if not await self.dispatch(line):
                    break
        finally:
            await self.client.close()
            if self.on_exit:
                await self.on_exit()

    async def dispatch(self, line: str) -> bool:
        """Handle one line. Returns False to exit."""
        try:
            words = shlex.split(line)
        except ValueError:
            words = line.split()
        cmd, args = words[0].lower(), words[1:]
        rest = line[len(words[0]):].strip() if words else ""
        if cmd not in COMMANDS:
            await self._objective(line, self.workflow)
            return True
        if cmd in ("exit", "quit"):
            return False
        if cmd == "help":
            out(HELP)
        elif cmd == "ask":
            await self._objective(rest, None)
        elif cmd == "run":
            await self._objective(rest, self.workflow)
        elif cmd == "use":
            await self._use(args)
        elif cmd == "back":
            self.workflow = None
        elif cmd == "show":
            await self._show(args)
        elif cmd == "set":
            await self._set(args)
        elif cmd in ("cancel", "pause", "resume"):
            if not args:
                out(f"usage: {cmd} <task id>")
            else:
                ok = await self.client.call(cmd, {"task_id": args[0]})
                out("ok" if ok else "nothing to do")
        elif cmd == "approvals":
            await self._approvals()
        elif cmd == "answer":
            if len(args) < 2:
                out("usage: answer <task id> <text>")
            else:
                await self._answer(args[0], " ".join(args[1:]))
        elif cmd == "cd":
            target = os.path.expanduser(args[0]) if args else os.path.expanduser("~")
            if os.path.isdir(target):
                self.cwd = os.path.abspath(target)
                out(self.cwd)
            else:
                out("no such directory")
        elif cmd == "stop":
            from swarm.config import load_settings
            from swarm.service.daemon import stop_service

            out("service stopped" if stop_service(load_settings().workspace) else "service was not running")
            return False
        elif cmd == "dashboard":
            out("run `swarm dashboard` in another terminal for the full-screen view")
        return True

    # --- handlers ----------------------------------------------------------------

    async def _objective(self, text: str, workflow: str | None) -> None:
        if not text.strip():
            out("nothing to run")
            return
        await self.runner.run(text, workflow=workflow, cwd=self.cwd)

    async def _use(self, args: list[str]) -> None:
        self.workflows = await self.client.call("workflows") or []
        if not args:
            for i, w in enumerate(self.workflows, 1):
                out(f"  {i:>2}  {w['slug']:<22} {w.get('description') or ''}")
            choice = (await asyncio.to_thread(input, "  workflow number (blank to cancel): ")).strip()
            if not choice:
                return
            args = [choice]
        pick = args[0]
        if pick.isdigit() and 1 <= int(pick) <= len(self.workflows):
            self.workflow = self.workflows[int(pick) - 1]["slug"]
        elif any(w["slug"] == pick for w in self.workflows):
            self.workflow = pick
        else:
            out("unknown workflow; `use` alone lists them")
            return
        out(f"using {self.workflow}; `run <objective>` to start, `back` to clear")

    async def _show(self, args: list[str]) -> None:
        what = args[0] if args else "status"
        if what == "tasks":
            await show_tasks(self.client)
        elif what == "models":
            await show_models(self.client)
        elif what == "workflows":
            await show_workflows(self.client)
        elif what == "permissions":
            await show_permissions(self.client)
        elif what == "status":
            await show_status(self.client)
        elif what == "task" and len(args) > 1:
            await show_task(self.client, args[1])
        elif what == "events":
            for e in await self.client.call("events", {"limit": 30}):
                out(f"  {e.get('ts', '')[11:19]}  {e.get('type')}  {e.get('task_id', '') or ''}  {e.get('node_id', '') or ''}")
        else:
            out(f"show what? {', '.join(SHOW)}")

    async def _set(self, args: list[str]) -> None:
        if len(args) < 2:
            out(f"set what? {', '.join(SET)}")
            return
        key, value = args[0], args[1]
        if key == "memory":
            s = await self.client.call("settings", {"changes": {"memory_ceiling_percent": float(value)}})
            out(f"memory ceiling {s['memory_ceiling_percent']:.0f}%")
        elif key == "mode":
            s = await self.client.call("settings", {"changes": {"default_mode": value}})
            out(f"mode {s['default_mode']}")
        elif key == "permissions":
            await self.client.call("settings", {"changes": {"permission_profile": value}})
            out(f"permissions {value}")
        elif key == "tool" and len(args) >= 3:
            await self.client.call("settings", {"changes": {"tool_overrides": {value: args[2]}}})
            out(f"{value}: {args[2]}")
        else:
            out(f"set what? {', '.join(SET)}")

    async def _approvals(self) -> None:
        snap = await self.client.call("snapshot")
        pending = snap.get("approvals") or []
        if not pending:
            out("no pending approvals")
            return
        for req in pending:
            decision = await self.runner._approve(req)
            await self.client.call("approve", {"request_id": req["id"], "decision": decision})

    async def _answer(self, task_id: str, text: str) -> None:
        d = await self.client.call("task", {"task_id": task_id})
        q = (d or {}).get("pending_question")
        if not q:
            out("that task has no pending question")
            return
        ok = await self.client.call("answer", {"task_id": task_id, "question_id": q["id"], "answer": text})
        out("answered" if ok else "could not answer")


def _save_history(readline, histfile: str) -> None:
    try:
        readline.write_history_file(histfile)
    except OSError:
        pass


async def console_main(workspace: str | None, *, embedded: bool = False, demo: bool = False) -> None:
    from swarm.config import load_settings

    settings = load_settings(workspace)
    client, on_exit = await connect(workspace, embedded=embedded, demo=demo)
    label = "service" if on_exit is None else ("demo" if demo else "embedded")
    await Console(client, on_exit, f"{label} · {settings.workspace}").run()
