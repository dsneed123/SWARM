"""Code execution tools. Both run as subprocesses inside the workspace files
directory with a timeout and a reduced environment."""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

from swarm.tools.registry import Tool, ToolContext, ToolResult

_SAFE_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "PYTHONIOENCODING")


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in _SAFE_ENV_KEYS}
    env["PYTHONIOENCODING"] = "utf-8"
    return env


async def _run_subprocess(argv: list[str], cwd: str, timeout: float, stdin: str | None = None) -> ToolResult:
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=cwd, env=_env(), stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin.encode() if stdin is not None else None), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return ToolResult(ok=False, error=f"timed out after {timeout:.0f}s")
    o, e = out.decode(errors="replace"), err.decode(errors="replace")
    text = o + (("\n[stderr]\n" + e) if e.strip() else "")
    return ToolResult(ok=proc.returncode == 0, output=text[:20000], data={"returncode": proc.returncode},
                      error=None if proc.returncode == 0 else f"exit {proc.returncode}: {e.strip()[-500:] or o[-500:]}")


class PythonTool(Tool):
    name = "python"
    description = ("Run a Python script (Python 3, standard library plus whatever is installed) inside the workspace. "
                   "Print results to stdout. Use it for calculations, data processing and file analysis.")
    parameters = {
        "type": "object",
        "properties": {"code": {"type": "string"}, "timeout_s": {"type": "integer"}},
        "required": ["code"],
    }
    category = "execute"
    side_effects = True
    timeout_s = 300.0

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        code = str(args.get("code", ""))
        if not code.strip():
            return ToolResult(ok=False, error="empty code")
        timeout = min(float(args.get("timeout_s") or 120), self.timeout_s - 5)
        ctx.workspace.files.mkdir(parents=True, exist_ok=True)
        return await _run_subprocess([sys.executable, "-I", "-"], str(ctx.workspace.files), timeout, stdin=code)


class ShellTool(Tool):
    name = "shell"
    description = "Run a shell command inside the workspace directory. Denied or approval-gated by default."
    parameters = {
        "type": "object",
        "properties": {"command": {"type": "string"}, "timeout_s": {"type": "integer"}},
        "required": ["command"],
    }
    category = "execute"
    side_effects = True
    timeout_s = 300.0

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        cmd = str(args.get("command", ""))
        if not cmd.strip():
            return ToolResult(ok=False, error="empty command")
        timeout = min(float(args.get("timeout_s") or 120), self.timeout_s - 5)
        ctx.workspace.files.mkdir(parents=True, exist_ok=True)
        return await _run_subprocess(["bash", "-lc", cmd], str(ctx.workspace.files), timeout)
