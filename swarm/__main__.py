"""Entry point.

    swarm                     the console (starts the background service if needed)
    swarm dashboard           optional full-screen view
    swarm ask "objective"     one objective, print the answer
    swarm run <workflow> "objective"
    swarm tasks | task <id> | status | models | workflows | permissions [profile]
    swarm stop                stop the background service
    swarm serve               run the service in the foreground (systemd)
"""

from __future__ import annotations

import argparse
import asyncio
import sys

ONE_SHOT = ("ask", "run", "tasks", "task", "status", "models", "workflows", "permissions")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="swarm", description="Local AI agent swarm for the GX10",
                                     epilog="Run with no arguments for the console.")
    parser.add_argument("--workspace", help="workspace directory (default: ./workspace or $SWARM_WORKSPACE)")
    parser.add_argument("--embedded", action="store_true", help="run the engine in this process instead of the service")
    parser.add_argument("--demo", action="store_true", help="fake model backend, no Ollama needed")
    parser.add_argument("command", nargs="?", help="ask, run, tasks, task, status, models, workflows, permissions, stop, serve, dashboard")
    parser.add_argument("args", nargs=argparse.REMAINDER)
    a = parser.parse_args(argv)

    if a.command == "serve":
        from swarm.service.daemon import main as serve_main

        serve_main(a.workspace)
    elif a.command == "stop":
        from swarm.config import load_settings
        from swarm.service.daemon import stop_service

        print("service stopped" if stop_service(load_settings(a.workspace).workspace) else "service was not running")
    elif a.command == "dashboard":
        from swarm.tui.app import run_tui

        run_tui(workspace=a.workspace, embedded=a.embedded, demo=a.demo)
    elif a.command in ONE_SHOT:
        from swarm.cli import one_shot

        if a.command in ("ask", "task") and not a.args or a.command == "run" and len(a.args) < 2:
            parser.error(f"{a.command} needs arguments")
        asyncio.run(one_shot(a.workspace, a.command, a.args, embedded=a.embedded, demo=a.demo))
    elif a.command is None:
        from swarm.console import console_main

        asyncio.run(console_main(a.workspace, embedded=a.embedded, demo=a.demo))
    else:
        parser.error(f"unknown command {a.command!r}")


if __name__ == "__main__":
    sys.exit(main())
