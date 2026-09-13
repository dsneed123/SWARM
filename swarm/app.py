"""Wires the subsystems together. Used by the service daemon, the embedded
TUI mode and integration tests."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from swarm.agents.runtime import AgentRuntime
from swarm.agents.state import AgentStateStore
from swarm.artifacts.store import ArtifactStore
from swarm.config import Settings, load_settings
from swarm.core.db import Database
from swarm.core.events import EventBus
from swarm.hardware.budget import ResourceBudget
from swarm.hardware.telemetry import HardwareMonitor
from swarm.models.backend import ModelBackend
from swarm.models.ollama import OllamaBackend
from swarm.models.profiles import ProfileStore
from swarm.models.scheduler import ModelScheduler
from swarm.orchestrator.orchestrator import Orchestrator
from swarm.paths import Workspace
from swarm.permissions.approvals import ApprovalBroker
from swarm.permissions.policy import PermissionResolver
from swarm.tools.builtin import builtin_tools
from swarm.tools.registry import ToolRegistry

log = logging.getLogger(__name__)


@dataclass
class App:
    settings: Settings
    workspace: Workspace
    bus: EventBus
    db: Database
    monitor: HardwareMonitor
    backend: ModelBackend
    scheduler: ModelScheduler
    tools: ToolRegistry
    approvals: ApprovalBroker
    artifacts: ArtifactStore
    runtime: AgentRuntime
    orchestrator: Orchestrator

    async def start(self) -> None:
        await self.monitor.start()
        healthy = await self.backend.health()
        self.bus.publish("backend.health", backend=self.backend.name, healthy=healthy)
        if healthy:
            await self.scheduler.start()
        else:
            log.warning("model backend %s is not reachable; tasks will wait until it is", self.backend.name)
            self.scheduler._task = None
        await self.orchestrator.start()

    async def stop(self) -> None:
        await self.orchestrator.stop()
        await self.scheduler.stop()
        await self.monitor.stop()
        await self.backend.close()
        self.artifacts.close()
        self.db.close()

    async def ensure_backend(self) -> bool:
        """Retry starting the scheduler if the backend was down at boot."""
        if self.scheduler._task is not None:
            return True
        if await self.backend.health():
            await self.scheduler.start()
            self.bus.publish("backend.health", backend=self.backend.name, healthy=True)
            return True
        return False


def build_app(settings: Settings | None = None, backend: ModelBackend | None = None, workspace: Path | None = None) -> App:
    settings = settings or load_settings(workspace)
    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ws = Workspace(settings.workspace).ensure()
    file_handler = logging.FileHandler(ws.logs / "swarm.log")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(file_handler)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    bus = EventBus()
    db = Database(ws.db)
    monitor = HardwareMonitor(interval_s=settings.hardware.sample_interval_s)
    backend = backend or OllamaBackend(settings.ollama.host, settings.ollama.request_timeout_s)
    profiles = ProfileStore(db)
    budget = ResourceBudget(settings.hardware.memory_ceiling_percent, settings.hardware.safety_reserve_gb)
    scheduler = ModelScheduler(backend, profiles, budget, monitor, bus, settings)
    approvals = ApprovalBroker(bus, timeout_s=settings.permissions.ask_timeout_s)
    resolver = PermissionResolver(settings.permissions.profile, settings.permissions.overrides)
    tools = ToolRegistry(resolver, approvals, bus)
    for t in builtin_tools():
        tools.register(t)
    artifacts = ArtifactStore(ws.artifacts, ws.db)
    runtime = AgentRuntime(scheduler, tools, artifacts, AgentStateStore(db), bus, ws, settings)
    orchestrator = Orchestrator(settings, bus, scheduler, runtime, tools, approvals, artifacts, db, ws, monitor)
    return App(settings=settings, workspace=ws, bus=bus, db=db, monitor=monitor, backend=backend, scheduler=scheduler,
               tools=tools, approvals=approvals, artifacts=artifacts, runtime=runtime, orchestrator=orchestrator)


def persist_settings(app: App, changes: dict[str, Any]) -> None:
    """Apply a few user-editable settings at runtime and write config.yaml."""
    if "memory_ceiling_percent" in changes:
        app.orchestrator.set_ceiling(float(changes["memory_ceiling_percent"]))
    if "permission_profile" in changes:
        app.tools.resolver.set_profile(str(changes["permission_profile"]))
        app.settings.permissions.profile = str(changes["permission_profile"])
    if "tool_overrides" in changes:
        from swarm.core.types import Policy

        for tool, pol in dict(changes["tool_overrides"]).items():
            app.tools.resolver.set_override(tool, Policy(pol) if pol else None)
        app.settings.permissions.overrides = dict(app.tools.resolver.overrides)
    if "default_mode" in changes:
        from swarm.core.types import ExecutionMode

        app.settings.orchestrator.default_mode = ExecutionMode(changes["default_mode"])
    for tier in ("fast", "standard", "deep"):
        key = f"tier_{tier}"
        if key in changes:
            setattr(app.settings.tiers, tier, changes[key] or None)
    app.settings.save()
    app.bus.publish("settings.changed", changes={k: str(v)[:80] for k, v in changes.items()})
