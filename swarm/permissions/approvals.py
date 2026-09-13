"""Ask-policy approvals.

When a tool resolves to Ask, the registry files an ``ApprovalRequest`` here
and waits. The TUI (or any client) answers it. Unanswered requests time out
as denied. Decisions can be remembered for the rest of a task so the user is
not asked the same thing twenty times by twenty agents.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from swarm.core.events import EventBus
from swarm.core.types import new_id


@dataclass
class ApprovalRequest:
    id: str
    tool: str
    arguments: dict[str, Any]
    task_id: str | None
    agent_id: str | None
    reason: str
    created_at: float = field(default_factory=time.time)
    decision: str | None = None  # allow | deny | allow_task
    future: asyncio.Future | None = field(default=None, repr=False)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tool": self.tool,
            "arguments": self.arguments,
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "reason": self.reason,
            "created_at": self.created_at,
            "decision": self.decision,
        }


class ApprovalBroker:
    def __init__(self, bus: EventBus, timeout_s: float = 600.0) -> None:
        self.bus = bus
        self.timeout_s = timeout_s
        self.pending: dict[str, ApprovalRequest] = {}
        self._task_grants: dict[tuple[str | None, str], bool] = {}
        self.history: list[ApprovalRequest] = []

    def remembered(self, task_id: str | None, tool: str) -> bool | None:
        return self._task_grants.get((task_id, tool))

    async def request(
        self,
        tool: str,
        arguments: dict[str, Any],
        *,
        task_id: str | None,
        agent_id: str | None,
        reason: str = "",
    ) -> bool:
        remembered = self.remembered(task_id, tool)
        if remembered is not None:
            return remembered
        loop = asyncio.get_running_loop()
        req = ApprovalRequest(
            id=new_id("appr"), tool=tool, arguments=arguments, task_id=task_id,
            agent_id=agent_id, reason=reason, future=loop.create_future(),
        )
        self.pending[req.id] = req
        self.bus.publish("approval.requested", **req.as_dict())
        try:
            return await asyncio.wait_for(req.future, timeout=self.timeout_s)
        except TimeoutError:
            req.decision = "timeout"
            self.bus.publish("approval.timeout", id=req.id, tool=tool)
            return False
        finally:
            self.pending.pop(req.id, None)
            self.history.append(req)
            del self.history[:-200]

    def respond(self, request_id: str, decision: str) -> bool:
        """decision: allow | deny | allow_task | deny_task"""
        req = self.pending.get(request_id)
        if req is None or req.future is None or req.future.done():
            return False
        allow = decision in ("allow", "allow_task")
        if decision in ("allow_task", "deny_task"):
            self._task_grants[(req.task_id, req.tool)] = allow
        req.decision = decision
        req.future.set_result(allow)
        self.bus.publish("approval.decided", id=request_id, tool=req.tool, decision=decision)
        return True

    def forget_task(self, task_id: str) -> None:
        for key in [k for k in self._task_grants if k[0] == task_id]:
            self._task_grants.pop(key, None)
        for req in list(self.pending.values()):
            if req.task_id == task_id and req.future and not req.future.done():
                req.future.set_result(False)
