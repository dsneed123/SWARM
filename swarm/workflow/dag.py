"""Workflow specs and the runtime DAG.

``WorkflowSpec`` is the declarative description: nodes, their capability,
tier/model, redundancy, consensus rule, tools, permissions and dependencies.
The orchestrator generates one for automatic tasks; users write them for
reusable workflows.

``DAG`` is the live execution state over a spec. Automatic DAGs are mutable
(the orchestrator can add nodes while running); user DAGs are immutable once
execution starts.
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, Field, model_validator

from swarm.core.types import ExecutionMode, NodeStatus, Policy, Tier


class ConsensusRule(BaseModel):
    mode: str = "evidence"  # evidence | single | all
    min_confidence: float = 0.55  # below this the node is flagged weak
    max_extra_agents: int = 2  # additional investigators allowed on disagreement
    escalate: bool = True  # escalate to a deeper model when evidence does not converge


class NodeSpec(BaseModel):
    id: str
    name: str = ""
    capability: str = "general"
    instruction: str = ""  # may contain {objective}
    tier: Tier | None = None
    model: str | None = None
    tools: list[str] | None = None  # None = capability defaults
    depends_on: list[str] = Field(default_factory=list)
    redundancy: int = Field(1, ge=1, le=12)
    independent_models: bool = True
    persistent: bool = False
    consensus: ConsensusRule = Field(default_factory=ConsensusRule)
    permissions: dict[str, Policy] = Field(default_factory=dict)
    context_from: list[str] | None = None  # None = every dependency
    include_objective: bool = True
    time_budget_s: float | None = None
    max_attempts: int = Field(3, ge=1, le=6)
    critical: bool = True
    think: bool | None = None
    temperature: float | None = None
    max_tool_rounds: int | None = None

    @property
    def label(self) -> str:
        return self.name or f"{self.capability} ({self.id})"


class WorkflowSpec(BaseModel):
    name: str
    description: str = ""
    version: int = 1
    nodes: list[NodeSpec]
    mode: ExecutionMode | None = None
    permissions: dict[str, Policy] = Field(default_factory=dict)
    immutable: bool = True  # user-designed workflows cannot be restructured by the orchestrator
    objective_class: str = ""  # research / analysis / coding / writing / question / action / mixed
    tags: list[str] = Field(default_factory=list)
    final_node: str | None = None  # node whose artifact is the task result; default = last sink

    @model_validator(mode="after")
    def _check(self) -> WorkflowSpec:
        ids = [n.id for n in self.nodes]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate node ids")
        known = set(ids)
        for n in self.nodes:
            for d in n.depends_on:
                if d not in known:
                    raise ValueError(f"node {n.id} depends on unknown node {d}")
            if n.context_from:
                for c in n.context_from:
                    if c not in known:
                        raise ValueError(f"node {n.id} takes context from unknown node {c}")
        topo_order(self.nodes)  # raises on cycles
        if self.final_node and self.final_node not in known:
            raise ValueError(f"final_node {self.final_node} is not a node")
        return self

    def node(self, node_id: str) -> NodeSpec:
        for n in self.nodes:
            if n.id == node_id:
                return n
        raise KeyError(node_id)

    def sinks(self) -> list[str]:
        used = {d for n in self.nodes for d in n.depends_on}
        return [n.id for n in self.nodes if n.id not in used]

    def result_node(self) -> str:
        if self.final_node:
            return self.final_node
        order = topo_order(self.nodes)
        sinks = self.sinks()
        return [i for i in order if i in sinks][-1] if sinks else order[-1]


def topo_order(nodes: list[NodeSpec]) -> list[str]:
    deps = {n.id: set(n.depends_on) for n in nodes}
    order: list[str] = []
    remaining = dict(deps)
    while remaining:
        ready = sorted(i for i, d in remaining.items() if not d)
        if not ready:
            raise ValueError(f"workflow has a cycle among {sorted(remaining)}")
        for i in ready:
            order.append(i)
            remaining.pop(i)
        for d in remaining.values():
            d.difference_update(ready)
    return order


class NodeState(BaseModel):
    id: str
    status: NodeStatus = NodeStatus.PENDING
    attempts: int = 0
    started_at: float | None = None
    finished_at: float | None = None
    agent_ids: list[str] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)  # raw per-agent artifacts
    result_artifact_id: str | None = None  # consensus / accepted result
    error: str | None = None
    error_kind: str | None = None
    consensus: dict[str, Any] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)  # orchestrator's recovery/escalation notes

    @property
    def elapsed_s(self) -> float:
        if not self.started_at:
            return 0.0
        return (self.finished_at or time.time()) - self.started_at


class DAG:
    def __init__(self, spec: WorkflowSpec, mutable: bool | None = None) -> None:
        self.spec = spec
        self.mutable = (not spec.immutable) if mutable is None else mutable
        self.nodes: dict[str, NodeSpec] = {n.id: n for n in spec.nodes}
        self.states: dict[str, NodeState] = {n.id: NodeState(id=n.id) for n in spec.nodes}
        self.mutations: list[dict[str, Any]] = []

    # --- structure ------------------------------------------------------

    def order(self) -> list[str]:
        return topo_order(list(self.nodes.values()))

    def add_node(self, node: NodeSpec, reason: str = "") -> NodeSpec:
        if not self.mutable:
            raise PermissionError("this workflow is immutable; the orchestrator may not add nodes")
        if node.id in self.nodes:
            raise ValueError(f"node {node.id} already exists")
        for d in node.depends_on:
            if d not in self.nodes:
                raise ValueError(f"unknown dependency {d}")
        self.nodes[node.id] = node
        try:
            self.order()
        except ValueError:
            del self.nodes[node.id]
            raise
        self.states[node.id] = NodeState(id=node.id)
        self.spec.nodes.append(node)
        self.mutations.append({"op": "add", "node": node.id, "reason": reason, "ts": time.time()})
        return node

    def remove_node(self, node_id: str, reason: str = "") -> None:
        if not self.mutable:
            raise PermissionError("this workflow is immutable")
        st = self.states[node_id]
        if st.status not in (NodeStatus.PENDING, NodeStatus.READY):
            raise ValueError("only nodes that have not started can be removed")
        if any(node_id in n.depends_on for n in self.nodes.values()):
            raise ValueError("other nodes depend on it")
        del self.nodes[node_id]
        del self.states[node_id]
        self.spec.nodes = [n for n in self.spec.nodes if n.id != node_id]
        self.mutations.append({"op": "remove", "node": node_id, "reason": reason, "ts": time.time()})

    def dependencies_met(self, node_id: str) -> bool:
        for d in self.nodes[node_id].depends_on:
            st = self.states[d].status
            if st == NodeStatus.COMPLETED:
                continue
            if st == NodeStatus.SKIPPED and not self.nodes[d].critical:
                continue
            return False
        return True

    def ready(self) -> list[NodeSpec]:
        out = []
        for nid in self.order():
            st = self.states[nid]
            if st.status in (NodeStatus.PENDING, NodeStatus.READY) and self.dependencies_met(nid):
                out.append(self.nodes[nid])
        return out

    def blocked(self) -> list[str]:
        """Pending nodes that can never run because a critical dependency failed."""
        out = []
        for nid, st in self.states.items():
            if st.status not in (NodeStatus.PENDING, NodeStatus.READY):
                continue
            for d in self.nodes[nid].depends_on:
                ds = self.states[d].status
                if ds in (NodeStatus.FAILED, NodeStatus.CANCELLED) or (ds == NodeStatus.SKIPPED and self.nodes[d].critical):
                    out.append(nid)
                    break
        return out

    def inputs_for(self, node_id: str) -> list[str]:
        """Result artifact ids of the nodes this node takes context from."""
        node = self.nodes[node_id]
        sources = node.context_from if node.context_from is not None else node.depends_on
        ids = []
        for s in sources:
            st = self.states.get(s)
            if st and st.result_artifact_id:
                ids.append(st.result_artifact_id)
        return ids

    # --- state ----------------------------------------------------------

    def is_done(self) -> bool:
        return all(s.status.terminal for s in self.states.values()) or (
            not self.ready() and all(s.status.terminal for nid, s in self.states.items() if nid not in self.blocked())
            and not any(s.status in (NodeStatus.RUNNING, NodeStatus.CONSENSUS) for s in self.states.values())
        )

    def succeeded(self) -> bool:
        for nid, st in self.states.items():
            if st.status == NodeStatus.COMPLETED:
                continue
            if st.status == NodeStatus.SKIPPED and not self.nodes[nid].critical:
                continue
            return False
        return True

    def result_artifact(self) -> str | None:
        rid = self.spec.result_node()
        st = self.states.get(rid)
        if st and st.result_artifact_id:
            return st.result_artifact_id
        # fall back to the last completed sink
        for nid in reversed(self.order()):
            if self.states[nid].status == NodeStatus.COMPLETED and self.states[nid].result_artifact_id:
                return self.states[nid].result_artifact_id
        return None

    def progress(self) -> tuple[int, int]:
        done = sum(1 for s in self.states.values() if s.status in (NodeStatus.COMPLETED, NodeStatus.SKIPPED))
        return done, len(self.states)

    def to_dict(self) -> dict[str, Any]:
        order = self.order()
        return {
            "name": self.spec.name,
            "mutable": self.mutable,
            "nodes": [
                {
                    **self.nodes[nid].model_dump(mode="json"),
                    "state": self.states[nid].model_dump(mode="json"),
                    "elapsed_s": round(self.states[nid].elapsed_s, 1),
                }
                for nid in order
            ],
            "mutations": self.mutations,
            "progress": self.progress(),
        }
