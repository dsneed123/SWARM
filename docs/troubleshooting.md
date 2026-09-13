# Troubleshooting

**Backend shows "unreachable".** The service could not reach Ollama. Check
`curl $SWARM_OLLAMA_HOST/api/version`. If your Ollama service sets
`OLLAMA_HOST` to a non-localhost address, the `ollama` CLI itself fails on
localhost too; set `SWARM_OLLAMA_HOST` in `.env` to the same address. The
service retries every 15 seconds and starts scheduling once it connects.

**"no model for … fits within the memory ceiling".** The smallest candidate
for that tier needs more than the usable budget. Usable is min(ceiling
target, swarm-held + free − reserve), so other processes holding memory
lower it. Raise the ceiling on the Hardware screen, free memory, or pull a
smaller model. Requests that are not pinned fall back to a lower tier
automatically; pinned models do not.

**Agents wait for a model for a long time.** Look at the Hardware screen:
`waiting` counts requests without a slot. Either every instance is busy
(`active` equals slots) or loading is blocked by the budget. Increase
`ollama.num_parallel` only if the Ollama service also has
`OLLAMA_NUM_PARALLEL` raised; otherwise requests queue inside Ollama.

**Memory numbers look wrong.** On the GB10 NVML reports no memory; the
swarm reads `/proc/meminfo` and Ollama's `size_vram`. An instance marked
`~` or `est` has not been observed yet; the figure is replaced within a few
seconds of loading.

**A task is stuck in `waiting_user`.** An interactive task asked a question
and nobody answered. Open the task or press `A`; Esc in the modal only
defers. Unanswered tool approvals time out as denied after
`permissions.ask_timeout_s`.

**A node failed with "format".** The model could not produce structured
JSON twice. The runtime salvages prose when it can; repeated failures make
the router avoid that model for that capability. Small or heavily
quantised models are the usual cause.

**Search returns nothing.** The built-in search uses DuckDuckGo's HTML
endpoint, which can rate-limit. Point `search.searxng_url` at a local
SearXNG instance for reliable results.

**Tasks marked failed after a restart.** Tasks that were running when the
service stopped are marked "service restarted while the task was active".
Mid-run resume is not implemented; resubmit.

**Where are the logs?** `workspace/logs/swarm.log` (service) and
`journalctl --user -u swarm` if you use the unit file. Set
`SWARM_LOG_LEVEL=DEBUG` for more.

**Reset everything.** Stop the service and delete the workspace directory
(or just `state.db` to keep artifacts and workflows). Profiles and learning
are in `state.db`.
