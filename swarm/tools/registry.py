"""Tool abstraction and the registry that enforces permissions.

Agents receive tool *names*; every invocation goes through
``ToolRegistry.invoke`` which resolves the policy for the call's scope, asks
the user when required, sandboxes paths, records provenance and timing, and
returns a ``ToolResult``. Agents cannot reach a tool object directly.
"""

from __future__ import annotations

import abc
import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from swarm.core.events import EventBus
from swarm.core.types import Policy, now_iso
from swarm.models.backend import ToolSpec
from swarm.paths import Workspace
from swarm.permissions.approvals import ApprovalBroker
from swarm.permissions.policy import PermissionResolver, PermissionScope

log = logging.getLogger(__name__)


@dataclass
class ToolContext:
    workspace: Workspace
    project_dir: Path | None = None  # the user's directory for this task; None = workspace/files
    task_id: str | None = None
    node_id: str | None = None
    agent_id: str | None = None
    scope: PermissionScope = field(default_factory=PermissionScope)
    settings: Any = None

    @property
    def files_root(self) -> Path:
        """Directory file and code tools are confined to."""
        root = self.project_dir or self.workspace.files
        root.mkdir(parents=True, exist_ok=True)
        return root

    def resolve(self, relative: str) -> Path:
        return self.workspace.resolve_inside(self.files_root, relative)


@dataclass
class ToolResult:
    ok: bool
    output: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    duration_s: float = 0.0
    sources: list[dict[str, Any]] = field(default_factory=list)  # {url/path, title, retrieved_at}

    def as_model_text(self, limit: int = 12000) -> str:
        text = self.output if self.ok else f"ERROR: {self.error}"
        return text if len(text) <= limit else text[:limit] + "\n…[truncated]"


class Tool(abc.ABC):
    name: str = ""
    description: str = ""
    parameters: dict[str, Any] = {"type": "object", "properties": {}}
    category: str = "read"  # read | write | execute | network | external
    side_effects: bool = False
    timeout_s: float = 120.0

    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description=self.description, parameters=self.parameters)

    @abc.abstractmethod
    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult: ...

    def describe_call(self, args: dict[str, Any]) -> str:
        return ", ".join(f"{k}={str(v)[:60]!r}" for k, v in args.items())


class ToolRegistry:
    def __init__(self, resolver: PermissionResolver, approvals: ApprovalBroker, bus: EventBus) -> None:
        self.resolver = resolver
        self.approvals = approvals
        self.bus = bus
        self._tools: dict[str, Tool] = {}
        self.stats: dict[str, dict[str, int]] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self, names: list[str] | None = None) -> list[ToolSpec]:
        names = names if names is not None else self.names()
        return [self._tools[n].spec() for n in names if n in self._tools]

    def catalog(self) -> list[dict[str, Any]]:
        table = self.resolver.table([(t.name, t.category) for t in self._tools.values()])
        return [
            {
                "name": t.name,
                "category": t.category,
                "side_effects": t.side_effects,
                "description": t.description,
                **table[t.name],
                "calls": self.stats.get(t.name, {}).get("calls", 0),
                "denied": self.stats.get(t.name, {}).get("denied", 0),
            }
            for t in sorted(self._tools.values(), key=lambda t: t.name)
        ]

    def policy_for(self, name: str, scope: PermissionScope | None = None) -> Policy:
        tool = self._tools.get(name)
        if tool is None:
            return Policy.DENY
        return self.resolver.resolve(name, tool.category, scope)

    def allowed_for(self, names: list[str], scope: PermissionScope | None = None) -> list[str]:
        """Tools that are not outright denied in this scope."""
        return [n for n in names if self.policy_for(n, scope) != Policy.DENY]

    async def invoke(self, name: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        tool = self._tools.get(name)
        st = self.stats.setdefault(name, {"calls": 0, "denied": 0, "failed": 0})
        st["calls"] += 1
        if tool is None:
            st["failed"] += 1
            return ToolResult(ok=False, error=f"unknown tool {name!r}")
        policy = self.resolver.resolve(name, tool.category, ctx.scope)
        if policy == Policy.DENY:
            st["denied"] += 1
            self.bus.publish("tool.denied", tool=name, task_id=ctx.task_id, agent_id=ctx.agent_id)
            return ToolResult(ok=False, error=f"tool {name!r} is not permitted by the current policy")
        if policy == Policy.ASK:
            self.bus.publish("tool.ask", tool=name, task_id=ctx.task_id, agent_id=ctx.agent_id)
            allowed = await self.approvals.request(
                name, args, task_id=ctx.task_id, agent_id=ctx.agent_id,
                reason=tool.describe_call(args),
            )
            if not allowed:
                st["denied"] += 1
                self.bus.publish("tool.denied", tool=name, task_id=ctx.task_id, agent_id=ctx.agent_id, by="user")
                return ToolResult(ok=False, error=f"user declined {name!r}")
        t0 = time.monotonic()
        self.bus.publish("tool.start", tool=name, task_id=ctx.task_id, agent_id=ctx.agent_id, args=_short(args))
        try:
            result = await asyncio.wait_for(tool.run(args, ctx), timeout=tool.timeout_s)
        except TimeoutError:
            result = ToolResult(ok=False, error=f"{name} timed out after {tool.timeout_s:.0f}s")
        except PermissionError as e:
            result = ToolResult(ok=False, error=f"permission: {e}")
        except Exception as e:  # noqa: BLE001 - tool bugs must not kill agents
            log.exception("tool %s crashed", name)
            result = ToolResult(ok=False, error=f"{type(e).__name__}: {e}")
        result.duration_s = time.monotonic() - t0
        for s in result.sources:
            s.setdefault("retrieved_at", now_iso())
        if not result.ok:
            st["failed"] += 1
        self.bus.publish("tool.end", tool=name, ok=result.ok, duration_s=round(result.duration_s, 2),
                         task_id=ctx.task_id, agent_id=ctx.agent_id, error=result.error)
        return result


def _short(args: dict[str, Any]) -> dict[str, str]:
    return {k: str(v)[:80] for k, v in args.items()}
