# Models and backends

## Backends

`swarm/models/backend.py` defines what a backend must do: list models with
metadata, report loaded models and their resident memory, load/unload,
chat (with tools, JSON schema output, context size, thinking flag). Ollama
is the implementation in `ollama.py`. Adding llama.cpp or vLLM means one
new class and one line in `swarm/app.py`.

Discovery is automatic: on start and on `r` in the TUI, models are listed
from Ollama and described (`/api/show`) to get parameter count, context
window, capabilities (tools, thinking, vision) and the architecture numbers
used to estimate KV-cache memory. Nothing about specific model names is
hard-coded; a `coder` in the name marks a coding specialty, `r1`/`think`
marks reasoning.

## Tiers

FAST, STANDARD, DEEP are assigned by parameter count (≤ 9B, ≤ 40B, larger)
unless pinned in config or from the Hardware screen. Every tier always has
a model if any chat model is installed. The orchestrator itself runs on
FAST and escalates to DEEP for hard planning or arbitration.

## Routing

For each request (capability, tier or pinned model, tool needs, models to
avoid, time budget) the router scores candidates:

- accuracy prior from size (diminishing returns),
- observed reliability for that capability (success rate with an
  optimistic prior so new models still get work), plus observed confidence
  and evidence quality,
- specialty match (+) or a code-tuned model asked to do general work (−),
- thinking support for reasoning-type work (+),
- resident already (+), needs eviction to load (−),
- expected time (prompt + generation from observed throughput, plus load
  time if not resident) against the time budget (−),
- already used for this question (−, for independence in redundancy).

The scheduler takes the ranked list and picks the first candidate that is
resident with a free slot (if its score is close to the best), otherwise
loads the best one that fits. If nothing in the tier can ever fit under the
ceiling, it degrades to the next tier down rather than failing.

## Scheduling and sharing

One loaded model instance serves many agents; a second copy is never
loaded. Each instance has `num_parallel` slots (match Ollama's
`OLLAMA_NUM_PARALLEL`); agents beyond that queue on the instance. A global
cap on concurrent generations can be set in config.

Loading is admission-controlled by the memory budget: target = ceiling% ×
total memory, usable = min(target, what the swarm holds + what is free −
reserve). When a load does not fit, idle instances are evicted least
recently used first. Idle instances are unloaded after 15 minutes, or after
30 seconds when the swarm is over its target (for example after you lower
the ceiling).

Models loaded by something else (you running `ollama run`) show up as
*external*: visible, never evicted by the swarm, and counted through the
system's free-memory figure.

## Profiles: learning from the machine

`ModelProfile` stores per model: observed resident memory at a context size
(from Ollama's `/api/ps`), generation and prompt tokens/s, load time,
latency, and per-capability calls / successes / failures / JSON failures /
tool failures / average confidence and evidence. Observations are
exponential moving averages (α = 0.3), so a single odd run cannot swing
routing. Memory estimates for a new context size scale the observed KV
portion using the model's architecture (layers, KV heads, head size).

Profiles live in the workspace database and survive restarts; they are the
"performance knowledge" store. The Hardware screen shows them.

## Context sizes

Instances are loaded with a fixed `num_ctx` (8192 by default, raised when a
request asks for more). Ollama reloads a model when `num_ctx` changes, so
the scheduler keeps one context size per instance and reuses it for every
request on that instance.

## Thinking models

For models that support it, thinking is enabled only for capabilities that
ask for it (reasoning, criticism, the arbiter). Research and synthesis run
with thinking off to keep latency down.
