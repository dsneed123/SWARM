"""The persistent local service: builds the app and serves the socket API."""

from __future__ import annotations

import asyncio
import logging
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


def main(workspace: str | None = None) -> None:
    try:
        asyncio.run(serve(workspace))
    except KeyboardInterrupt:
        pass
