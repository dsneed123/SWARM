"""Drives the console non-interactively against the fake backend."""

from __future__ import annotations

import io
from contextlib import redirect_stdout

from swarm.app import build_app
from swarm.config import Settings
from swarm.console import Console
from swarm.models.fake import FakeBackend, small_models
from swarm.service.api import Api
from swarm.service.protocol import LocalClient
from tests.helpers import structured


def responder(model, messages, tools, schema):
    return structured("forty-two", 0.9, content="The answer is forty-two.")


async def test_console_commands_and_objective(tmp_path):
    settings = Settings(workspace=tmp_path / "ws")
    app = build_app(settings, backend=FakeBackend(small_models(), responder=responder))
    await app.start()
    client = LocalClient(Api(app))
    con = Console(client, None, "test")
    con.cwd = str(tmp_path)

    async def run(line: str) -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            keep = await con.dispatch(line)
        assert keep is not False or line in ("exit", "quit")
        return buf.getvalue()

    assert "Core" in await run("help")
    assert "backend" in await run("show status")
    assert "tiny:1b" in await run("show models")
    assert "verified-research" in await run("show workflows")
    assert "memory ceiling 55%" in await run("set memory 55")
    assert app.settings.hardware.memory_ceiling_percent == 55
    await run("set permissions safe")
    assert app.tools.resolver.profile == "safe"
    await run("set tool github allow")
    assert app.tools.resolver.overrides["github"].value == "allow"
    assert "using verified-research" in await run("use verified-research")
    assert con.prompt == "swarm (verified-research) > "
    await run("back")
    assert con.prompt == "swarm > "
    text = await run("what is six times seven")
    assert "forty-two" in text and "confidence" in text
    tasks = await run("show tasks")
    assert "completed" in tasks and "six times seven" in tasks
    assert app.orchestrator.tasks.recent()[0].cwd == str(tmp_path)
    assert "unknown workflow" in await run("use nope")
    assert await con.dispatch("exit") is False
    await client.close()
    await app.stop()
