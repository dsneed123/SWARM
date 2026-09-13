"""Tasks, their persistence, and the global queue.

A task is one user objective. Its planning/execution state (spec, DAG
states, result) is persisted so the TUI can show history and so a restart
does not lose what was done. Queued tasks are admitted by the orchestrator
according to hardware headroom, not a fixed slot count.
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, Field

from swarm.core.db import Database
from swarm.core.types import ExecutionMode, TaskStatus, new_id
from swarm.workflow.dag import WorkflowSpec


class Question(BaseModel):
    id: str = Field(default_factory=lambda: new_id("q"))
    text: str
    options: list[str] = Field(default_factory=list)
    context: str = ""
    answer: str | None = None
    asked_at: float = Field(default_factory=time.time)
    answered_at: float | None = None
    node_id: str | None = None


class Task(BaseModel):
    id: str = Field(default_factory=lambda: new_id("task"))
    objective: str
    mode: ExecutionMode = ExecutionMode.AUTONOMOUS
    status: TaskStatus = TaskStatus.QUEUED
    priority: int = 0  # higher first
    workflow_slug: str | None = None  # user workflow used, if any
    spec: WorkflowSpec | None = None
    objective_class: str = ""
    complexity: str = ""
    created_at: float = Field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    result_artifact_id: str | None = None
    error: str | None = None
    questions: list[Question] = Field(default_factory=list)
    interventions: list[dict[str, Any]] = Field(default_factory=list)  # manual actions by the user
    node_states: dict[str, Any] = Field(default_factory=dict)  # snapshot for persistence/history
    overrides: dict[str, Any] = Field(default_factory=dict)  # e.g. {"tier": "deep", "redundancy": 3}
    cwd: str | None = None  # directory the user launched from; file tools work there
    models_used: list[str] = Field(default_factory=list)
    summary: str = ""

    @property
    def elapsed_s(self) -> float:
        if not self.started_at:
            return 0.0
        return (self.finished_at or time.time()) - self.started_at

    def pending_question(self) -> Question | None:
        for q in self.questions:
            if q.answer is None:
                return q
        return None

    def brief(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "objective": self.objective,
            "mode": self.mode.value,
            "status": self.status.value,
            "priority": self.priority,
            "workflow": self.workflow_slug or (self.spec.name if self.spec else None),
            "objective_class": self.objective_class,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_s": round(self.elapsed_s, 1),
            "result_artifact_id": self.result_artifact_id,
            "error": self.error,
            "pending_question": self.pending_question().model_dump() if self.pending_question() else None,
            "nodes": len(self.spec.nodes) if self.spec else 0,
            "summary": self.summary,
            "cwd": self.cwd,
        }


class TaskStore:
    KIND = "task"

    def __init__(self, db: Database) -> None:
        self.db = db

    def save(self, task: Task) -> None:
        self.db.put_doc(self.KIND, task.id, task.model_dump(mode="json"))

    def get(self, task_id: str) -> Task | None:
        doc = self.db.get_doc(self.KIND, task_id)
        return Task.model_validate(doc) if doc else None

    def all(self) -> list[Task]:
        tasks = [Task.model_validate(d) for d in self.db.list_docs(self.KIND).values()]
        tasks.sort(key=lambda t: t.created_at, reverse=True)
        return tasks

    def delete(self, task_id: str) -> None:
        self.db.delete_doc(self.KIND, task_id)

    def mark_interrupted(self) -> list[str]:
        """On startup: tasks that were running when the service died."""
        out = []
        for t in self.all():
            if not t.status.terminal:
                t.status = TaskStatus.FAILED
                t.error = "service restarted while the task was active"
                t.finished_at = time.time()
                self.save(t)
                out.append(t.id)
        return out


class TaskQueue:
    """In-memory view of live tasks; persistence goes through TaskStore."""

    def __init__(self, store: TaskStore) -> None:
        self.store = store
        self.tasks: dict[str, Task] = {}

    def add(self, task: Task) -> Task:
        self.tasks[task.id] = task
        self.store.save(task)
        return task

    def get(self, task_id: str) -> Task | None:
        return self.tasks.get(task_id) or self.store.get(task_id)

    def save(self, task: Task) -> None:
        self.tasks[task.id] = task
        self.store.save(task)

    def queued(self) -> list[Task]:
        q = [t for t in self.tasks.values() if t.status == TaskStatus.QUEUED]
        q.sort(key=lambda t: (-t.priority, t.created_at))
        return q

    def active(self) -> list[Task]:
        return [t for t in self.tasks.values() if not t.status.terminal and t.status != TaskStatus.QUEUED]

    def recent(self, limit: int = 50) -> list[Task]:
        live = list(self.tasks.values())
        seen = {t.id for t in live}
        stored = [t for t in self.store.all() if t.id not in seen]
        allt = live + stored
        allt.sort(key=lambda t: t.created_at, reverse=True)
        return allt[:limit]
