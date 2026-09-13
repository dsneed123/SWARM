"""Agent runtime: runs one agent from a spec to an artifact.

An agent is stateless unless the spec names a persistent key. It gets a
model lease from the scheduler, optionally loops over tool calls through the
registry (which enforces permissions), and finishes with a structured JSON
result that becomes an immutable artifact. Failures carry a diagnosis kind so
the orchestrator can choose a recovery strategy instead of blindly retrying.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from swarm.agents.state import AgentStateStore
from swarm.artifacts.model import Artifact, Evidence, ModelInfo, Provenance, ToolUse
from swarm.artifacts.store import ArtifactStore
from swarm.capabilities.library import get_capability
from swarm.core.events import EventBus
from swarm.core.types import AgentStatus, Tier, new_id, now_iso
from swarm.models.backend import BackendError, ChatMessage, ChatResult
from swarm.models.router import ModelRequest
from swarm.models.scheduler import Lease, ModelScheduler
from swarm.paths import Workspace
from swarm.permissions.policy import PermissionScope
from swarm.tools.registry import ToolContext, ToolRegistry

log = logging.getLogger(__name__)

ARTIFACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "conclusion": {"type": "string"},
        "confidence": {"type": "number"},
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string"},
                    "source": {"type": "string"},
                    "source_type": {"type": "string", "enum": ["web", "file", "tool", "model", "user", "artifact"]},
                    "quote": {"type": "string"},
                    "quality": {"type": "number"},
                    "supports": {"type": "boolean"},
                },
                "required": ["claim"],
            },
        },
        "reasoning_summary": {"type": "string"},
        "contradictions": {"type": "array", "items": {"type": "string"}},
        "unresolved": {"type": "array", "items": {"type": "string"}},
        "next_action": {"type": "string"},
        "content": {"type": "string"},
        "notes_for_self": {"type": "string"},
    },
    "required": ["conclusion", "confidence", "evidence", "reasoning_summary"],
}

OUTPUT_INSTRUCTIONS = """
Return ONLY a JSON object with these fields:
- conclusion: the direct answer / result (a few sentences; dense)
- confidence: 0.0-1.0, calibrated
- evidence: list of {claim, source, source_type (web|file|tool|model|user|artifact), quote, quality 0-1, supports true|false}
- reasoning_summary: how you got there, briefly
- contradictions: things that conflict with the conclusion or between sources
- unresolved: open questions you could not settle
- next_action: what should happen next, if anything
- content: the full long-form output when the task calls for one (report, code, plan); otherwise empty
""".strip()


class AgentError(RuntimeError):
    """kind: model | tool | context | resource | timeout | format | cancelled | backend"""

    def __init__(self, kind: str, message: str, *, partial: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.partial = partial or {}


@dataclass
class AgentSpec:
    task_id: str
    node_id: str
    capability: str
    instruction: str
    tier: Tier | None = None
    model: str | None = None
    tools: list[str] | None = None  # None = capability defaults
    context: list[dict[str, Any]] = field(default_factory=list)  # compact artifact views
    context_text: str = ""  # extra text (user input, file excerpts) chosen by the orchestrator
    input_artifact_ids: list[str] = field(default_factory=list)
    persistent_key: str | None = None
    attempt: int = 1
    temperature: float | None = None
    time_budget_s: float | None = None
    exclude_models: set[str] = field(default_factory=set)
    scope: PermissionScope = field(default_factory=PermissionScope)
    think: bool | None = None
    max_tool_rounds: int | None = None
    role_label: str = ""  # human-friendly name for the TUI
    project_dir: str | None = None  # where file/code tools operate for this task
    id: str = field(default_factory=lambda: new_id("agent"))


@dataclass
class Agent:
    spec: AgentSpec
    status: AgentStatus = AgentStatus.CREATED
    model: str | None = None
    started_at: float | None = None
    finished_at: float | None = None
    tool_uses: list[ToolUse] = field(default_factory=list)
    artifact_id: str | None = None
    error: str | None = None
    error_kind: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    rounds: int = 0
    transcript: list[dict[str, Any]] = field(default_factory=list)  # for inspection in the TUI
    task: asyncio.Task | None = field(default=None, repr=False)
    memory_share: int = 0

    @property
    def id(self) -> str:
        return self.spec.id

    @property
    def elapsed_s(self) -> float:
        if self.started_at is None:
            return 0.0
        return (self.finished_at or time.time()) - self.started_at

    def as_dict(self, full: bool = False) -> dict[str, Any]:
        d = {
            "id": self.id,
            "task_id": self.spec.task_id,
            "node_id": self.spec.node_id,
            "capability": self.spec.capability,
            "role": self.spec.role_label or self.spec.capability,
            "status": self.status.value,
            "model": self.model,
            "tier": self.spec.tier.value if self.spec.tier else None,
            "elapsed_s": round(self.elapsed_s, 1),
            "tools": [t.tool for t in self.tool_uses],
            "tool_calls": len(self.tool_uses),
            "artifact_id": self.artifact_id,
            "error": self.error,
            "error_kind": self.error_kind,
            "attempt": self.spec.attempt,
            "persistent": bool(self.spec.persistent_key),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "memory_share": self.memory_share,
        }
        if full:
            d["instruction"] = self.spec.instruction
            d["context"] = self.spec.context
            d["context_text"] = self.spec.context_text
            d["allowed_tools"] = self.spec.tools
            d["input_artifact_ids"] = self.spec.input_artifact_ids
            d["transcript"] = self.transcript
            d["tool_uses"] = [t.model_dump() for t in self.tool_uses]
        return d


class AgentRuntime:
    def __init__(
        self,
        scheduler: ModelScheduler,
        tools: ToolRegistry,
        artifacts: ArtifactStore,
        states: AgentStateStore,
        bus: EventBus,
        workspace: Workspace,
        settings: Any,
    ) -> None:
        self.scheduler = scheduler
        self.tools = tools
        self.artifacts = artifacts
        self.states = states
        self.bus = bus
        self.workspace = workspace
        self.settings = settings
        self.agents: dict[str, Agent] = {}
        self.max_history = 300

    # --- public ---------------------------------------------------------

    def create(self, spec: AgentSpec) -> Agent:
        agent = Agent(spec=spec)
        self.agents[spec.id] = agent
        self._trim()
        self.bus.publish("agent.created", **agent.as_dict())
        return agent

    async def run(self, spec: AgentSpec) -> Artifact:
        agent = self.agents.get(spec.id) or self.create(spec)
        agent.task = asyncio.current_task()
        agent.started_at = time.time()
        try:
            artifact = await self._execute(agent)
            agent.status = AgentStatus.COMPLETED
            agent.artifact_id = artifact.id
            return artifact
        except asyncio.CancelledError:
            agent.status = AgentStatus.CANCELLED
            agent.error_kind = "cancelled"
            raise
        except AgentError as e:
            agent.status = AgentStatus.FAILED
            agent.error, agent.error_kind = str(e), e.kind
            raise
        except BackendError as e:
            agent.status = AgentStatus.FAILED
            agent.error, agent.error_kind = str(e), "backend"
            raise AgentError("backend", str(e)) from e
        except TimeoutError as e:
            agent.status = AgentStatus.FAILED
            agent.error, agent.error_kind = str(e) or "timeout", "timeout"
            raise AgentError("timeout", str(e) or "agent timed out") from e
        except Exception as e:  # noqa: BLE001
            log.exception("agent %s crashed", agent.id)
            agent.status = AgentStatus.FAILED
            agent.error, agent.error_kind = f"{type(e).__name__}: {e}", "model"
            raise AgentError("model", agent.error) from e
        finally:
            agent.finished_at = time.time()
            self.bus.publish("agent.finished", **agent.as_dict())

    def cancel(self, agent_id: str) -> bool:
        agent = self.agents.get(agent_id)
        if agent and agent.task and not agent.task.done():
            agent.task.cancel()
            return True
        return False

    def live(self) -> list[Agent]:
        return [a for a in self.agents.values() if not a.status.terminal]

    def get(self, agent_id: str) -> Agent | None:
        return self.agents.get(agent_id)

    # --- execution ------------------------------------------------------

    async def _execute(self, agent: Agent) -> Artifact:
        spec = agent.spec
        cap = get_capability(spec.capability)
        tier = spec.tier or cap.tier
        tool_names = spec.tools if spec.tools is not None else list(cap.tools)
        tool_names = self.tools.allowed_for(tool_names, spec.scope)
        req = ModelRequest(
            capability=spec.capability, tier=tier, model=spec.model,
            needs_tools=bool(tool_names) and cap.needs_tools, needs_json=True,
            exclude=set(spec.exclude_models), time_budget_s=spec.time_budget_s,
            expected_prompt_tokens=self._estimate_prompt_tokens(spec, cap),
            expected_completion_tokens=cap.expected_completion_tokens,
            purpose=f"{spec.node_id}/{spec.capability}",
        )
        agent.status = AgentStatus.WAITING_MODEL
        self.bus.publish("agent.status", id=agent.id, status=agent.status.value)
        lease = await self.scheduler.acquire(req, timeout_s=spec.time_budget_s)
        try:
            agent.model = lease.model
            agent.memory_share = lease.instance.memory // max(1, lease.instance.leases)
            agent.status = AgentStatus.RUNNING
            self.bus.publish("agent.status", id=agent.id, status=agent.status.value, model=agent.model)
            if not lease.profile.supports_tools():
                tool_names = []
            messages = self._build_messages(agent, cap, tool_names)
            think = spec.think if spec.think is not None else cap.think
            if not lease.profile.supports_thinking():
                think = None
            elif think is None:
                think = False  # thinking is slow; only on when the capability asks for it
            temperature = spec.temperature if spec.temperature is not None else cap.temperature
            rounds = spec.max_tool_rounds if spec.max_tool_rounds is not None else cap.max_tool_rounds
            if tool_names:
                messages = await self._tool_loop(agent, lease, messages, tool_names, temperature, think, rounds)
            data = await self._structured(agent, lease, messages, temperature, think)
        finally:
            lease.release()
        return self._to_artifact(agent, lease, data)

    def _estimate_prompt_tokens(self, spec: AgentSpec, cap: Any) -> int:
        chars = len(cap.system_prompt()) + len(spec.instruction) + len(spec.context_text)
        chars += len(json.dumps(spec.context))
        return chars // 4 + 200

    def _build_messages(self, agent: Agent, cap: Any, tool_names: list[str]) -> list[ChatMessage]:
        spec = agent.spec
        system = cap.system_prompt()
        if tool_names:
            system += ("\n\nTOOLS: you may call the provided tools. Call them when the task needs facts, files or "
                       "computation. After you have what you need, answer without further tool calls.")
        system += "\n\n" + OUTPUT_INSTRUCTIONS
        budget_chars = int(getattr(self.settings.orchestrator, "agent_context_tokens", 6000)) * 4
        parts = [f"TASK:\n{spec.instruction.strip()}"]
        if spec.context:
            ctx_json = json.dumps(spec.context, indent=1, ensure_ascii=False)
            if len(ctx_json) > budget_chars:
                ctx_json = ctx_json[:budget_chars] + "\n…[context truncated by runtime]"
            parts.append(f"CONTEXT ARTIFACTS (from other agents; compact views):\n{ctx_json}")
        if spec.project_dir and tool_names:
            parts.append(f"WORKING DIRECTORY: {spec.project_dir} (file paths are relative to it)")
        if spec.context_text:
            text = spec.context_text
            remaining = max(1000, budget_chars - sum(len(p) for p in parts))
            if len(text) > remaining:
                text = text[:remaining] + "\n…[truncated by runtime]"
            parts.append(f"ADDITIONAL CONTEXT:\n{text}")
        if spec.persistent_key:
            state = self.states.get(spec.persistent_key)
            if state.notes:
                parts.append(f"YOUR NOTES FROM PREVIOUS RUNS (run #{state.runs}):\n{state.notes}")
            parts.append("You are a persistent agent: include a short `notes_for_self` field with what to remember next time.")
        user = "\n\n".join(parts)
        agent.transcript.append({"role": "system", "content": system})
        agent.transcript.append({"role": "user", "content": user})
        return [ChatMessage("system", system), ChatMessage("user", user)]

    async def _tool_loop(
        self, agent: Agent, lease: Lease, messages: list[ChatMessage], tool_names: list[str],
        temperature: float, think: bool | None, max_rounds: int,
    ) -> list[ChatMessage]:
        specs = self.tools.specs(tool_names)
        from pathlib import Path

        ctx = ToolContext(workspace=self.workspace, project_dir=Path(agent.spec.project_dir) if agent.spec.project_dir else None,
                          task_id=agent.spec.task_id, node_id=agent.spec.node_id,
                          agent_id=agent.id, scope=agent.spec.scope, settings=self.settings)
        for _ in range(max_rounds):
            result = await self._chat(agent, lease, messages, tools=specs, temperature=temperature, think=think)
            agent.rounds += 1
            if not result.tool_calls:
                if result.content.strip():
                    messages.append(ChatMessage("assistant", result.content))
                    agent.transcript.append({"role": "assistant", "content": result.content})
                break
            messages.append(ChatMessage("assistant", result.content, tool_calls=result.tool_calls))
            agent.transcript.append({"role": "assistant", "content": result.content,
                                     "tool_calls": [{"name": t.name, "arguments": t.arguments} for t in result.tool_calls]})
            for call in result.tool_calls[:6]:
                agent.status = AgentStatus.TOOL_CALL
                self.bus.publish("agent.status", id=agent.id, status=agent.status.value, tool=call.name)
                if call.name not in tool_names:
                    tr_text = f"ERROR: tool {call.name!r} is not available to you. Available: {tool_names}"
                    ok = False
                    duration = 0.0
                    sources: list[dict[str, Any]] = []
                else:
                    tr = await self.tools.invoke(call.name, call.arguments, ctx)
                    tr_text, ok, duration, sources = tr.as_model_text(), tr.ok, tr.duration_s, tr.sources
                agent.tool_uses.append(ToolUse(tool=call.name, arguments=call.arguments, ok=ok,
                                               duration_s=round(duration, 2), summary=tr_text[:200]))
                agent.transcript.append({"role": "tool", "name": call.name, "content": tr_text[:4000], "sources": sources})
                messages.append(ChatMessage("tool", tr_text, tool_name=call.name))
                agent.status = AgentStatus.RUNNING
        else:
            messages.append(ChatMessage("user", "Tool budget exhausted. Answer now with what you have."))
        return messages

    async def _structured(
        self, agent: Agent, lease: Lease, messages: list[ChatMessage], temperature: float, think: bool | None
    ) -> dict[str, Any]:
        msgs = list(messages)
        if agent.rounds > 0:
            msgs.append(ChatMessage("user", "Now return your final result as the JSON object described in the instructions."))
        for attempt in range(2):
            result = await self._chat(agent, lease, msgs, json_schema=ARTIFACT_SCHEMA, temperature=temperature, think=think)
            agent.transcript.append({"role": "assistant", "content": result.content, "structured": True})
            data = parse_json_object(result.content)
            if data is not None and "conclusion" in data:
                return data
            if attempt == 0:
                msgs.append(ChatMessage("assistant", result.content))
                msgs.append(ChatMessage("user", "That was not valid JSON matching the required fields. Return only the JSON object."))
        # Salvage whatever prose came back rather than losing the work entirely.
        text = result.content.strip()
        if not text:
            raise AgentError("format", "model returned no usable output")
        prof = lease.profile
        prof.stats(agent.spec.capability).json_failures += 1
        self.scheduler.profiles.save(prof)
        return {"conclusion": text[:2000], "confidence": 0.3, "evidence": [],
                "reasoning_summary": "unstructured output salvaged by runtime", "unresolved": ["model did not return structured output"]}

    async def _chat(self, agent: Agent, lease: Lease, messages: list[ChatMessage], **kw: Any) -> ChatResult:
        try:
            result = await lease.chat(messages, **kw)
        except BackendError as e:
            msg = str(e).lower()
            kind = "resource" if ("memory" in msg or "cuda" in msg or "oom" in msg) else "backend"
            raise AgentError(kind, str(e)) from e
        agent.prompt_tokens += result.prompt_tokens
        agent.completion_tokens += result.completion_tokens
        return result

    def _to_artifact(self, agent: Agent, lease: Lease, data: dict[str, Any]) -> Artifact:
        spec = agent.spec
        evidence: list[Evidence] = []
        fetched = {s.get("url") or s.get("path"): s for tu in agent.transcript if tu.get("role") == "tool" for s in tu.get("sources", [])}
        for raw in data.get("evidence") or []:
            if not isinstance(raw, dict) or not raw.get("claim"):
                continue
            src = raw.get("source") or None
            stype = raw.get("source_type") or ("web" if src and str(src).startswith("http") else "model")
            if stype not in ("web", "file", "tool", "model", "user", "artifact"):
                stype = "model"
            q = raw.get("quality")
            try:
                quality = min(1.0, max(0.0, float(q))) if q is not None else (0.7 if src else 0.4)
            except (TypeError, ValueError):
                quality = 0.5
            retrieved = fetched.get(src, {}).get("retrieved_at") if src else None
            if stype == "web" and not retrieved and src:
                retrieved = now_iso()
            evidence.append(Evidence(claim=str(raw["claim"])[:600], source=str(src)[:500] if src else None,
                                     source_type=stype, quote=(str(raw.get("quote"))[:400] if raw.get("quote") else None),
                                     quality=quality, supports=bool(raw.get("supports", True)), retrieved_at=retrieved))
        cited = {e.source for e in evidence if e.source}
        for key, s in fetched.items():
            if key and key not in cited and str(key).startswith("http"):
                evidence.append(Evidence(claim=f"consulted: {s.get('title') or key}", source=str(key), source_type="web",
                                         quality=0.3, retrieved_at=s.get("retrieved_at")))
        try:
            confidence = min(1.0, max(0.0, float(data.get("confidence", 0.5))))
        except (TypeError, ValueError):
            confidence = 0.5
        if agent.tool_uses and all(not t.ok for t in agent.tool_uses):
            confidence = min(confidence, 0.4)
        r = None
        artifact = Artifact(
            kind="result",
            title=(spec.role_label or spec.capability)[:80],
            conclusion=str(data.get("conclusion", ""))[:6000],
            confidence=confidence,
            evidence=evidence[:40],
            reasoning_summary=str(data.get("reasoning_summary", ""))[:3000],
            contradictions=[str(c)[:500] for c in (data.get("contradictions") or []) if c][:20],
            unresolved=[str(u)[:500] for u in (data.get("unresolved") or []) if u][:20],
            next_action=str(data.get("next_action") or "")[:500],
            content=str(data.get("content") or ""),
            model=ModelInfo(backend=lease.profile.backend, model=lease.model, tier=(spec.tier or get_capability(spec.capability).tier).value,
                            prompt_tokens=agent.prompt_tokens, completion_tokens=agent.completion_tokens,
                            tokens_per_s=round(lease.profile.tokens_per_s or 0, 1)),
            execution_time_s=round(agent.elapsed_s, 2),
            tools=list(agent.tool_uses),
            provenance=Provenance(task_id=spec.task_id, node_id=spec.node_id, agent_id=agent.id,
                                  capability=spec.capability, attempt=spec.attempt, inputs=list(spec.input_artifact_ids)),
            tags=[spec.capability],
        )
        self.artifacts.put(artifact)
        if spec.persistent_key:
            state = self.states.get(spec.persistent_key)
            notes = str(data.get("notes_for_self") or "").strip() or state.notes
            state.remember(notes, artifact.id)
            self.states.save(state)
        del r
        return artifact

    def _trim(self) -> None:
        if len(self.agents) <= self.max_history:
            return
        finished = [a for a in self.agents.values() if a.status.terminal]
        finished.sort(key=lambda a: a.finished_at or 0)
        for a in finished[: len(self.agents) - self.max_history]:
            self.agents.pop(a.id, None)


_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def parse_json_object(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if not text:
        return None
    for candidate in (text, *(m.group(1) for m in _JSON_BLOCK.finditer(text))):
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(text[start : end + 1])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            return None
    return None
