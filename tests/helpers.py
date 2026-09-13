"""Shared fixtures for exercising the engine with the fake backend."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from swarm.agents.runtime import AgentRuntime
from swarm.agents.state import AgentStateStore
from swarm.artifacts.store import ArtifactStore
from swarm.config import Settings
from swarm.core.db import Database
from swarm.core.events import EventBus
from swarm.hardware.budget import GB, ResourceBudget
from swarm.hardware.telemetry import HardwareMonitor, HardwareSample
from swarm.models.backend import ChatMessage, ChatResult, ToolCall
from swarm.models.fake import FakeBackend, FakeModel
from swarm.models.profiles import ProfileStore
from swarm.models.scheduler import ModelScheduler
from swarm.paths import Workspace
from swarm.permissions.approvals import ApprovalBroker
from swarm.permissions.policy import PermissionResolver
from swarm.tools.builtin import builtin_tools
from swarm.tools.registry import ToolRegistry


class FixedMonitor(HardwareMonitor):
    def __init__(self, total_gb: float, available_gb: float, budget: ResourceBudget | None = None):
        self.total = int(total_gb * GB)
        self.available = int(available_gb * GB)
        self.budget = budget
        super().__init__()

    def sample(self) -> HardwareSample:
        allocated = self.budget.allocated if self.budget else 0
        s = HardwareSample(
            ts=time.time(), mem_total=self.total, mem_available=self.available - allocated,
            mem_used=self.total - self.available + allocated, swap_used=0, cpu_percent=5.0, load1=0.5,
        )
        self._latest = s
        return s


class Harness:
    """Everything below the orchestrator, wired to a FakeBackend."""

    def __init__(self, tmp_path: Path, responder=None, models: list[FakeModel] | None = None,
                 profile: str = "autonomous", total_gb: float = 128, available_gb: float = 110, parallel: int = 4):
        self.settings = Settings(workspace=tmp_path / "ws")
        self.settings.ollama.num_parallel = parallel
        self.settings.permissions.profile = profile
        self.workspace = Workspace(self.settings.workspace).ensure()
        self.bus = EventBus()
        self.events: list[dict[str, Any]] = []
        self.bus.subscribe(self.events.append)
        self.db = Database(self.workspace.db)
        self.backend = FakeBackend(models, responder=responder)
        self.profiles = ProfileStore(self.db)
        self.budget = ResourceBudget(self.settings.hardware.memory_ceiling_percent, 2)
        self.monitor = FixedMonitor(total_gb, available_gb, self.budget)
        self.scheduler = ModelScheduler(self.backend, self.profiles, self.budget, self.monitor, self.bus, self.settings)
        self.scheduler.reconcile_interval_s = 0.05
        self.approvals = ApprovalBroker(self.bus, timeout_s=0.5)
        self.resolver = PermissionResolver(profile)
        self.tools = ToolRegistry(self.resolver, self.approvals, self.bus)
        for t in builtin_tools():
            self.tools.register(t)
        self.artifacts = ArtifactStore(self.workspace.artifacts, self.workspace.db)
        self.states = AgentStateStore(self.db)
        self.runtime = AgentRuntime(self.scheduler, self.tools, self.artifacts, self.states, self.bus,
                                    self.workspace, self.settings)

    async def start(self) -> Harness:
        await self.scheduler.start()
        return self

    async def stop(self) -> None:
        await self.scheduler.stop()
        self.artifacts.close()
        self.db.close()


def structured(conclusion: str, confidence: float = 0.8, **extra: Any) -> dict[str, Any]:
    d = {"conclusion": conclusion, "confidence": confidence, "evidence": [], "reasoning_summary": "because"}
    d.update(extra)
    return d


def tool_call(name: str, **arguments: Any) -> ChatResult:
    return ChatResult(content="", tool_calls=[ToolCall(name=name, arguments=arguments)], completion_tokens=5, eval_s=0.1)


def last_user(messages: list[ChatMessage]) -> str:
    for m in reversed(messages):
        if m.role == "user":
            return m.content
    return ""


def dump(obj: Any) -> str:
    return json.dumps(obj)
