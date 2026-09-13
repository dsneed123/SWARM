# Architecture

The system is one Python package with subsystems that talk through a few
narrow interfaces. Read this top-down: the orchestrator is the control
plane, everything below it is a service the orchestrator uses.

```
TUI  ──socket/JSON──▶  Service API  ──▶  Orchestrator
                                          │  plans tasks, runs DAG nodes, spawns agents,
                                          │  consensus, recovery, context, learning
                                          ▼
              Engine (DAG scheduling)   Agent runtime   Tool registry ── Permissions
                                          │                │
                                          ▼                ▼
                                   Model scheduler ◀── Hardware monitor + budget
                                          │
                                          ▼
                                   Model backend (Ollama)
     Stores: artifacts (content-addressed) · model profiles · tasks · memory · learning
```

## Orchestrator (`swarm/orchestrator/`)

The only component allowed to create, remove or restructure agents. One
instance lives for the life of the service. Per task it:

1. **Plans** (`planner.py`). Its own FAST model classifies the objective and
   proposes nodes from the capability library, guided by compositions that
   worked before (`memory/learning.py`). An invalid plan is retried once on
   the DEEP tier; failing that, a template for the objective class is used.
   Interactive tasks first get a clarification check.
2. **Executes** the DAG through the engine, providing `run_node`.
3. **Runs a node** (`_run_node`): builds the context, assigns distinct models
   to redundant agents when the tier has more than one, spawns the agents,
   collects artifacts, and resolves them by consensus. If all agents fail it
   diagnoses the failure (`recovery.py`) and applies a strategy: wait, switch
   model, escalate tier, reduce context, drop tools, split the node, or give
   up. Each decision is recorded on the node and in the failure log.
4. **Resolves** (`consensus.py`): weights each artifact by confidence,
   evidence quality and reasoning quality, discounts repeated models, groups
   conclusions into positions (an LLM grouper with a lexical fallback), and
   decides converged / weak / disagreement. On disagreement it spawns more
   independent agents (up to the node's `max_extra_agents`), then a DEEP
   arbiter that judges on evidence. In interactive mode the user can pick.
5. **Extends** automatic DAGs once when the final result looks weak (adds a
   verification pass and a revised synthesis). Never for user workflows.
6. **Finalises**: builds a `final` artifact with a Sources list gathered
   from the whole ancestry, harvests well-sourced facts into world knowledge,
   and records the composition outcome for learning.

The orchestrator escalates itself (planning, grouping, digesting) to a
deeper model only when its FAST model fails; there is no permanent judge.

## Workflow engine (`swarm/workflow/`)

`dag.py` holds `WorkflowSpec` (declarative) and `DAG` (runtime state).
`engine.py` schedules ready nodes concurrently, honours pause/cancel,
notices nodes added while running, and marks nodes blocked by a failed
critical dependency as skipped. It knows nothing about agents or consensus.
`library.py` stores user workflows as YAML.

## Agents (`swarm/agents/`, `swarm/capabilities/`)

A capability is a prompt, default tier, default tools and output shape. An
`AgentSpec` names a capability, an instruction, context views and tool
permissions; `AgentRuntime.run` acquires a model lease, loops over tool
calls through the registry, then asks for a structured JSON result and
turns it into an artifact. Agents are stateless unless the node is
`persistent`, in which case compact notes survive between runs in the
database, independent of whether the model is loaded.

## Artifacts (`swarm/artifacts/`)

The unit of information. Content-addressed (`art_<sha256 prefix>`),
immutable, versioned through `parents`. `compact()` produces the dense view
downstream agents see. `orchestrator/context.py` keeps an agent's context
inside its token budget: trims evidence first, then folds older artifacts
into `digest` artifacts (model-made, heuristic fallback) that keep
conclusions, evidence, contradictions, open questions and provenance.

## Models (`swarm/models/`)

`backend.py` is the abstract interface; `ollama.py` implements it.
`profiles.py` merges discovered metadata with observations (resident
memory, tokens/s, latency, per-capability reliability). `router.py` ranks
candidates for a request; `scheduler.py` admits requests against the
budget, loads/evicts/unloads instances, shares one instance across agents,
and records every call into the profile. See `docs/models.md`.

## Hardware (`swarm/hardware/`)

`telemetry.py` samples memory (`/proc/meminfo`), CPU, load and NVML GPU
metrics once a second. `budget.py` turns the user's ceiling percentage into
a target and a realistic usable amount (what the swarm already holds plus
what is actually free, minus a reserve) and keeps a ledger of allocations
that observed values overwrite.

## Tools and permissions (`swarm/tools/`, `swarm/permissions/`)

Tools implement `Tool.run` and are registered by name. `ToolRegistry.invoke`
is the only path from an agent to a side effect: it resolves the policy for
the call's scope (global profile and overrides, then workflow and node
tightening), asks the user through the `ApprovalBroker` when the policy is
Ask, sandboxes paths to the workspace, and records timing and sources.

## Memory (`swarm/memory/`, `swarm/tasks/`)

Four separate, small stores in one SQLite file: world knowledge (facts with
sources, FTS search, capped), failure log, workflow knowledge (best
compositions per objective class, EMA-scored), and task state. Model
performance lives in the profiles. Nothing is injected into prompts
wholesale; the planner gets one composition hint and root nodes get up to
five matching facts marked "verify before relying on them".

## Service and TUI (`swarm/service/`, `swarm/tui/`)

`service/api.py` is the command surface; `protocol.py` serves it over a
Unix socket as newline-delimited JSON with event push, and `LocalClient`
offers the same interface in-process. The Textual TUI polls a snapshot every
second and streams events; approval requests and questions pop modals.
