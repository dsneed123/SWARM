"""Entry point.

    swarm            open the terminal control center (connects to the service,
                     or runs the engine in-process when no service is running)
    swarm serve      run the persistent service
    swarm --help
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="swarm", description="Local AI agent swarm for the GX10")
    parser.add_argument("--workspace", help="workspace directory (default: ./workspace or $SWARM_WORKSPACE)")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the persistent local service")
    tui = sub.add_parser("tui", help="open the terminal control center (default)")
    tui.add_argument("--embedded", action="store_true", help="run the engine in-process even if a service is running")
    tui.add_argument("--demo", action="store_true", help="use a fake model backend (no Ollama needed)")
    args = parser.parse_args(argv)

    if args.command == "serve":
        from swarm.service.daemon import main as serve_main

        serve_main(args.workspace)
        return
    from swarm.tui.app import run_tui

    run_tui(workspace=args.workspace, embedded=getattr(args, "embedded", False), demo=getattr(args, "demo", False))


if __name__ == "__main__":
    sys.exit(main())
