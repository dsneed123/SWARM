# Development

## Layout

```
swarm/
  config.py, paths.py, app.py, __main__.py
  core/          types, event bus, sqlite helper
  artifacts/     artifact model and content-addressed store
  hardware/      telemetry sampling, memory budget
  models/        backend interface, ollama, fake backend, profiles, router, scheduler
  tools/         registry and built-in tools (web, files, python/shell, http, github)
  permissions/   policies and approvals
  capabilities/  the capability library
  agents/        agent runtime and persistent agent state
  workflow/      spec, DAG, engine, library
  orchestrator/  orchestrator, planner, consensus, recovery, context, model-backed helpers
  memory/        world knowledge, failure log, workflow learning
  tasks/         task model, store, queue
  service/       API, socket protocol, daemon
  tui/           Textual app, screens, widgets
tests/           unit and integration tests (fake backend), hardware/ollama-gated tests
examples/        workflows, systemd unit
docs/
```

## Tests

```
pytest                       # everything that runs without a GPU or Ollama
SWARM_OLLAMA_TESTS=1 pytest  # also talk to a live Ollama
SWARM_HW_TESTS=1 pytest      # also check GB10 telemetry
SWARM_NET_TESTS=1 pytest     # also hit the web (search/fetch)
ruff check swarm tests
```

The `FakeBackend` (`swarm/models/fake.py`) simulates models with sizes,
latencies and scripted responses. `tests/helpers.py` wires the whole engine
to it with a monitor that reports fixed memory, so scheduler, budget,
consensus, recovery and the orchestrator are exercised deterministically.
`tests/test_orchestrator.py` is the end-to-end suite; its responder
dispatches on the system prompt so planner, grouper, digester and agents all
answer realistically. `tests/test_tui.py` drives the TUI headlessly.

For a real run without the TUI, `swarm serve` plus a small script against
`swarm.app.build_app` is the quickest loop; see the smoke-test shape in
`tests/test_service.py`.

## Adding a tool

Subclass `swarm.tools.registry.Tool`: set `name`, `description`, JSON
`parameters`, `category` (read / write / network / execute / external),
`side_effects`, implement `async run(args, ctx) -> ToolResult`, and return
`sources` (URL/path + retrieval time) for anything fetched so artifacts can
cite it. Register it in `swarm/tools/builtin/__init__.py`. Give
capabilities that should use it the name in their default `tools`. Policy
comes from the category unless a profile names the tool explicitly.

## Adding a backend

Implement `swarm.models.backend.ModelBackend` (list, describe, loaded,
load, unload, chat) and construct it in `swarm/app.py`. `loaded()` should
report resident memory per model; that is what turns estimates into
observations. Capabilities in the descriptor (`tools`, `thinking`) drive
routing and prompting.

## Adding a capability

Add a `Capability` in `swarm/capabilities/library.py`: prompt, default tier,
default tools, temperature, whether it produces long-form `content`. The
planner's schema enumerates capability names automatically.

## Conventions

- Subsystems communicate through the event bus for observation and through
  explicit method calls for control; agents never reach across.
- Anything persisted is small and structured; artifacts are the only large
  records and they are written once.
- No secrets in the repo; machine-specific values go in `.env` or
  `workspace/config.yaml`, both ignored by git.
- Commit in logical batches with short messages.
