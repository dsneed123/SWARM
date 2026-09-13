# Configuration

Precedence, highest first: runtime changes made in the TUI (which are
written back to the config file), environment variables prefixed `SWARM_`,
`workspace/config.yaml`, built-in defaults. A `.env` file in the repo root
is loaded into the environment.

`config.example.yaml` shows every key. Copy it to `workspace/config.yaml`
and edit what you need; missing keys keep their defaults.

## Environment variables

| Variable | Meaning |
| --- | --- |
| `SWARM_WORKSPACE` | workspace directory (default `./workspace`) |
| `SWARM_OLLAMA_HOST` | Ollama URL (default `http://localhost:11434`) |
| `SWARM_OLLAMA_NUM_PARALLEL` | concurrent requests one loaded model serves; match `OLLAMA_NUM_PARALLEL` |
| `SWARM_MEMORY_CEILING_PERCENT` | swarm memory ceiling, percent of total memory |
| `SWARM_TIER_FAST` / `_STANDARD` / `_DEEP` | pin a model to a tier |
| `SWARM_PERMISSION_PROFILE` | `safe`, `normal` or `autonomous` |
| `SWARM_SEARXNG_URL` | use a SearXNG instance for web search instead of DuckDuckGo HTML |
| `SWARM_LOG_LEVEL` | logging level |

## Sections

**ollama** — `host`, `request_timeout_s`, `num_parallel`, `keep_alive`.
The scheduler unloads models itself; `keep_alive` only matters if the
service dies.

**hardware** — `memory_ceiling_percent` is the upper operating boundary for
everything the swarm loads. The scheduler packs work up to it but does not
try to fill it. `safety_reserve_gb` stays free regardless.
`max_concurrent_inference` caps simultaneous generations across all models
(0 = derived from loaded instances × `num_parallel`).

**tiers** — optional pins. Without pins, models ≤ 9B parameters are FAST,
≤ 40B STANDARD, larger DEEP; the largest installed model always serves as
DEEP and the smallest as FAST if a tier would otherwise be empty.

**orchestrator** — `tier` the orchestrator runs on (FAST), `escalation_tier`
for hard planning/arbitration (DEEP), `default_mode`, `task_soft_timeout_s`
(after this, optional work is cut: redundancy drops to 1, extra consensus
rounds are skipped), `task_hard_timeout_s` (cancel), `node_timeout_s`,
`max_redundancy`, `max_consensus_rounds`, `agent_context_tokens` (budget for
the context handed to one agent), `cite_sources`.

**permissions** — `profile`, `overrides` (tool → allow/ask/deny),
`ask_timeout_s` (unanswered approvals are denied).

**search** — `searxng_url`, `user_agent`, `max_results`, `fetch_max_chars`.

**service** — `socket` path (relative to the workspace).

## Permission profiles

| | read (files, list) | network (search, fetch, http) | write (files) | execute (python, shell) | external (github, email) |
| --- | --- | --- | --- | --- | --- |
| safe | allow | allow | ask | deny (python: ask) | deny |
| normal | allow | allow | allow | python allow, shell ask | ask |
| autonomous | allow | allow | allow | allow | allow (email: ask) |

Per-tool overrides sit on top of the profile. Workflows and nodes can only
tighten the effective policy (a global Deny stays Deny; a global Ask cannot
become Allow from a workflow file). File tools are confined to
`workspace/files`.

## Runtime changes from the TUI

The Hardware screen changes the ceiling and default mode and pins tiers;
the Permissions screen changes the profile and overrides. These are applied
immediately and saved to `workspace/config.yaml`.
