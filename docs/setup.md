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

Copy `.env.example` to `.env` (in the repo, or in `~/.swarm/.env`) if your Ollama does not listen on
`localhost:11434` (for example when the service has `OLLAMA_HOST` set to a
LAN or Tailscale address):

```
SWARM_OLLAMA_HOST=http://<host>:11434
```

## Run

```
swarm
```

`swarm` starts the service in the background when none is running (its
log goes to `~/.swarm/logs/service.log`) and opens the console. Quitting
the console leaves the service, its loaded models and running tasks in
place; `swarm stop` shuts it down, `swarm status` tells you whether it is up.

To have `swarm` on your PATH from any directory, either install with
`pipx install .` or link the venv script: `ln -s $PWD/.venv/bin/swarm ~/.local/bin/swarm`.

Other entry points:

- `swarm ask "objective"` — one objective, prints the answer and exits.
- `swarm run <workflow> "objective"` — the same with a saved workflow.
- `swarm tasks`, `swarm task <id>`, `swarm models`, `swarm workflows`,
  `swarm permissions [profile]` — read-only views; they never start the service.
- `swarm dashboard` — full-screen live view.
- `swarm serve` — run the service in the foreground (what the systemd unit does).
- `swarm --demo` — fake backend, no Ollama, for a look around.

State lives in `~/.swarm` (override with `SWARM_HOME`, `SWARM_WORKSPACE` or
`--workspace`): config, socket, SQLite state, artifacts, saved workflows,
logs, and a `files/` directory. File and code tools operate on the
directory you launched `swarm` from (`cd` inside the console changes it),
confined to that directory.

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
