"""The persistent local service: builds the app and serves the socket API."""

from __future__ import annotations

import asyncio
import logging
import os
import signal

from swarm.app import build_app
from swarm.config import load_settings
from swarm.service.api import Api
from swarm.service.protocol import SocketServer

log = logging.getLogger(__name__)


async def serve(workspace: str | None = None) -> None:
    settings = load_settings(workspace)
    app = build_app(settings)
    api = Api(app)
    server = SocketServer(api, str(settings.socket_path))
    await app.start()
    await server.start()
    pidfile = settings.workspace / "swarm.pid"
    pidfile.write_text(str(os.getpid()))
    log.info("swarm service listening on %s (workspace %s)", settings.socket_path, settings.workspace)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    try:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=15.0)
            except TimeoutError:
                await app.ensure_backend()
    finally:
        log.info("swarm service shutting down")
        await server.stop()
        await app.stop()
        if pidfile.exists():
            pidfile.unlink()


def main(workspace: str | None = None) -> None:
    try:
        asyncio.run(serve(workspace))
    except KeyboardInterrupt:
        pass


def service_pid(workspace_dir) -> int | None:
    """PID of a running service for this workspace, or None."""
    from pathlib import Path

    pidfile = Path(workspace_dir) / "swarm.pid"
    if not pidfile.exists():
        return None
    try:
        pid = int(pidfile.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (ValueError, ProcessLookupError, PermissionError):
        return None


def stop_service(workspace_dir) -> bool:
    import time

    pid = service_pid(workspace_dir)
    if pid is None:
        return False
    os.kill(pid, signal.SIGTERM)
    for _ in range(50):
        if service_pid(workspace_dir) is None:
            return True
        time.sleep(0.2)
    return service_pid(workspace_dir) is None
