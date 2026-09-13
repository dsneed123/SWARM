from __future__ import annotations

import asyncio

from swarm.app import build_app
from swarm.config import Settings
from swarm.models.fake import FakeBackend, small_models
from swarm.service.api import Api
from swarm.service.protocol import LocalClient, SocketClient, SocketServer
from tests.helpers import structured


def responder(model, messages, tools, schema):
    return structured("ok", 0.8)


async def test_socket_roundtrip_and_events(tmp_path):
    settings = Settings(workspace=tmp_path / "ws")
    settings.service.socket = str(tmp_path / "s.sock")
    app = build_app(settings, backend=FakeBackend(small_models(), responder=responder))
    api = Api(app)
    server = SocketServer(api, str(settings.socket_path))
    await app.start()
    await server.start()
    client = SocketClient(str(settings.socket_path))
    await client.connect()
    assert await client.call("ping") == "pong"
    snap = await client.call("snapshot")
    assert "scheduler" in snap and snap["backend_ok"]
    tools = await client.call("tools")
    assert any(t["name"] == "web_search" for t in tools)
    wfs = await client.call("workflows")
    assert any(w["slug"] == "verified-research" for w in wfs)
    await client.call("settings", {"changes": {"memory_ceiling_percent": 55}})
    assert (await client.call("snapshot"))["settings"]["memory_ceiling_percent"] == 55
    assert (tmp_path / "ws" / "config.yaml").exists()
    brief = await client.call("submit", {"objective": "hello", "mode": "autonomous"})
    got = None
    for _ in range(200):
        ev = await asyncio.wait_for(client.events.get(), timeout=5)
        if ev["type"] == "task.finished" and ev["task_id"] == brief["id"]:
            got = ev
            break
    assert got and got["status"] == "completed"
    detail = await client.call("task", {"task_id": brief["id"]})
    assert detail["result"]["kind"] == "final"
    try:
        await client.call("nope")
    except RuntimeError as e:
        assert "unknown method" in str(e)
    await client.close()
    await server.stop()
    await app.stop()


async def test_local_client(tmp_path):
    settings = Settings(workspace=tmp_path / "ws")
    app = build_app(settings, backend=FakeBackend(small_models(), responder=responder))
    await app.start()
    client = LocalClient(Api(app))
    assert await client.call("ping") == "pong"
    caps = await client.call("capabilities")
    assert any(c["name"] == "research" for c in caps)
    await client.close()
    await app.stop()
