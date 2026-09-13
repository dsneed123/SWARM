# Dashboard and command line

`swarm` opens the dashboard (starting the service first if needed). It is
deliberately plain: your terminal's colours, no boxes, colour only on
things that need attention. Arrows and Tab move, Enter opens, Esc goes
back, `?` lists every key.

## The plain command line

If you prefer not to have a full-screen view:

```
swarm prompt                     # a line prompt; type an objective, get the answer
swarm ask "tallest mountain in Europe?"
swarm run verified-research "..."
swarm tasks | task <id> | status | models | workflows | permissions [profile]
swarm stop
```

Inside `swarm prompt`, slash commands do the rest: `/tasks`, `/task <id>`,
`/status`, `/models`, `/memory 60`, `/permissions normal`, `/allow github`,
`/deny shell`, `/workflows`, `/run <slug> <objective>`, `/mode interactive`,
`/cancel <id>`, `/quit`. Approvals and questions are asked inline; Ctrl+C
cancels the running objective.

## Dashboard (`d`)

Two summary lines (memory, GPU, CPU, backend; swarm allocation vs target,
loaded models, queue), then the task list (status, mode, progress
`done/total`, elapsed, latest consensus confidence), live agents, and the
event log. Models live on the Hardware screen.

- `n` new objective. The dialog takes the objective (multi-line), mode
  (autonomous / interactive), a saved workflow or automatic planning, and
  optional tier, model pin, redundancy and priority. Ctrl+S submits.
- `Enter` opens the selected task (or agent when the agents table has
  focus). `space` pauses/resumes, `c` cancels.

## Task (`Enter` on a task)

Header with objective, status, class, workflow, elapsed, pending question,
result summary and DAG changes. Panels: DAG nodes (capability, tier,
status, attempts, artifacts/redundancy, consensus status and confidence),
agents, artifacts, and the selected node's detail (instruction, consensus
positions with support/evidence/models, recovery notes, errors).

- `Enter` on a node opens its result artifact; on an agent, the agent; on an
  artifact, the artifact.
- `r` retries a failed node (only while the task is running), `m` reassigns
  a node's tier/model (a running node's agents are cancelled and the node
  restarts), `space` pause/resume, `c` cancel, `f` final result.

## Agent

Role, model, tier, elapsed, tokens, approximate memory share, allowed tools
and calls, error diagnosis; the exact instruction and context it received;
the transcript including tool calls and results. `a` opens its artifact,
`x` cancels it, `t` hides the transcript.

## Artifact

Conclusion, confidence, reasoning, evidence with source type, URL/path and
retrieval time, contradictions, open questions, next action, content, and
provenance (task, node, agent, inputs, parents). `p` walks to the parent,
`j` shows raw JSON.

## Workflows (`w`)

List of bundled examples and your workflows. `Enter` runs one with an
objective, `n` creates, `e` edits, `x` deletes. The editor edits name,
description, class, mode, permissions and nodes; Ctrl+N adds a node, Enter
edits the selected node, Ctrl+R removes it, Ctrl+S saves.

## Hardware & settings (`h`)

Full telemetry and scheduler state: totals, ceiling, target, usable,
allocated, headroom, allocations with estimated vs observed marks,
inference cap, waiting requests, load/unload/eviction counts, orchestrator
model and call counts, memory store sizes. `+`/`-` move the ceiling 5%,
`m` toggles the default mode, `u` unloads the selected idle model, `1/2/3`
pin the selected model as FAST/STANDARD/DEEP, `0` clears pins.

## Permissions (`p`)

Profile and effective policy per tool with source (profile or override),
call and denial counts. `1/2/3` switch profile, `a/s/d` set allow/ask/deny
for the selected tool, `x` clears the override.

## Learning (`l`)

Best compositions per objective class with runs, score, confidence and
duration; recent failures with the recovery chosen.

## Prompts

When a tool needs approval, a modal shows the tool and its arguments:
`a` allow once, `t` allow for the rest of this task, `d` deny, `x` deny for
the task, Esc decide later (`A` reopens pending approvals). When an
interactive task needs an answer, a modal offers the options or a free-text
field; Esc answers later and the task stays in `waiting_user`.
