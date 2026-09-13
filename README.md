# SWARM

A local AI agent swarm for the ASUS GX10 (NVIDIA GB10, unified memory).
You give it an objective in plain language; a small persistent orchestrator
plans a DAG of agents, picks models by tier, runs redundant agents where
accuracy matters, weighs their evidence, and hands back a cited answer.
Everything runs on the machine through Ollama, and everything is visible
and controllable from a keyboard-driven terminal dashboard.

```
pip install -e .
swarm serve          # in one terminal (or install the systemd unit)
swarm                # the control center, in another
```

Press `n`, type what you want, press Ctrl+S. Watch the DAG, the agents, the
models and the memory budget live. Press `?` for keys.

## What it does

- **Objective → workflow.** The orchestrator classifies the objective and
  builds a DAG from a capability library (research, reasoning, analysis,
  planning, criticism, verification, coding, writing, data, tool use,
  synthesis). Automatic DAGs can grow while running; workflows you design
  yourself are never rewritten.
- **Three tiers, many models.** FAST / STANDARD / DEEP map onto whatever
  Ollama has installed. Agents share loaded model instances; the scheduler
  loads, evicts and unloads against a memory ceiling you set (e.g. 70%).
- **Hardware-aware.** Unified memory, GPU utilisation, CPU load and Ollama's
  own resident-memory reports feed the scheduler continuously, and observed
  numbers replace estimates in the model profiles.
- **Evidence-weighted consensus.** Redundant agents are compared on
  confidence, evidence and source quality, reasoning, and independence, not
  by vote. A minority with stronger evidence triggers more investigation or a
  deeper arbiter.
- **Artifacts, not transcripts.** Agents return immutable, content-addressed
  artifacts (conclusion, confidence, evidence with sources and retrieval
  times, contradictions, open questions, provenance). Downstream agents get
  compact, compressed views chosen by the orchestrator.
- **Adaptive recovery.** Failures are diagnosed (model, tool, context,
  resources, backend) and answered with a different strategy, not a retry.
- **Permissions.** SAFE / NORMAL / AUTONOMOUS profiles with per-tool Allow /
  Ask / Deny, per-workflow and per-node tightening, approvals in the TUI, and
  a sandboxed workspace for files.
- **Lightweight memory and learning.** Model performance, workflow
  compositions that worked, sourced facts, and failure records are kept small
  and retrieved only when relevant.

## Documentation

- [Setup](docs/setup.md) — requirements, install, running as a service
- [Architecture](docs/architecture.md) — the subsystems and how data flows
- [Configuration](docs/configuration.md) — config file, environment, permissions
- [Workflows](docs/workflows.md) — automatic plans and reusable YAML workflows
- [Models and backends](docs/models.md) — tiers, routing, scheduling, profiles
- [The TUI](docs/tui.md) — screens and keys
- [Troubleshooting](docs/troubleshooting.md)
- [Development](docs/development.md) — layout, tests, adding tools/backends

## Status

Working end to end on the GX10 with Ollama. The model backend is abstract;
only Ollama is implemented so far. See `docs/development.md` for what adding
another backend involves.

MIT licensed.
