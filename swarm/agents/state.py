"""State for persistent agents.

Persistent agents keep compact notes between runs. This lives in the
database, not in a loaded model: the model can be unloaded and reloaded
freely while the agent's memory stays put.
"""

from __future__ import annotations

import time
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from swarm.core.db import Database


class AgentState(BaseModel):
    key: str  # "<workflow or task>:<node>" chosen by the orchestrator
    notes: str = ""  # agent-maintained running notes, kept short
    runs: int = 0
    last_artifacts: list[str] = Field(default_factory=list)
    updated_at: float = Field(default_factory=time.time)
    extras: dict[str, Any] = Field(default_factory=dict)

    MAX_NOTES: ClassVar[int] = 4000

    def remember(self, notes: str, artifact_id: str | None) -> None:
        self.notes = notes[-self.MAX_NOTES :]
        if artifact_id:
            self.last_artifacts = (self.last_artifacts + [artifact_id])[-10:]
        self.runs += 1
        self.updated_at = time.time()


class AgentStateStore:
    KIND = "agent_state"

    def __init__(self, db: Database) -> None:
        self.db = db

    def get(self, key: str) -> AgentState:
        doc = self.db.get_doc(self.KIND, key)
        return AgentState.model_validate(doc) if doc else AgentState(key=key)

    def save(self, state: AgentState) -> None:
        self.db.put_doc(self.KIND, state.key, state.model_dump(mode="json"))

    def delete(self, key: str) -> None:
        self.db.delete_doc(self.KIND, key)
