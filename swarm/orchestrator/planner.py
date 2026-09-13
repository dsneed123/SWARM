"""Turns an objective into a workflow spec.

The orchestrator's small model classifies the objective and proposes a DAG
using the capability library, guided by tier/redundancy rules and by
compositions that worked before for the same class. Invalid plans are
retried once on a deeper model. If that fails too, a template for the class
is used so a task never dies in planning.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from swarm.capabilities.library import CAPABILITIES
from swarm.core.types import Tier
from swarm.memory.learning import WorkflowKnowledge
from swarm.orchestrator.assist import Assistant
from swarm.workflow.dag import ConsensusRule, NodeSpec, WorkflowSpec

log = logging.getLogger(__name__)

CLASSES = ("question", "research", "analysis", "coding", "writing", "action", "mixed")

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "objective_class": {"type": "string", "enum": list(CLASSES)},
        "complexity": {"type": "string", "enum": ["simple", "moderate", "complex"]},
        "needs_external_info": {"type": "boolean"},
        "accuracy_critical": {"type": "boolean"},
        "nodes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "capability": {"type": "string", "enum": sorted(CAPABILITIES)},
                    "instruction": {"type": "string"},
                    "tier": {"type": "string", "enum": ["fast", "standard", "deep"]},
                    "redundancy": {"type": "integer"},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "capability", "instruction", "depends_on"],
            },
        },
    },
    "required": ["objective_class", "complexity", "needs_external_info", "nodes"],
}

PLANNER_SYSTEM = """You are the planner of a local AI agent swarm. Given a user objective you design a small DAG of
agents. Available capabilities:
{capabilities}

Tiers: fast (small model, quick, good for retrieval/extraction/simple checks), standard (mid-size, most reasoning),
deep (largest model, slow; use for synthesis of conflicting evidence, hard reasoning, final answers that must be right).

Rules:
- Create only the nodes the objective needs. A simple factual question: one node (general or research) is enough.
- When facts must be right, use redundancy 2-3 on research/verification nodes (independent agents get consensus).
- Add a criticism node for anything where errors are costly, and a synthesis node whenever several nodes feed a final answer.
- Node ids: short snake_case. depends_on must reference earlier ids. Instructions must be specific and self-contained;
  refer to the objective as {{objective}} (a placeholder).
- Prefer fast tier for research/verification/tool_use, standard for analysis/coding/writing/criticism, deep for synthesis.
- Never exceed {max_nodes} nodes or redundancy {max_redundancy}."""


class Planner:
    def __init__(self, assistant: Assistant, knowledge: WorkflowKnowledge, *, max_redundancy: int = 5,
                 max_nodes: int = 10, escalation_tier: Tier = Tier.DEEP) -> None:
        self.assistant = assistant
        self.knowledge = knowledge
        self.max_redundancy = max_redundancy
        self.max_nodes = max_nodes
        self.escalation_tier = escalation_tier

    async def plan(self, objective: str, overrides: dict[str, Any] | None = None) -> tuple[WorkflowSpec, dict[str, Any]]:
        overrides = overrides or {}
        caps = "\n".join(f"- {c.name}: {c.description}" for c in CAPABILITIES.values())
        system = PLANNER_SYSTEM.format(capabilities=caps, max_nodes=self.max_nodes, max_redundancy=self.max_redundancy)
        user = f"OBJECTIVE:\n{objective.strip()}\n"
        guess = classify_heuristically(objective)
        hint = self.knowledge.suggest(guess)
        if hint:
            user += (f"\nA composition that worked well before for similar ({guess}) objectives: {hint.signature} "
                     f"(avg confidence {hint.avg_confidence:.2f}, {hint.avg_duration_s:.0f}s). Use it as a starting point if it fits.\n")
        if overrides.get("tier"):
            user += f"\nThe user requested the {overrides['tier']} tier for the main work.\n"
        if overrides.get("redundancy"):
            user += f"\nThe user requested redundancy {overrides['redundancy']} for the main investigation.\n"
        user += "\nReturn the plan JSON."
        meta: dict[str, Any] = {"source": "model", "class_guess": guess, "hint": hint.signature if hint else None}
        data = await self.assistant.ask_json(system, user, PLAN_SCHEMA, purpose="planning", max_tokens=2000)
        spec = self._to_spec(data, objective, overrides) if data else None
        if spec is None:
            meta["escalated"] = True
            data = await self.assistant.ask_json(system, user, PLAN_SCHEMA, tier=self.escalation_tier,
                                                 purpose="planning-escalated", max_tokens=2000, time_budget_s=300)
            spec = self._to_spec(data, objective, overrides) if data else None
        if spec is None:
            meta["source"] = "template"
            spec = template_plan(guess, objective, overrides, self.max_redundancy)
            data = {"objective_class": guess, "complexity": "moderate"}
        meta["objective_class"] = spec.objective_class
        meta["complexity"] = (data or {}).get("complexity", "moderate")
        meta["accuracy_critical"] = bool((data or {}).get("accuracy_critical"))
        return spec, meta

    def _to_spec(self, data: dict[str, Any], objective: str, overrides: dict[str, Any]) -> WorkflowSpec | None:
        try:
            nodes_raw = data.get("nodes") or []
            if not nodes_raw:
                return None
            cls = data.get("objective_class") if data.get("objective_class") in CLASSES else classify_heuristically(objective)
            nodes: list[NodeSpec] = []
            ids: dict[str, str] = {}
            for i, raw in enumerate(nodes_raw[: self.max_nodes]):
                nid = _slug(str(raw.get("id") or f"n{i + 1}"))
                while nid in ids.values():
                    nid += "_"
                ids[str(raw.get("id") or nid)] = nid
            for i, raw in enumerate(nodes_raw[: self.max_nodes]):
                cap = str(raw.get("capability", "general"))
                if cap not in CAPABILITIES:
                    cap = "general"
                tier = _tier(raw.get("tier"))
                red = int(raw.get("redundancy") or 1)
                red = max(1, min(self.max_redundancy, red))
                deps = [ids[d] for d in (raw.get("depends_on") or []) if d in ids and ids[d] != ids[str(raw.get("id") or f"n{i + 1}")]]
                instr = str(raw.get("instruction") or "").strip()
                if not instr:
                    instr = f"Work on: {{objective}}"
                if "{objective}" not in instr:
                    instr += "\n\nObjective: {objective}"
                nodes.append(NodeSpec(
                    id=ids[str(raw.get("id") or f"n{i + 1}")], name=str(raw.get("name") or "")[:60], capability=cap,
                    instruction=instr, tier=tier, redundancy=red, depends_on=deps,
                    consensus=ConsensusRule(max_extra_agents=2 if red > 1 else 1),
                ))
            apply_overrides(nodes, overrides, self.max_redundancy)
            spec = WorkflowSpec(name=f"auto: {objective[:50]}", description="generated by the orchestrator",
                                nodes=nodes, immutable=False, objective_class=cls)
            return spec
        except Exception as e:  # noqa: BLE001 - any malformed plan is a retry
            log.info("plan rejected: %s", e)
            return None


def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9_]+", "_", s.lower()).strip("_")
    return s[:32] or "node"


def _tier(v: Any) -> Tier | None:
    try:
        return Tier(str(v).lower()) if v else None
    except ValueError:
        return None


def apply_overrides(nodes: list[NodeSpec], overrides: dict[str, Any], max_redundancy: int) -> None:
    tier = _tier(overrides.get("tier"))
    model = overrides.get("model")
    red = overrides.get("redundancy")
    for n in nodes:
        if tier and n.capability not in ("synthesis",):
            n.tier = tier
        if model:
            n.model = model
        if red and n.capability in ("research", "verification", "reasoning", "analysis", "general"):
            n.redundancy = max(1, min(max_redundancy, int(red)))


_RESEARCH = re.compile(r"\b(research|find out|latest|current|news|compare|who|when|where|what is|how much|price|source|cite)\b", re.I)
_CODE = re.compile(r"\b(code|script|function|implement|bug|python|javascript|rust|api|program|refactor|test)\b", re.I)
_WRITE = re.compile(r"\b(write|draft|essay|report|summar|blog|email|document|article|memo)\b", re.I)
_ANALYSE = re.compile(r"\b(analy[sz]e|evaluate|assess|review|pros and cons|trade-?offs|data|csv|statistics)\b", re.I)
_ACTION = re.compile(r"\b(create an issue|open a pr|post|send|upload|call the api|github)\b", re.I)


def classify_heuristically(objective: str) -> str:
    o = objective.strip()
    scores = {
        "action": len(_ACTION.findall(o)) * 2,
        "coding": len(_CODE.findall(o)),
        "writing": len(_WRITE.findall(o)),
        "analysis": len(_ANALYSE.findall(o)),
        "research": len(_RESEARCH.findall(o)),
    }
    best = max(scores, key=scores.get)
    if scores[best] == 0:
        return "question" if len(o) < 160 and o.rstrip().endswith("?") else "research"
    return best


def template_plan(cls: str, objective: str, overrides: dict[str, Any], max_redundancy: int) -> WorkflowSpec:
    o = "{objective}"
    if cls == "question":
        nodes = [NodeSpec(id="answer", capability="general", instruction=f"Answer accurately: {o}", tier=Tier.STANDARD, redundancy=2)]
    elif cls == "coding":
        nodes = [
            NodeSpec(id="plan", capability="planning", instruction=f"Plan the implementation for: {o}", tier=Tier.STANDARD),
            NodeSpec(id="implement", capability="coding", instruction=f"Implement the plan for: {o}. Test with python.", depends_on=["plan"], tier=Tier.STANDARD),
            NodeSpec(id="review", capability="criticism", instruction=f"Review the implementation for: {o}", depends_on=["implement"], tier=Tier.STANDARD),
            NodeSpec(id="finalize", capability="coding", instruction=f"Apply the review and deliver final code for: {o}", depends_on=["review"], context_from=["implement", "review"], tier=Tier.STANDARD),
        ]
    elif cls == "writing":
        nodes = [
            NodeSpec(id="gather", capability="research", instruction=f"Gather source material for: {o}", tier=Tier.FAST, redundancy=2),
            NodeSpec(id="draft", capability="writing", instruction=f"Write: {o}", depends_on=["gather"], tier=Tier.STANDARD),
            NodeSpec(id="critique", capability="criticism", instruction=f"Critique the draft for: {o}", depends_on=["draft"], tier=Tier.STANDARD),
            NodeSpec(id="final", capability="writing", instruction=f"Revise the draft using the critique. Deliver the final text for: {o}", depends_on=["critique"], context_from=["draft", "critique"], tier=Tier.STANDARD),
        ]
    elif cls == "analysis":
        nodes = [
            NodeSpec(id="analyze", capability="analysis", instruction=f"Analyse: {o}", tier=Tier.STANDARD, redundancy=2),
            NodeSpec(id="critique", capability="criticism", instruction=f"Critique the analysis of: {o}", depends_on=["analyze"], tier=Tier.STANDARD),
            NodeSpec(id="synthesis", capability="synthesis", instruction=f"Deliver the final analysis for: {o}", depends_on=["critique"], context_from=["analyze", "critique"], tier=Tier.DEEP),
        ]
    elif cls == "action":
        nodes = [
            NodeSpec(id="plan", capability="planning", instruction=f"Plan the concrete steps for: {o}", tier=Tier.STANDARD),
            NodeSpec(id="act", capability="tool_use", instruction=f"Carry out the plan for: {o}", depends_on=["plan"], tier=Tier.FAST),
            NodeSpec(id="verify", capability="verification", instruction=f"Verify the outcome of: {o}", depends_on=["act"], tier=Tier.FAST),
        ]
    else:  # research / mixed
        nodes = [
            NodeSpec(id="research", capability="research", instruction=f"Research thoroughly with sources: {o}", tier=Tier.FAST, redundancy=3),
            NodeSpec(id="critique", capability="criticism", instruction=f"Critique the research on: {o}", depends_on=["research"], tier=Tier.STANDARD),
            NodeSpec(id="verify", capability="verification", instruction=f"Verify the key claims found for: {o}", depends_on=["research"], tier=Tier.FAST, redundancy=2),
            NodeSpec(id="synthesis", capability="synthesis", instruction=f"Write the final, cited answer to: {o}", depends_on=["critique", "verify"], context_from=["research", "critique", "verify"], tier=Tier.DEEP),
        ]
    apply_overrides(nodes, overrides, max_redundancy)
    return WorkflowSpec(name=f"auto ({cls}): {objective[:40]}", description="template plan", nodes=nodes, immutable=False, objective_class=cls)
