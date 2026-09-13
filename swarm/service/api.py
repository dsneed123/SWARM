"""The command surface shared by the socket daemon and the embedded mode.

Every call takes a dict of parameters and returns JSON-serialisable data.
The TUI only ever talks to this layer.
"""

from __future__ import annotations

import asyncio
from typing import Any

from swarm.app import App, persist_settings
from swarm.core.types import ExecutionMode
from swarm.workflow.dag import WorkflowSpec


class Api:
    def __init__(self, app: App) -> None:
        self.app = app

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}
        fn = getattr(self, f"m_{method}", None)
        if fn is None:
            raise ValueError(f"unknown method {method}")
        result = fn(**params)
        if asyncio.iscoroutine(result):
            result = await result
        return result

    # --- state ------------------------------------------------------------

    def m_snapshot(self) -> dict[str, Any]:
        snap = self.app.orchestrator.snapshot()
        snap["hardware_info"] = self.app.monitor.info.__dict__
        snap["settings"] = {
            "memory_ceiling_percent": self.app.settings.hardware.memory_ceiling_percent,
            "default_mode": self.app.settings.orchestrator.default_mode.value,
            "tiers": self.app.settings.tiers.model_dump(),
            "workspace": str(self.app.settings.workspace),
            "backend": self.app.backend.name,
            "ollama_host": self.app.settings.ollama.host,
        }
        snap["backend_ok"] = self.app.scheduler._task is not None
        return snap

    def m_events(self, limit: int = 100) -> list[dict[str, Any]]:
        return list(self.app.bus.history)[-limit:]

    def m_task(self, task_id: str) -> dict[str, Any] | None:
        return self.app.orchestrator.task_detail(task_id)

    def m_agent(self, agent_id: str) -> dict[str, Any] | None:
        a = self.app.runtime.get(agent_id)
        return a.as_dict(full=True) if a else None

    def m_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        a = self.app.artifacts.get(artifact_id)
        return a.model_dump(mode="json") if a else None

    def m_artifacts(self, task_id: str) -> list[dict[str, Any]]:
        return [a.model_dump(mode="json") for a in self.app.artifacts.for_task(task_id)]

    def m_tools(self) -> list[dict[str, Any]]:
        return self.app.tools.catalog()

    def m_learning(self) -> dict[str, Any]:
        return {"workflows": self.app.orchestrator.learning.summary(),
                "failures": self.app.orchestrator.failures.recent(30)}

    # --- tasks ------------------------------------------------------------

    def m_submit(self, objective: str, mode: str | None = None, workflow: str | None = None,
                 overrides: dict[str, Any] | None = None, priority: int = 0, cwd: str | None = None) -> dict[str, Any]:
        task = self.app.orchestrator.submit(objective, mode=ExecutionMode(mode) if mode else None, workflow=workflow,
                                            overrides=overrides, priority=priority, cwd=cwd)
        return task.brief()

    def m_pause(self, task_id: str) -> bool:
        return self.app.orchestrator.pause(task_id)

    def m_resume(self, task_id: str) -> bool:
        return self.app.orchestrator.resume(task_id)

    def m_cancel(self, task_id: str) -> bool:
        return self.app.orchestrator.cancel(task_id)

    def m_answer(self, task_id: str, question_id: str, answer: str) -> bool:
        return self.app.orchestrator.answer(task_id, question_id, answer)

    def m_retry_node(self, task_id: str, node_id: str) -> bool:
        return self.app.orchestrator.retry_node(task_id, node_id)

    def m_reassign(self, task_id: str, node_id: str, model: str | None = None, tier: str | None = None) -> bool:
        return self.app.orchestrator.reassign_model(task_id, node_id, model, tier)

    def m_cancel_agent(self, agent_id: str) -> bool:
        return self.app.runtime.cancel(agent_id)

    def m_approve(self, request_id: str, decision: str) -> bool:
        return self.app.approvals.respond(request_id, decision)

    # --- models / settings -------------------------------------------------

    async def m_refresh_models(self) -> int:
        if not await self.app.ensure_backend():
            return 0
        return len(await self.app.scheduler.refresh_models())

    async def m_unload_model(self, name: str) -> bool:
        return await self.app.scheduler.unload_model(name)

    def m_settings(self, changes: dict[str, Any]) -> dict[str, Any]:
        persist_settings(self.app, changes)
        return self.m_snapshot()["settings"]

    # --- workflows ----------------------------------------------------------

    def m_workflows(self) -> list[dict[str, Any]]:
        return self.app.orchestrator.workflows.list()

    def m_workflow(self, slug: str) -> dict[str, Any]:
        return self.app.orchestrator.workflows.load(slug).model_dump(mode="json")

    def m_save_workflow(self, spec: dict[str, Any], slug: str | None = None) -> str:
        ws = WorkflowSpec.model_validate(spec)
        return str(self.app.orchestrator.workflows.save(ws, slug))

    def m_delete_workflow(self, slug: str) -> bool:
        return self.app.orchestrator.workflows.delete(slug)

    def m_capabilities(self) -> list[dict[str, Any]]:
        from swarm.capabilities.library import CAPABILITIES

        return [{"name": c.name, "description": c.description, "tier": c.tier.value, "tools": c.tools} for c in CAPABILITIES.values()]

    def m_ping(self) -> str:
        return "pong"
