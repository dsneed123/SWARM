"""Sandboxed file access. Paths are relative to ``workspace/files`` and
anything that resolves outside it is refused."""

from __future__ import annotations

from typing import Any

from swarm.core.types import now_iso
from swarm.tools.registry import Tool, ToolContext, ToolResult


class ReadFileTool(Tool):
    name = "read_file"
    description = "Read a text file from the swarm workspace. Path is relative to the workspace files directory."
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}, "max_chars": {"type": "integer"}},
        "required": ["path"],
    }
    category = "read"

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = ctx.workspace.resolve_inside(ctx.workspace.files, str(args["path"]))
        if not path.is_file():
            return ToolResult(ok=False, error=f"not a file: {args['path']}")
        limit = int(args.get("max_chars") or 20000)
        text = path.read_text(errors="replace")
        return ToolResult(
            ok=True,
            output=text[:limit] + ("\n…[truncated]" if len(text) > limit else ""),
            data={"path": str(args["path"]), "bytes": path.stat().st_size},
            sources=[{"path": str(args["path"]), "retrieved_at": now_iso()}],
        )


class WriteFileTool(Tool):
    name = "write_file"
    description = "Write a text file inside the swarm workspace (creates parent directories)."
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}, "content": {"type": "string"}, "append": {"type": "boolean"}},
        "required": ["path", "content"],
    }
    category = "write"
    side_effects = True

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = ctx.workspace.resolve_inside(ctx.workspace.files, str(args["path"]))
        path.parent.mkdir(parents=True, exist_ok=True)
        content = str(args.get("content", ""))
        if args.get("append"):
            with path.open("a") as f:
                f.write(content)
        else:
            path.write_text(content)
        return ToolResult(ok=True, output=f"wrote {len(content)} chars to {args['path']}", data={"path": str(args["path"])})


class ListFilesTool(Tool):
    name = "list_files"
    description = "List files in a workspace directory (relative path, default root)."
    parameters = {"type": "object", "properties": {"path": {"type": "string"}}}
    category = "read"

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        rel = str(args.get("path") or ".")
        path = ctx.workspace.resolve_inside(ctx.workspace.files, rel)
        if not path.is_dir():
            return ToolResult(ok=False, error=f"not a directory: {rel}")
        entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name))
        lines = [f"{'d' if p.is_dir() else 'f'} {p.relative_to(ctx.workspace.files)} {p.stat().st_size if p.is_file() else ''}" for p in entries[:500]]
        return ToolResult(ok=True, output="\n".join(lines) or "(empty)", data={"count": len(entries)})
