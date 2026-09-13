"""The persistent top-level orchestrator.

It is the only component that creates, removes or restructures agents. For
each task it plans (or loads) a workflow, runs the DAG through the engine,
executes every node by spawning agents from the capability library,
evaluates their artifacts with evidence-weighted consensus, escalates or
adds work when evidence does not converge, recovers from failures with a
diagnosis rather than a blind retry, and finishes with a cited final
artifact. It normally runs on a FAST model and escalates itself only for
hard planning or judgement calls.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from swarm.agents.runtime import AgentError, AgentRuntime, AgentSpec
from swarm.artifacts.model import Artifact, Evidence, Provenance
from swarm.artifacts.store import ArtifactStore
from swarm.capabilities.library import get_capability
from swarm.config import Settings
from swarm.core.db import Database
from swarm.core.events import EventBus
from swarm.core.types import ExecutionMode, NodeStatus, TaskStatus, Tier
from swarm.hardware.telemetry import HardwareMonitor
from swarm.memory.learning import WorkflowKnowledge
from swarm.memory.store import FailureLog, WorldKnowledge
from swarm.models.router import ModelRequest
from swarm.models.scheduler import ModelScheduler
from swarm.orchestrator.assist import CLARIFY_SCHEMA, Assistant, LLMGrouper, make_digester
from swarm.orchestrator.consensus import ConsensusResult, evaluate_consensus
from swarm.orchestrator.context import ContextBuilder
from swarm.orchestrator.planner import Planner
from swarm.orchestrator.recovery import FailureContext, diagnose
from swarm.paths import Workspace
from swarm.permissions.approvals import ApprovalBroker
from swarm.permissions.policy import PermissionScope
from swarm.tasks.queue import Question, Task, TaskQueue, TaskStore
from swarm.tools.registry import ToolRegistry
from swarm.workflow.dag import DAG, NodeSpec, WorkflowSpec
from swarm.workflow.engine import Engine, ExecutionControl
from swarm.workflow.library import WorkflowLibrary

log = logging.getLogger(__name__)


def citable_sources(artifacts: list[Artifact], limit: int = 15) -> dict[str, Evidence]:
    """External sources worth citing: real URLs or paths attached to supporting claims,
    best evidence first. Pages an agent merely opened ("consulted") only count when
    nothing better exists."""
    cited: dict[str, Evidence] = {}
    consulted: dict[str, Evidence] = {}
    for a in artifacts:
        for e in a.evidence:
            src = (e.source or "").strip()
            if not src or e.source_type not in ("web", "file") or not e.supports:
                continue
            if "://" not in src and "/" not in src and "." not in src:
                continue  # "Wikipedia page" is not a citation
            bucket = consulted if e.claim.lower().startswith("consulted:") else cited
            if src not in bucket or e.quality > bucket[src].quality:
                bucket[src] = e
    ranked = sorted(cited.values(), key=lambda e: e.quality, reverse=True)
    if not ranked:
        ranked = sorted(consulted.values(), key=lambda e: e.quality, reverse=True)
    return {e.source: e for e in ranked[:limit] if e.source}


@dataclass
class TaskRun:
    task: Task
    dag: DAG
    control: ExecutionControl = field(default_factory=ExecutionControl)
    started: float = field(default_factory=time.time)
    hurry: bool = False  # past the soft timeout: cut optional work
    extended: bool = False  # one automatic post-hoc extension allowed
    node_agents: dict[str, list[str]] = field(default_factory=dict)
    models: set[str] = field(default_factory=set)
    answers: list[str] = field(default_factory=list)
    runner: asyncio.Task | None = None
    engine_task: asyncio.Task | None = None

    @property
    def elapsed(self) -> float:
        return time.time() - self.started


class Orchestrator:
    def __init__(
        self,
        settings: Settings,
        bus: EventBus,
        scheduler: ModelScheduler,
        runtime: AgentRuntime,
        tools: ToolRegistry,
        approvals: ApprovalBroker,
        artifacts: ArtifactStore,
        db: Database,
        workspace: Workspace,
        monitor: HardwareMonitor,
    ) -> None:
        self.settings = settings
        self.bus = bus
        self.scheduler = scheduler
        self.runtime = runtime
        self.tools = tools
        self.approvals = approvals
        self.artifacts = artifacts
        self.db = db
        self.workspace = workspace
        self.monitor = monitor
        self.tasks = TaskQueue(TaskStore(db))
        self.failures = FailureLog(db)
        self.world = WorldKnowledge(db)
        self.learning = WorkflowKnowledge(db)
        self.workflows = WorkflowLibrary(workspace.workflows)
        oc = settings.orchestrator
        self.assistant = Assistant(scheduler, tier=oc.tier, model=settings.tiers.pinned(oc.tier))
        self.planner = Planner(self.assistant, self.learning, max_redundancy=oc.max_redundancy,
                               escalation_tier=oc.escalation_tier)
        self.engine = Engine(bus)
        self.context = ContextBuilder(artifacts, make_digester(self.assistant))
        self.grouper = LLMGrouper(self.assistant)
        self.runs: dict[str, TaskRun] = {}
        self.max_parallel_tasks = 4
        self._admission: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._stopping = False

    # --- lifecycle ------------------------------------------------------

    async def start(self) -> None:
        interrupted = self.tasks.store.mark_interrupted()
        if interrupted:
            self.bus.publish("orchestrator.interrupted_tasks", tasks=interrupted)
        self._admission = asyncio.create_task(self._admission_loop(), name="orchestrator-admission")
        self.bus.publish("orchestrator.started")

    async def stop(self) -> None:
        self._stopping = True
        if self._admission:
            self._admission.cancel()
            try:
                await self._admission
            except asyncio.CancelledError:
                pass
        for run in list(self.runs.values()):
            run.control.cancel()
            if run.runner:
                run.runner.cancel()
        await asyncio.gather(*(r.runner for r in self.runs.values() if r.runner), return_exceptions=True)

    # --- public API -----------------------------------------------------

    def submit(self, objective: str, *, mode: ExecutionMode | None = None, workflow: str | None = None,
               overrides: dict[str, Any] | None = None, priority: int = 0, cwd: str | None = None) -> Task:
        objective = objective.strip()
        if not objective:
            raise ValueError("objective is empty")
        task = Task(objective=objective, mode=mode or self.settings.orchestrator.default_mode,
                    workflow_slug=workflow, overrides=overrides or {}, priority=priority, cwd=cwd)
        if workflow:
            spec = self.workflows.load(workflow)  # raises if missing
            task.objective_class = spec.objective_class
        self.tasks.add(task)
        self._wake.set()
        self.bus.publish("task.submitted", task_id=task.id, objective=objective, mode=task.mode.value, workflow=workflow)
        return task

    def pause(self, task_id: str) -> bool:
        run = self.runs.get(task_id)
        if not run:
            return False
        run.control.pause()
        run.task.status = TaskStatus.PAUSED
        run.task.interventions.append({"ts": time.time(), "action": "pause"})
        self.tasks.save(run.task)
        self.bus.publish("task.pause_requested", task_id=task_id)
        return True

    def resume(self, task_id: str) -> bool:
        run = self.runs.get(task_id)
        if not run:
            return False
        run.control.unpause()
        if run.task.status == TaskStatus.PAUSED:
            run.task.status = TaskStatus.RUNNING
        run.task.interventions.append({"ts": time.time(), "action": "resume"})
        self.tasks.save(run.task)
        self.bus.publish("task.resumed", task_id=task_id)
        return True

    def cancel(self, task_id: str) -> bool:
        run = self.runs.get(task_id)
        task = self.tasks.get(task_id)
        if run:
            run.control.cancel()
            for agent_ids in run.node_agents.values():
                for aid in agent_ids:
                    self.runtime.cancel(aid)
            run.task.interventions.append({"ts": time.time(), "action": "cancel"})
            if run.runner:
                run.runner.cancel()
            return True
        if task and task.status == TaskStatus.QUEUED:
            task.status = TaskStatus.CANCELLED
            task.finished_at = time.time()
            self.tasks.save(task)
            self.bus.publish("task.cancelled", task_id=task_id)
            return True
        return False

    def answer(self, task_id: str, question_id: str, answer: str) -> bool:
        task = self.tasks.get(task_id)
        if not task:
            return False
        for q in task.questions:
            if q.id == question_id and q.answer is None:
                q.answer = answer
                q.answered_at = time.time()
                self.tasks.save(task)
                self.bus.publish("task.answered", task_id=task_id, question_id=question_id)
                return True
        return False

    def retry_node(self, task_id: str, node_id: str) -> bool:
        run = self.runs.get(task_id)
        if not run or node_id not in run.dag.states:
            return False
        st = run.dag.states[node_id]
        if st.status not in (NodeStatus.FAILED, NodeStatus.SKIPPED, NodeStatus.CANCELLED):
            return False
        st.status = NodeStatus.PENDING
        st.error = st.error_kind = None
        st.finished_at = None
        st.notes.append("manual retry")
        run.task.interventions.append({"ts": time.time(), "action": "retry", "node": node_id})
        # Un-skip dependants so they can run again.
        for nid, s in run.dag.states.items():
            if s.status == NodeStatus.SKIPPED and node_id in run.dag.nodes[nid].depends_on:
                s.status = NodeStatus.PENDING
                s.error = None
        self.bus.publish("node.retry", task_id=task_id, node_id=node_id, by="user")
        return True

    def reassign_model(self, task_id: str, node_id: str, model: str | None, tier: str | None = None) -> bool:
        run = self.runs.get(task_id)
        if not run or node_id not in run.dag.nodes:
            return False
        node = run.dag.nodes[node_id]
        node.model = model or None
        if tier:
            node.tier = Tier(tier)
        st = run.dag.states[node_id]
        st.notes.append(f"user reassigned model={model} tier={tier}")
        run.task.interventions.append({"ts": time.time(), "action": "reassign", "node": node_id, "model": model, "tier": tier})
        # Learn conservatively from the intervention: the model the user moved away from
        # gets one failure for this capability, which nudges future routing.
        previous = {self.runtime.get(aid).model for aid in run.node_agents.get(node_id, []) if self.runtime.get(aid) and self.runtime.get(aid).model}
        for prev_model in previous:
            if prev_model != model:
                prof = self.scheduler.profiles.get(prev_model)
                if prof:
                    prof.record_call(node.capability, ok=False, duration_s=0.0, ts=time.time())
                    self.scheduler.profiles.save(prof)
                self.failures.record(task_id=task_id, node_id=node_id, model=prev_model, capability=node.capability,
                                     kind="user_reassign", action="reassign", message=f"user moved node to model={model} tier={tier}")
        if st.status in (NodeStatus.RUNNING, NodeStatus.CONSENSUS):
            for aid in run.node_agents.get(node_id, []):
                self.runtime.cancel(aid)
        elif st.status.terminal:
            self.retry_node(task_id, node_id)
        self.bus.publish("node.reassigned", task_id=task_id, node_id=node_id, model=model, tier=tier)
        return True

    def set_ceiling(self, percent: float) -> None:
        percent = max(10.0, min(95.0, float(percent)))
        self.settings.hardware.memory_ceiling_percent = percent
        self.scheduler.budget.ceiling_percent = percent
        self.bus.publish("settings.ceiling", percent=percent)

    # --- admission ------------------------------------------------------

    def can_admit(self) -> bool:
        running = [r for r in self.runs.values() if not r.task.status.terminal]
        if len(running) >= self.max_parallel_tasks:
            return False
        if not running:
            return True
        snap = self.scheduler.snapshot()
        budget = snap["budget"]
        smallest = min((p.estimate_memory(self.scheduler.default_ctx, self.scheduler.parallel)
                        for p in self.scheduler.router.by_tier(Tier.FAST)), default=0)
        free_slots = any(i["state"] == "ready" and i["active"] < self.scheduler.parallel for i in snap["instances"])
        return budget["headroom"] >= smallest or free_slots

    async def _admission_loop(self) -> None:
        while True:
            try:
                for task in self.tasks.queued():
                    if not self.can_admit():
                        break
                    self._start(task)
            except Exception:  # noqa: BLE001
                log.exception("admission loop error")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=1.0)
            except TimeoutError:
                pass

    def _start(self, task: Task) -> TaskRun:
        run = TaskRun(task=task, dag=DAG(WorkflowSpec(name="pending", nodes=[NodeSpec(id="_")], immutable=False)))
        self.runs[task.id] = run
        run.runner = asyncio.create_task(self._run_task(run), name=f"task-{task.id}")
        return run

    # --- task lifecycle -------------------------------------------------

    def can_start_node(self, run: TaskRun) -> bool:
        """Hardware gate for starting another node: let the scheduler queue inside a task,
        but do not fan out more nodes when nothing could get a model anyway."""
        snap = self.scheduler.snapshot()
        active_nodes = sum(1 for s in run.dag.states.values() if s.status in (NodeStatus.RUNNING, NodeStatus.CONSENSUS))
        if active_nodes == 0:
            return True
        waiting = snap["waiting"]
        return waiting < max(2, snap["inference_cap"])

    async def _run_task(self, run: TaskRun) -> None:
        task = run.task
        task.status = TaskStatus.PLANNING
        task.started_at = time.time()
        self.tasks.save(task)
        self.bus.publish("task.planning", task_id=task.id)
        try:
            spec = await self._prepare_spec(run)
            run.dag = DAG(spec)
            task.spec = spec
            task.status = TaskStatus.RUNNING
            self._persist(run)
            self.bus.publish("task.started", task_id=task.id, nodes=len(spec.nodes), workflow=spec.name,
                             mutable=run.dag.mutable, objective_class=task.objective_class)
            ok = await self._execute(run)
            if ok and run.dag.mutable and not run.extended and not run.hurry:
                if await self._maybe_extend(run):
                    ok = await self._execute(run)
            if run.control.cancelled.is_set():
                task.status = TaskStatus.CANCELLED
            elif ok:
                final = await self._finalize(run)
                task.result_artifact_id = final.id
                task.summary = final.conclusion[:300]
                task.status = TaskStatus.COMPLETED
            else:
                task.status = TaskStatus.FAILED
                failed = [f"{nid}: {s.error}" for nid, s in run.dag.states.items() if s.status == NodeStatus.FAILED]
                task.error = "; ".join(failed)[:500] or "workflow did not complete"
        except asyncio.CancelledError:
            task.status = TaskStatus.CANCELLED
        except Exception as e:  # noqa: BLE001
            log.exception("task %s crashed", task.id)
            task.status = TaskStatus.FAILED
            task.error = f"{type(e).__name__}: {e}"
        finally:
            task.finished_at = time.time()
            task.models_used = sorted(run.models)
            self._persist(run)
            self.approvals.forget_task(task.id)
            self._learn(run)
            self.runs.pop(task.id, None)
            self._wake.set()
            self.bus.publish("task.finished", task_id=task.id, status=task.status.value, elapsed_s=round(task.elapsed_s, 1),
                             error=task.error, result=task.result_artifact_id)

    async def _execute(self, run: TaskRun) -> bool:
        watchdog = asyncio.create_task(self._watchdog(run))
        try:
            return await self.engine.execute(run.dag, lambda n: self._run_node(run, n), task_id=run.task.id,
                                             control=run.control, can_start=lambda n: self.can_start_node(run))
        finally:
            watchdog.cancel()

    async def _watchdog(self, run: TaskRun) -> None:
        oc = self.settings.orchestrator
        while True:
            await asyncio.sleep(2.0)
            self._persist(run)
            if not run.hurry and run.elapsed > oc.task_soft_timeout_s:
                run.hurry = True
                self.bus.publish("task.hurry", task_id=run.task.id, elapsed_s=round(run.elapsed))
                for nid, st in run.dag.states.items():
                    if st.status == NodeStatus.PENDING:
                        run.dag.nodes[nid].redundancy = 1
            if run.elapsed > oc.task_hard_timeout_s:
                self.bus.publish("task.hard_timeout", task_id=run.task.id)
                run.control.cancel()
                return

    def _persist(self, run: TaskRun) -> None:
        run.task.node_states = {nid: st.model_dump(mode="json") for nid, st in run.dag.states.items()}
        self.tasks.save(run.task)

    # --- planning -------------------------------------------------------

    async def _prepare_spec(self, run: TaskRun) -> WorkflowSpec:
        task = run.task
        if task.workflow_slug:
            spec = self.workflows.load(task.workflow_slug)
            spec = spec.model_copy(deep=True)
            spec.immutable = True
            if spec.mode and task.mode is None:
                task.mode = spec.mode
            task.objective_class = spec.objective_class
            return spec
        if task.mode == ExecutionMode.INTERACTIVE:
            await self._clarify(run)
        spec, meta = await self.planner.plan(task.objective + self._answers_text(run), task.overrides)
        task.objective_class = spec.objective_class
        task.complexity = meta.get("complexity", "")
        self.bus.publish("task.planned", task_id=task.id, source=meta.get("source"), objective_class=spec.objective_class,
                         nodes=[f"{n.id}:{n.capability}x{n.redundancy}" for n in spec.nodes], hint=meta.get("hint"),
                         escalated=meta.get("escalated", False))
        return spec

    async def _clarify(self, run: TaskRun) -> None:
        task = run.task
        data = await self.assistant.ask_json(
            "Decide whether an objective is too ambiguous to act on without asking the user one question. "
            "Most objectives are fine; only ask when different readings would produce materially different work.",
            f"OBJECTIVE: {task.objective}\n\nReturn {{needs_clarification, question, options, assumption}}.",
            CLARIFY_SCHEMA, purpose="clarify", max_tokens=300)
        if not data or not data.get("needs_clarification") or not data.get("question"):
            return
        await self._ask_user(run, str(data["question"]), [str(o) for o in data.get("options") or []][:4])

    async def _ask_user(self, run: TaskRun, text: str, options: list[str], node_id: str | None = None) -> str | None:
        task = run.task
        q = Question(text=text, options=options, node_id=node_id)
        task.questions.append(q)
        prev = task.status
        task.status = TaskStatus.WAITING_USER
        self.tasks.save(task)
        self.bus.publish("task.question", task_id=task.id, question_id=q.id, text=text, options=options)
        while q.answer is None:
            if run.control.cancelled.is_set():
                return None
            await asyncio.sleep(0.5)
        task.status = prev if prev != TaskStatus.WAITING_USER else TaskStatus.RUNNING
        run.answers.append(f"Q: {text}\nA: {q.answer}")
        self.tasks.save(task)
        return q.answer

    def _answers_text(self, run: TaskRun) -> str:
        return ("\n\nUser clarifications:\n" + "\n".join(run.answers)) if run.answers else ""

    # --- node execution -------------------------------------------------

    def _scope(self, run: TaskRun, node: NodeSpec) -> PermissionScope:
        return PermissionScope(task_id=run.task.id, workflow_overrides=dict(run.dag.spec.permissions),
                               node_overrides=dict(node.permissions),
                               node_allowed_tools=set(node.tools) if node.tools is not None else None)

    def _assign_models(self, node: NodeSpec, n: int, tier: Tier, exclude: set[str]) -> list[str | None]:
        """Pick distinct models for redundant agents when the tier offers them."""
        if node.model or n == 1 or not node.independent_models:
            return [node.model] * n
        snap = self.scheduler.snapshot()
        loaded = {i["name"] for i in snap["instances"] if i["state"] == "ready"}
        cands = self.scheduler.router.candidates(
            ModelRequest(capability=node.capability, tier=tier, needs_tools=get_capability(node.capability).needs_tools,
                         exclude=exclude),
            loaded=loaded, headroom=snap["budget"]["headroom"], num_ctx=self.scheduler.default_ctx,
            parallel=self.scheduler.parallel)
        names = [c.profile.name for c in cands if c.score >= cands[0].score - 1.2] if cands else []
        if len(names) < 2:
            return [None] * n
        return [names[i % len(names)] for i in range(n)]

    async def _run_node(self, run: TaskRun, node: NodeSpec) -> None:
        task, dag = run.task, run.dag
        st = dag.states[node.id]
        cap = get_capability(node.capability)
        tier = node.tier or cap.tier
        model_pin = node.model
        tools = node.tools
        context_scale = 1.0
        time_scale = 1.0
        exclude: set[str] = set()
        max_attempts = node.max_attempts
        attempt = 0
        while attempt < max_attempts:
            attempt += 1
            st.attempts = attempt
            if run.control.cancelled.is_set():
                st.status = NodeStatus.CANCELLED
                return
            await run.control.resume.wait()
            redundancy = 1 if run.hurry else node.redundancy
            context, used_ids, context_text = await self._context_for(run, node, context_scale)
            budget = (node.time_budget_s or self.settings.orchestrator.node_timeout_s) * time_scale
            models = self._assign_models(node, redundancy, tier, exclude) if not model_pin else [model_pin] * redundancy
            specs = [
                AgentSpec(
                    task_id=task.id, node_id=node.id, capability=node.capability,
                    instruction=node.instruction.replace("{objective}", task.objective) + self._answers_text(run),
                    tier=tier, model=models[i], tools=tools, context=context, context_text=context_text,
                    input_artifact_ids=used_ids, persistent_key=f"{task.workflow_slug or task.id}:{node.id}" if node.persistent else None,
                    attempt=attempt, temperature=node.temperature, time_budget_s=budget,
                    exclude_models=set(exclude) | {m for m in models[:i] if m} if node.independent_models else set(exclude),
                    scope=self._scope(run, node), think=node.think, max_tool_rounds=node.max_tool_rounds,
                    role_label=f"{node.label}" + (f" #{i + 1}" if redundancy > 1 else ""),
                    project_dir=task.cwd,
                )
                for i in range(redundancy)
            ]
            artifacts, errors = await self._spawn(run, node, specs)
            if run.control.cancelled.is_set():
                st.status = NodeStatus.CANCELLED
                return
            if artifacts:
                result = await self._resolve(run, node, artifacts, tier, exclude)
                if result is None:
                    st.status = NodeStatus.CANCELLED
                    return
                st.result_artifact_id = result.id
                st.status = NodeStatus.COMPLETED
                for a in artifacts:
                    if a.model:
                        run.models.add(a.model.model)
                return
            # Everything failed: diagnose the dominant failure and adapt.
            err = errors[0]
            agent = self.runtime.get(specs[0].id)
            fctx = FailureContext(
                kind=err.kind, message=str(err), attempt=attempt, max_attempts=max_attempts, critical=node.critical,
                model=agent.model if agent else None, tier=tier, tools_used=len(agent.tool_uses) if agent else 0,
                tools_failed=sum(1 for t in agent.tool_uses if not t.ok) if agent else 0,
                context_chars=len(context_text) + len(str(context)), tried_models=set(exclude),
                alternatives=max(0, len(self.scheduler.router.by_tier(tier)) - len(exclude) - 1),
                pinned_model=bool(model_pin),
            )
            d = diagnose(fctx)
            st.notes.append(f"attempt {attempt}: {err.kind} -> {d.action}: {d.note}")
            self.failures.record(task_id=task.id, node_id=node.id, model=fctx.model, capability=node.capability,
                                 kind=err.kind, action=d.action, message=str(err))
            self.bus.publish("node.recovery", task_id=task.id, node_id=node.id, attempt=attempt, kind=err.kind, **d.as_dict())
            if d.action in ("give_up", "skip"):
                break
            if d.action == "split" and dag.mutable:
                if await self._split_node(run, node, err):
                    st.status = NodeStatus.SKIPPED
                    st.error = "split into sub-nodes"
                    node.critical = False
                    return
                break
            if d.action == "wait":
                await asyncio.sleep(d.wait_s)
            elif d.action == "switch_model":
                exclude |= set(d.changes.get("exclude", set()))
                if "tier" in d.changes:
                    tier = d.changes["tier"]
                    model_pin = None
            elif d.action == "escalate_tier":
                tier = d.changes["tier"]
                model_pin = None
            elif d.action == "reduce_context":
                context_scale *= d.changes.get("context_scale", 0.5)
            elif d.action == "drop_tools":
                tools = []
            elif d.action == "retry":
                time_scale *= d.changes.get("time_scale", 1.0)
            if d.action == "wait" and attempt >= max_attempts:
                max_attempts += 1  # a wait is not a real attempt
        # Out of attempts.
        last = st.notes[-1] if st.notes else "failed"
        if node.critical:
            st.status = NodeStatus.FAILED
            st.error = last
        else:
            st.status = NodeStatus.SKIPPED
            st.error = last
        if task.mode == ExecutionMode.INTERACTIVE and node.critical and dag.mutable:
            ans = await self._ask_user(run, f"Node '{node.label}' failed ({last}). Retry it once more or stop the task?",
                                       ["retry", "stop"], node_id=node.id)
            if ans == "retry":
                st.status = NodeStatus.PENDING
                st.error = None
                st.notes.append("user asked for another attempt")

    async def _spawn(self, run: TaskRun, node: NodeSpec, specs: list[AgentSpec]) -> tuple[list[Artifact], list[AgentError]]:
        for s in specs:
            self.runtime.create(s)
        run.node_agents.setdefault(node.id, []).extend(s.id for s in specs)
        run.dag.states[node.id].agent_ids.extend(s.id for s in specs)
        results = await asyncio.gather(*(self.runtime.run(s) for s in specs), return_exceptions=True)
        artifacts: list[Artifact] = []
        errors: list[AgentError] = []
        for r in results:
            if isinstance(r, Artifact):
                artifacts.append(r)
                run.dag.states[node.id].artifact_ids.append(r.id)
            elif isinstance(r, AgentError):
                errors.append(r)
            elif isinstance(r, asyncio.CancelledError):
                errors.append(AgentError("cancelled", "cancelled"))
            elif isinstance(r, BaseException):
                errors.append(AgentError("model", f"{type(r).__name__}: {r}"))
        return artifacts, errors

    async def _context_for(self, run: TaskRun, node: NodeSpec, scale: float) -> tuple[list[dict[str, Any]], list[str], str]:
        budget = int(self.settings.orchestrator.agent_context_tokens * scale)
        ids = run.dag.inputs_for(node.id)
        views, used = await self.context.build(ids, token_budget=budget, task_id=run.task.id, node_id=node.id,
                                               objective=run.task.objective)
        text_parts: list[str] = []
        if not node.depends_on:
            facts = self.world.search(run.task.objective, limit=5)
            if facts:
                text_parts.append("Facts recorded from earlier tasks (verify before relying on them):\n" + "\n".join(
                    f"- {f.statement} (source: {f.source}, retrieved {f.retrieved_at})" for f in facts))
        return views, used, "\n\n".join(text_parts)

    # --- consensus and escalation ----------------------------------------

    async def _resolve(self, run: TaskRun, node: NodeSpec, artifacts: list[Artifact], tier: Tier, exclude: set[str]) -> Artifact | None:
        st = run.dag.states[node.id]
        rule = node.consensus
        if rule.mode == "single" or (len(artifacts) == 1 and node.redundancy == 1 and rule.mode != "evidence"):
            return artifacts[0]
        st.status = NodeStatus.CONSENSUS
        all_arts = list(artifacts)
        extra_used = 0
        result: ConsensusResult | None = None
        for round_ in range(1, self.settings.orchestrator.max_consensus_rounds + 1):
            result = await evaluate_consensus(all_arts, rule, self.grouper, task_id=run.task.id, node_id=node.id, round_=round_)
            self.artifacts.put(result.merged)
            st.consensus = result.as_dict()
            self.bus.publish("node.consensus", task_id=run.task.id, node_id=node.id, round=round_, status=result.status,
                             reason=result.reason, positions=len(result.positions), confidence=result.merged.confidence)
            if not result.needs_more or run.hurry or run.control.cancelled.is_set():
                break
            budget_left = rule.max_extra_agents - extra_used
            if budget_left <= 0 or node.redundancy == 1 and result.status == "single" and round_ > 1:
                break
            # More independent investigation: new models where possible, one tier up when evidence is weak.
            used_models = {a.model.model for a in all_arts if a.model}
            n_extra = min(budget_left, 2 if result.status == "disagreement" else 1)
            extra_tier = tier.up() if result.status in ("weak", "single") and rule.escalate else tier
            context, used_ids, context_text = await self._context_for(run, node, 1.0)
            positions = "\n".join(f"- position ({p.support:.2f} support, evidence {p.evidence_score:.2f}): {p.best.conclusion[:400]}"
                                  for p in result.positions)
            specs = [
                AgentSpec(
                    task_id=run.task.id, node_id=node.id, capability=node.capability,
                    instruction=node.instruction.replace("{objective}", run.task.objective) + self._answers_text(run)
                    + f"\n\nPrevious agents reached these positions; investigate independently and resolve with evidence:\n{positions}",
                    tier=extra_tier, model=None, tools=node.tools, context=context, context_text=context_text,
                    input_artifact_ids=used_ids, attempt=round_ + 1, time_budget_s=node.time_budget_s or self.settings.orchestrator.node_timeout_s,
                    exclude_models=used_models | exclude, scope=self._scope(run, node), think=node.think,
                    role_label=f"{node.label} extra #{extra_used + i + 1}", project_dir=run.task.cwd,
                )
                for i in range(n_extra)
            ]
            extra_used += n_extra
            self.bus.publish("node.more_agents", task_id=run.task.id, node_id=node.id, count=n_extra, tier=extra_tier.value, reason=result.reason)
            more, _ = await self._spawn(run, node, specs)
            if run.control.cancelled.is_set():
                return None
            if not more:
                break
            all_arts.extend(more)
        assert result is not None
        merged = result.merged
        if result.needs_more and rule.escalate and not run.hurry and result.status == "disagreement":
            arbiter = await self._arbitrate(run, node, all_arts, result)
            if arbiter is not None:
                merged = arbiter
                st.consensus["arbiter"] = arbiter.id
        if result.needs_more and run.task.mode == ExecutionMode.INTERACTIVE and result.status == "disagreement" and len(result.positions) > 1:
            opts = [p.best.conclusion[:80] for p in result.positions[:3]] + ["proceed with the weighted result"]
            ans = await self._ask_user(run, f"Agents disagree on '{node.label}'. Which position should the swarm adopt?", opts, node_id=node.id)
            if ans and ans != opts[-1]:
                for p in result.positions:
                    if p.best.conclusion[:80] == ans:
                        merged = merged.revise(conclusion=p.best.conclusion, confidence=max(merged.confidence, 0.7),
                                               reasoning_summary="position chosen by the user. " + merged.reasoning_summary)
                        self.artifacts.put(merged)
                        st.notes.append("user chose a position")
                        break
        return merged

    async def _arbitrate(self, run: TaskRun, node: NodeSpec, artifacts: list[Artifact], result: ConsensusResult) -> Artifact | None:
        """Escalate a persistent disagreement to a deeper model that judges the positions on evidence."""
        deep = self.settings.orchestrator.escalation_tier
        views = [a.compact(max_evidence=6, max_chars=800) for a in artifacts]
        spec = AgentSpec(
            task_id=run.task.id, node_id=node.id, capability="reasoning",
            instruction=(f"Independent agents disagree about: {node.instruction.replace('{objective}', run.task.objective)}\n"
                         "Judge the positions strictly on evidence quality, source quality and reasoning, not on how many "
                         "agents hold them. State which position is best supported (or that none is), why, and what remains open."),
            tier=deep, context=views, input_artifact_ids=[a.id for a in artifacts], attempt=99, scope=self._scope(run, node),
            time_budget_s=self.settings.orchestrator.node_timeout_s, role_label=f"{node.label} arbiter", think=True, tools=[],
        )
        self.bus.publish("node.escalated", task_id=run.task.id, node_id=node.id, tier=deep.value, reason=result.reason)
        arts, _ = await self._spawn(run, node, [spec])
        if not arts:
            return None
        a = arts[0]
        merged = a.revise(kind="consensus", parents=[x.id for x in artifacts] + [a.id],
                          reasoning_summary="arbitrated by a deeper model. " + a.reasoning_summary)
        self.artifacts.put(merged)
        run.models.add(a.model.model if a.model else "?")
        return merged

    # --- dynamic restructuring -------------------------------------------

    async def _split_node(self, run: TaskRun, node: NodeSpec, err: AgentError) -> bool:
        """Ask the orchestrator model to break a failing node into 2-3 narrower ones."""
        schema = {"type": "object", "properties": {"subtasks": {"type": "array", "items": {"type": "string"}}}, "required": ["subtasks"]}
        data = await self.assistant.ask_json(
            "Split a task that an agent could not complete into 2-3 narrower, independent subtasks.",
            f"TASK: {node.instruction.replace('{objective}', run.task.objective)}\nFAILURE: {err}", schema,
            purpose="split", max_tokens=400)
        subs = [s for s in (data or {}).get("subtasks", []) if isinstance(s, str) and s.strip()][:3]
        if len(subs) < 2:
            return False
        ids = []
        for i, s in enumerate(subs):
            nid = f"{node.id}_part{i + 1}"
            run.dag.add_node(NodeSpec(id=nid, name=f"{node.label} part {i + 1}", capability=node.capability, instruction=s + "\n\nObjective: {objective}",
                                      tier=node.tier, tools=node.tools, depends_on=list(node.depends_on), redundancy=1,
                                      max_attempts=1, critical=node.critical), reason=f"split of {node.id}")
            ids.append(nid)
        run.dag.add_node(NodeSpec(id=f"{node.id}_merge", name=f"{node.label} merge", capability="synthesis", tier=Tier.STANDARD,
                                  instruction=f"Merge the partial results into the answer for: {node.instruction}", depends_on=ids), reason=f"merge of {node.id}")
        for nid, n in run.dag.nodes.items():
            if node.id in n.depends_on and nid not in ids and nid != f"{node.id}_merge":
                n.depends_on = [d for d in n.depends_on if d != node.id] + [f"{node.id}_merge"]
        self.bus.publish("dag.mutated", task_id=run.task.id, op="split", node=node.id, into=ids)
        return True

    async def _maybe_extend(self, run: TaskRun) -> bool:
        """After a successful automatic run, add a verification pass when the result looks weak."""
        run.extended = True
        rid = run.dag.result_artifact()
        art = self.artifacts.get(rid) if rid else None
        if art is None:
            return False
        weak = art.confidence < 0.6 or len(art.unresolved) >= 3 or (art.contradictions and art.confidence < 0.75)
        if not weak:
            return False
        result_node = run.dag.spec.result_node()
        vid = f"verify_{result_node}"
        if vid in run.dag.nodes:
            return False
        run.dag.add_node(NodeSpec(id=vid, name="verify weak result", capability="verification", tier=Tier.FAST, redundancy=2,
                                  instruction="Verify the key claims and resolve the open questions in the current answer to: {objective}",
                                  depends_on=[result_node], max_attempts=1, critical=False), reason="weak final result")
        run.dag.add_node(NodeSpec(id=f"final_{result_node}", name="revised final", capability="synthesis", tier=Tier.DEEP,
                                  instruction="Produce the final answer, incorporating the verification results, for: {objective}",
                                  depends_on=[vid], context_from=[result_node, vid], max_attempts=1), reason="weak final result")
        run.dag.spec.final_node = f"final_{result_node}"
        self.bus.publish("dag.mutated", task_id=run.task.id, op="extend", reason="weak final result", confidence=art.confidence)
        return True

    # --- finalisation and learning -------------------------------------

    async def _finalize(self, run: TaskRun) -> Artifact:
        rid = run.dag.result_artifact()
        art = self.artifacts.get(rid) if rid else None
        if art is None:
            art = Artifact(kind="failure", conclusion="no result artifact", confidence=0.0,
                           provenance=Provenance(task_id=run.task.id, node_id="final"))
        sources = citable_sources(self.artifacts.ancestry(art.id))
        content = art.content or art.conclusion
        if self.settings.orchestrator.cite_sources and sources and "Sources:" not in content:
            lines = [f"[{i + 1}] {e.source}" + (f" (retrieved {e.retrieved_at[:10]})" if e.retrieved_at else "") for i, e in enumerate(sources.values())]
            content = content.rstrip() + "\n\nSources:\n" + "\n".join(lines)
        final = Artifact(
            kind="final", title=run.task.objective[:80], conclusion=art.conclusion, confidence=art.confidence,
            evidence=art.evidence, reasoning_summary=art.reasoning_summary, contradictions=art.contradictions,
            unresolved=art.unresolved, content=content, model=art.model, execution_time_s=round(run.elapsed, 1),
            data={"sources": [{"source": e.source, "retrieved_at": e.retrieved_at, "claim": e.claim} for e in sources.values()],
                  "models": sorted(run.models), "nodes": len(run.dag.nodes)},
            provenance=Provenance(task_id=run.task.id, node_id="final", capability="final", inputs=[art.id]),
            parents=[art.id], tags=["final"],
        )
        self.artifacts.put(final)
        for e in sources.values():
            if e.quality >= 0.7 and e.supports:
                self.world.add(e.claim, source=e.source, retrieved_at=e.retrieved_at,
                               confidence=min(final.confidence, e.quality), task_id=run.task.id)
        self.world.prune()
        return final

    def _learn(self, run: TaskRun) -> None:
        task = run.task
        if task.workflow_slug or task.spec is None or task.status == TaskStatus.CANCELLED:
            return
        final = self.artifacts.get(task.result_artifact_id) if task.result_artifact_id else None
        try:
            self.learning.record_outcome(
                objective_class=task.objective_class or "mixed", spec=task.spec, models=sorted(run.models),
                success=task.status == TaskStatus.COMPLETED, confidence=final.confidence if final else 0.0,
                duration_s=task.elapsed_s, task_id=task.id, expected_duration_s=self.settings.orchestrator.task_soft_timeout_s / 3)
        except Exception:  # noqa: BLE001
            log.exception("learning record failed")

    # --- views ----------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        tasks = self.tasks.recent(60)
        return {
            "tasks": [t.brief() for t in tasks],
            "runs": {tid: {"dag": r.dag.to_dict(), "elapsed_s": round(r.elapsed, 1), "hurry": r.hurry,
                           "paused": r.control.is_paused, "mode": r.task.mode.value} for tid, r in self.runs.items()},
            "agents": [a.as_dict() for a in sorted(self.runtime.agents.values(), key=lambda a: a.started_at or 0, reverse=True)[:80]],
            "scheduler": self.scheduler.snapshot(),
            "hardware": self.monitor.latest.as_dict(),
            "approvals": [r.as_dict() for r in self.approvals.pending.values()],
            "queue": {"queued": len(self.tasks.queued()), "active": len(self.runs), "can_admit": self.can_admit()},
            "orchestrator": {"tier": self.settings.orchestrator.tier.value, "model": self.assistant.model,
                             "calls": self.assistant.calls, "failures": self.assistant.failures},
            "permissions": {"profile": self.tools.resolver.profile, "overrides": {k: v.value for k, v in self.tools.resolver.overrides.items()}},
            "memory": {"facts": self.world.count(), "artifacts": self.artifacts.count(), "failures": len(self.failures.recent(1000))},
        }

    def task_detail(self, task_id: str) -> dict[str, Any] | None:
        task = self.tasks.get(task_id)
        if not task:
            return None
        run = self.runs.get(task_id)
        d = task.brief()
        d["questions"] = [q.model_dump() for q in task.questions]
        d["interventions"] = task.interventions
        d["overrides"] = task.overrides
        d["models_used"] = task.models_used
        if run:
            d["dag"] = run.dag.to_dict()
            d["hurry"] = run.hurry
        elif task.spec:
            d["dag"] = {"name": task.spec.name, "mutable": not task.spec.immutable,
                        "nodes": [{**n.model_dump(mode="json"), "state": task.node_states.get(n.id, {})} for n in task.spec.nodes],
                        "mutations": [], "progress": None}
        d["agents"] = [a.as_dict() for a in self.runtime.agents.values() if a.spec.task_id == task_id]
        d["artifacts"] = [{"id": a.id, "kind": a.kind, "title": a.title, "node": a.provenance.node_id, "confidence": a.confidence,
                           "created_at": a.provenance.created_at} for a in self.artifacts.for_task(task_id)]
        if task.result_artifact_id:
            final = self.artifacts.get(task.result_artifact_id)
            d["result"] = final.model_dump(mode="json") if final else None
        return d
