# Workflows

A workflow is a DAG of nodes. Each node names a capability, an instruction,
and how it should run: tier or exact model, redundancy, consensus rule,
tools, permissions, persistence, time budget, attempts, whether a failure
is fatal for the task. Independent nodes run concurrently when hardware
allows.

## Automatic workflows

When you submit an objective without a workflow, the orchestrator plans
one. Automatic DAGs are *mutable*: while running, the orchestrator may split
a node that keeps failing into narrower sub-nodes plus a merge node, and it
may add a verification pass and a revised synthesis once if the final
result looks weak (low confidence, several open questions, unresolved
contradictions). Every change is listed on the task screen.

Overrides on submission (the new-task dialog): request a tier, pin a model,
or set redundancy for the investigative nodes.

## Reusable workflows

Saved as YAML in `workspace/workflows/`; bundled examples are in
`examples/workflows/`. User workflows are *immutable* once execution starts:
the orchestrator may retry, switch models within a node, and recover from
failures, but it never adds, removes or rewires nodes.

```yaml
name: Verified research
description: Independent researchers, a critic, verification, deep synthesis.
objective_class: research          # helps the learning system and the planner
mode: autonomous                   # optional default mode
permissions:                       # workflow-wide tightening (never loosening)
  shell: deny
nodes:
  - id: research
    capability: research
    instruction: "Research thoroughly with sources: {objective}"
    tier: fast
    redundancy: 3                  # three independent agents, evidence-weighted consensus
    consensus:
      mode: evidence               # evidence | single
      max_extra_agents: 2          # more investigators on disagreement
      escalate: true               # deep arbiter if still unresolved
  - id: critique
    capability: criticism
    instruction: "Critique the research on: {objective}"
    depends_on: [research]
    tier: standard
  - id: synthesis
    capability: synthesis
    instruction: "Write the final, cited answer to: {objective}"
    depends_on: [critique]
    context_from: [research, critique]   # which artifacts it sees (default: its dependencies)
    tier: deep
final_node: synthesis              # whose artifact is the task result (default: last sink)
```

Node fields:

| Field | Meaning |
| --- | --- |
| `capability` | one of research, reasoning, analysis, planning, criticism, verification, coding, writing, data, tool_use, synthesis, general |
| `instruction` | task text; `{objective}` is replaced with the user's objective |
| `tier` / `model` | `fast`, `standard`, `deep`, or an exact Ollama model name |
| `redundancy` | number of independent agents (1–12); different models are preferred when the tier has several |
| `independent_models` | set false to allow the same model for all redundant agents |
| `consensus` | `mode`, `min_confidence`, `max_extra_agents`, `escalate` |
| `tools` | explicit tool list; omit for the capability's defaults; `[]` for none |
| `permissions` | per-node tightening, e.g. `{write_file: deny}` |
| `persistent` | keep notes between runs (keyed by workflow and node) |
| `context_from` | node ids whose artifacts this node receives |
| `time_budget_s`, `max_attempts`, `critical`, `think`, `temperature`, `max_tool_rounds` | execution behaviour |

The TUI's workflow editor (press `w`, then `n` or `e`) edits all of this
without touching YAML. Examples cannot be deleted; edit one and save it under
your own slug instead.

## Consensus, briefly

Each artifact gets a weight from confidence (35%), evidence quality (45%)
and reasoning quality (20%); a second artifact from the same model counts
0.6× (less independent). Conclusions are grouped into positions. The
top position wins if it holds at least 60% of weighted support *and* no
other position has evidence at least 0.15 stronger. Otherwise the node is a
disagreement: more independent agents are spawned (up to
`max_extra_agents`), and if that does not converge a DEEP arbiter judges the
positions on evidence. In interactive mode you are asked which position to
adopt. The merged artifact keeps the losing positions as contradictions so
they are visible downstream.
