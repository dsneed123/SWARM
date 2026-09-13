"""Integrations with external services: generic HTTP APIs and GitHub via the
``gh`` CLI (which handles authentication itself, so no tokens live here)."""

from __future__ import annotations

import asyncio
import json
import shlex
import shutil
from typing import Any

import httpx

from swarm.core.types import now_iso
from swarm.tools.registry import Tool, ToolContext, ToolResult


class HttpRequestTool(Tool):
    name = "http_request"
    description = "Make an HTTP request to an API (GET/POST/PUT/DELETE) with optional JSON body and headers."
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"]},
            "headers": {"type": "object"},
            "json": {"type": "object"},
            "params": {"type": "object"},
        },
        "required": ["url"],
    }
    category = "network"
    side_effects = True
    timeout_s = 60.0

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        method = str(args.get("method") or "GET").upper()
        url = str(args.get("url", ""))
        if not url.startswith(("http://", "https://")):
            return ToolResult(ok=False, error="url must be http(s)")
        async with httpx.AsyncClient(timeout=45, follow_redirects=True) as client:
            r = await client.request(method, url, headers=args.get("headers") or None,
                                     json=args.get("json"), params=args.get("params") or None)
        body = r.text
        try:
            parsed = r.json()
            body = json.dumps(parsed, indent=1)[:15000]
        except ValueError:
            body = body[:15000]
        return ToolResult(ok=r.status_code < 400, output=f"HTTP {r.status_code}\n{body}",
                          data={"status": r.status_code, "url": str(r.url)},
                          error=None if r.status_code < 400 else f"HTTP {r.status_code}",
                          sources=[{"url": str(r.url), "retrieved_at": now_iso()}] if method == "GET" else [])


_GH_READ = {"repo view", "issue list", "issue view", "pr list", "pr view", "pr diff", "pr checks",
            "search repos", "search issues", "search prs", "search code", "release list", "release view",
            "run list", "run view", "api"}
_GH_WRITE = {"issue create", "issue comment", "issue close", "pr create", "pr comment", "pr review",
             "pr merge", "release create", "repo clone", "repo fork", "gist create"}
_GH_FORBIDDEN = ("auth", "repo delete", "secret", "ssh-key", "gpg-key", "config", "extension")


class GitHubTool(Tool):
    name = "github"
    description = ("Use GitHub through the gh CLI. Provide the arguments after 'gh', e.g. "
                   "'issue list -R owner/repo --limit 20' or 'api repos/owner/repo'. Read-only "
                   "commands are cheap; write commands (create/comment/merge) require permission.")
    parameters = {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}
    category = "external"
    side_effects = True
    timeout_s = 90.0

    def describe_call(self, args: dict[str, Any]) -> str:
        return f"gh {args.get('command', '')}"

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if not shutil.which("gh"):
            return ToolResult(ok=False, error="gh CLI is not installed")
        cmd = str(args.get("command", "")).strip()
        try:
            argv = shlex.split(cmd)
        except ValueError as e:
            return ToolResult(ok=False, error=f"bad command: {e}")
        if not argv:
            return ToolResult(ok=False, error="empty command")
        if argv[0] == "gh":
            argv = argv[1:]
        head = " ".join(argv[:2])
        if any(head.startswith(f) for f in _GH_FORBIDDEN):
            return ToolResult(ok=False, error=f"gh {head!r} is not available to agents")
        if head == "api" and any(a in ("-X", "--method") for a in argv) and "GET" not in argv:
            return ToolResult(ok=False, error="only GET requests are allowed through gh api")
        if head not in _GH_READ and head not in _GH_WRITE and argv[0] not in ("repo", "issue", "pr", "search", "release", "run"):
            return ToolResult(ok=False, error=f"gh {head!r} is not on the allowed list")
        proc = await asyncio.create_subprocess_exec(
            "gh", *argv, cwd=str(ctx.workspace.files), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        o, e = out.decode(errors="replace"), err.decode(errors="replace")
        return ToolResult(ok=proc.returncode == 0, output=(o or e)[:15000],
                          data={"returncode": proc.returncode},
                          error=None if proc.returncode == 0 else e.strip()[-500:],
                          sources=[{"url": f"github:{cmd}", "retrieved_at": now_iso()}] if head in _GH_READ else [])
