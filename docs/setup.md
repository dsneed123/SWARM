# Setup

## Requirements

- Linux (developed on the ASUS GX10, Ubuntu 24.04, aarch64). Any Linux box
  works for development; GPU telemetry and unified-memory accounting are only
  meaningful on the GX10.
- Python 3.11 or newer.
- [Ollama](https://ollama.com) with at least one chat model pulled. Models
  that support tool calling (`ollama show <model>` lists `tools` under
  capabilities) are needed for research and tool-using agents.
- Optional: `nvidia-ml-py` for GPU utilisation/temperature/power (installed
  with the `gpu` extra). Memory on the GB10 is read from `/proc/meminfo`
  because NVML does not report memory on unified-memory parts.
- Optional: the `gh` CLI, authenticated, if you want the GitHub tool.

## Install

```
git clone https://github.com/dsneed123/SWARM.git
cd SWARM
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[gpu,dev]"
```

Copy `.env.example` to `.env` if your Ollama does not listen on
`localhost:11434` (for example when the service has `OLLAMA_HOST` set to a
LAN or Tailscale address):

```
SWARM_OLLAMA_HOST=http://<host>:11434
```

## Run

Two processes: the service (engine) and the control center (TUI).

```
swarm serve      # engine; keeps models, tasks and memory alive
swarm            # TUI; connects over workspace/swarm.sock
```

If no service is running, `swarm` starts the engine in-process ("embedded")
so you can still use it; everything stops when you quit. `swarm tui --demo`
runs with a fake backend and no Ollama, useful for exploring the interface.

The workspace defaults to `./workspace` (override with `--workspace` or
`SWARM_WORKSPACE`). It holds the config, socket, SQLite state, artifacts,
saved workflows, the sandboxed `files/` directory agents may use, and logs.

## Run as a systemd user service

`examples/swarm.service` is a user unit. Adjust the paths, then:

```
mkdir -p ~/.config/systemd/user
cp examples/swarm.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now swarm
journalctl --user -u swarm -f
```

`loginctl enable-linger $USER` keeps it running when you are logged out.

## Verify

Open the TUI, press `h`: the backend line should read `ok` and the model
table should list your Ollama models with tiers assigned. Press `n`, submit
"What is the tallest mountain in Europe?" and watch the dashboard. The
first run loads a model, so expect a short delay before agents start.
