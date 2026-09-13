# Console and dashboard

`swarm` opens the console, starting the service first if needed.

```
  swarm  ·  local agent swarm on the GX10
  8 models · backend ok · 40 GB usable · permissions normal · service · /home/you/.swarm
  working directory /home/you/project

swarm > what changed in python 3.13 garbage collection?
swarm > use verified-research
swarm (verified-research) > run is the GX10 faster than a 4090 for 70B inference?
swarm > show models
swarm > set memory 60
```

Anything that is not a command runs as an objective: a few progress lines
per node (plan, models loaded, consensus verdicts, recoveries), then the
answer with its sources, confidence, time and models used. Ctrl+C cancels
the running objective. Questions from interactive tasks and tool
approvals are asked inline.

Commands (`help` prints them; Tab completes them):

| Command | What it does |
| --- | --- |
| `ask <text>` / `run <text>` | run an objective, plain or with the selected workflow |
| `use <workflow>` / `use` / `back` | select a saved workflow (by name or from a numbered menu); the prompt shows it |
| `show tasks` / `show task <id>` | task history; one task's nodes, consensus and notes, and its result |
| `show models` / `show status` / `show workflows` / `show permissions` / `show events` | what is loaded and how the machine is doing |
| `set memory 60` | memory ceiling in percent |
| `set mode interactive` | default execution mode |
| `set permissions safe` / `set tool shell deny` | permission profile and per-tool overrides |
| `cancel` / `pause` / `resume <id>` | control a running task |
| `approvals` / `answer <id> <text>` | answer pending approvals and questions |
| `cd <dir>` | change the directory agents get for file and code tools |
| `stop` / `exit` | stop the service / leave (the service keeps running) |

One-shot forms exist for scripts: `swarm ask "..."`, `swarm tasks`,
`swarm models`, `swarm status`, `swarm stop`.

## Dashboard (`swarm dashboard`)

An optional full-screen live view. Arrows and Tab move, Enter opens, Esc
goes back, `?` lists every key.

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
