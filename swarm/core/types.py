"""Small shared enums and value types used across subsystems.

Anything here must be dependency-free so every module can import it.
"""

from __future__ import annotations

import enum
import secrets
import time
from datetime import UTC, datetime


class Tier(str, enum.Enum):
    """Conceptual intelligence tiers. The router maps these to real models."""

    FAST = "fast"
    STANDARD = "standard"
    DEEP = "deep"

    @property
    def rank(self) -> int:
        return {"fast": 0, "standard": 1, "deep": 2}[self.value]

    def up(self) -> Tier:
        return {Tier.FAST: Tier.STANDARD, Tier.STANDARD: Tier.DEEP, Tier.DEEP: Tier.DEEP}[self]

    def down(self) -> Tier:
        return {Tier.FAST: Tier.FAST, Tier.STANDARD: Tier.FAST, Tier.DEEP: Tier.STANDARD}[self]


class TaskStatus(str, enum.Enum):
    QUEUED = "queued"
    PLANNING = "planning"
    RUNNING = "running"
    PAUSED = "paused"
    WAITING_USER = "waiting_user"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED)


class NodeStatus(str, enum.Enum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    CONSENSUS = "consensus"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in (
            NodeStatus.COMPLETED,
            NodeStatus.FAILED,
            NodeStatus.SKIPPED,
            NodeStatus.CANCELLED,
        )


class AgentStatus(str, enum.Enum):
    CREATED = "created"
    WAITING_MODEL = "waiting_model"
    RUNNING = "running"
    TOOL_CALL = "tool_call"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in (AgentStatus.COMPLETED, AgentStatus.FAILED, AgentStatus.CANCELLED)


class ExecutionMode(str, enum.Enum):
    AUTONOMOUS = "autonomous"
    INTERACTIVE = "interactive"


class Policy(str, enum.Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


def new_id(prefix: str) -> str:
    """Short, sortable-enough ids like ``task_5f3a9c2d``."""
    return f"{prefix}_{secrets.token_hex(4)}"


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def monotonic() -> float:
    return time.monotonic()
