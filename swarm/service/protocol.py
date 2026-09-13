"""Newline-delimited JSON over a Unix socket.

Request:  {"id": 1, "method": "snapshot", "params": {...}}
Response: {"id": 1, "result": ...} or {"id": 1, "error": "..."}
Events:   {"event": {...}} pushed to clients that sent {"method": "subscribe"}.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from swarm.service.api import Api

log = logging.getLogger(__name__)


class SocketServer:
    def __init__(self, api: Api, path: str) -> None:
        self.api = api
        self.path = path
        self._server: asyncio.AbstractServer | None = None
        self._clients: set[asyncio.StreamWriter] = set()

    async def start(self) -> None:
        import os

        if os.path.exists(self.path):
            os.unlink(self.path)
        self._server = await asyncio.start_unix_server(self._handle, path=self.path)
        os.chmod(self.path, 0o600)
        self.api.app.bus.subscribe(self._broadcast)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        for w in list(self._clients):
            w.close()
        import os

        if os.path.exists(self.path):
            os.unlink(self.path)

    def _broadcast(self, event: dict[str, Any]) -> None:
        line = (json.dumps({"event": event}, default=str) + "\n").encode()
        for w in list(self._clients):
            try:
                w.write(line)
            except Exception:  # noqa: BLE001
                self._clients.discard(w)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                try:
                    req = json.loads(raw)
                except json.JSONDecodeError:
                    writer.write(b'{"error": "bad json"}\n')
                    continue
                rid = req.get("id")
                method = req.get("method")
                if method == "subscribe":
                    self._clients.add(writer)
                    writer.write((json.dumps({"id": rid, "result": True}) + "\n").encode())
                    continue
                try:
                    result = await self.api.call(method, req.get("params") or {})
                    payload = {"id": rid, "result": result}
                except Exception as e:  # noqa: BLE001
                    payload = {"id": rid, "error": f"{type(e).__name__}: {e}"}
                writer.write((json.dumps(payload, default=str) + "\n").encode())
                await writer.drain()
        except (ConnectionResetError, asyncio.IncompleteReadError, BrokenPipeError):
            pass
        finally:
            self._clients.discard(writer)
            writer.close()


class SocketClient:
    def __init__(self, path: str) -> None:
        self.path = path
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._next = 0
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=2000)
        self._pump: asyncio.Task | None = None

    async def connect(self, subscribe: bool = True) -> None:
        self._reader, self._writer = await asyncio.open_unix_connection(self.path)
        self._pump = asyncio.create_task(self._read_loop())
        if subscribe:
            await self.call("subscribe")

    async def close(self) -> None:
        if self._pump:
            self._pump.cancel()
        if self._writer:
            self._writer.close()

    async def _read_loop(self) -> None:
        assert self._reader
        try:
            while True:
                raw = await self._reader.readline()
                if not raw:
                    break
                msg = json.loads(raw)
                if "event" in msg:
                    try:
                        self.events.put_nowait(msg["event"])
                    except asyncio.QueueFull:
                        pass
                    continue
                fut = self._pending.pop(msg.get("id"), None)
                if fut and not fut.done():
                    if "error" in msg:
                        fut.set_exception(RuntimeError(msg["error"]))
                    else:
                        fut.set_result(msg.get("result"))
        except (asyncio.CancelledError, ConnectionResetError):
            pass
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("service disconnected"))

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        assert self._writer
        self._next += 1
        rid = self._next
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        self._writer.write((json.dumps({"id": rid, "method": method, "params": params or {}}) + "\n").encode())
        await self._writer.drain()
        return await fut


class LocalClient:
    """Same interface as SocketClient but calls the Api in-process (embedded mode)."""

    def __init__(self, api: Api) -> None:
        self.api = api
        self.events = api.app.bus.queue(maxsize=2000)

    async def connect(self, subscribe: bool = True) -> None:
        return None

    async def close(self) -> None:
        self.api.app.bus.release_queue(self.events)

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        return await self.api.call(method, params or {})
