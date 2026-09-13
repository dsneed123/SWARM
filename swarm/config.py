"""Settings for the whole system.

Precedence (highest first): explicit overrides, environment variables prefixed
``SWARM_``, ``config.yaml`` in the workspace root, built-in defaults. A
``.env`` file next to the repo root is loaded into the environment if present.
Nothing machine-specific lives in the repository; see ``config.example.yaml``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from swarm.core.types import ExecutionMode, Policy, Tier


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


class OllamaConfig(BaseModel):
    host: str = "http://localhost:11434"
    request_timeout_s: float = 600.0
    # How many concurrent requests one loaded model instance serves before we queue.
    # Match OLLAMA_NUM_PARALLEL on the Ollama service; the scheduler learns the
    # effective value from observed queueing latency but this is the starting point.
    num_parallel: int = 4
    # Default keep-alive when a model is loaded through the scheduler. The scheduler
    # unloads explicitly, so this only matters if the service dies.
    keep_alive: str = "30m"


class HardwareConfig(BaseModel):
    # Upper operating boundary for everything the swarm loads/runs, percent of total
    # unified memory. Treated as a target to pack useful work into, not a quota to fill.
    memory_ceiling_percent: float = Field(70.0, ge=10.0, le=95.0)
    # Memory kept free for the OS and other processes regardless of the ceiling.
    safety_reserve_gb: float = 4.0
    sample_interval_s: float = 1.0
    # Upper bound on concurrently running inference calls across all loaded models.
    # 0 means derive from hardware (loaded models * num_parallel).
    max_concurrent_inference: int = 0


class TierConfig(BaseModel):
    """Optional pins. Empty means the router picks from installed models."""

    fast: str | None = None
    standard: str | None = None
    deep: str | None = None

    def pinned(self, tier: Tier) -> str | None:
        return getattr(self, tier.value)


class OrchestratorConfig(BaseModel):
    tier: Tier = Tier.FAST
    escalation_tier: Tier = Tier.DEEP
    default_mode: ExecutionMode = ExecutionMode.AUTONOMOUS
    # Overall wall-clock guard for a task before the orchestrator starts cutting scope.
    task_soft_timeout_s: float = 1800.0
    task_hard_timeout_s: float = 7200.0
    node_timeout_s: float = 900.0
    # Redundancy bounds the orchestrator works within for automatic plans.
    max_redundancy: int = 5
    max_consensus_rounds: int = 3
    # Rough token budget for context handed to a single agent.
    agent_context_tokens: int = 6000
    cite_sources: bool = True


class ToolPermissionConfig(BaseModel):
    profile: str = "normal"
    overrides: dict[str, Policy] = Field(default_factory=dict)
    ask_timeout_s: float = 600.0


class SearchConfig(BaseModel):
    searxng_url: str | None = None
    user_agent: str = "swarm-local-agent/0.1"
    max_results: int = 8
    fetch_max_chars: int = 12000


class ServiceConfig(BaseModel):
    # Unix socket path relative to the workspace unless absolute.
    socket: str = "swarm.sock"


class Settings(BaseModel):
    workspace: Path = Field(default_factory=lambda: Path.cwd() / "workspace")
    ollama: OllamaConfig = Field(default_factory=OllamaConfig)
    hardware: HardwareConfig = Field(default_factory=HardwareConfig)
    tiers: TierConfig = Field(default_factory=TierConfig)
    orchestrator: OrchestratorConfig = Field(default_factory=OrchestratorConfig)
    permissions: ToolPermissionConfig = Field(default_factory=ToolPermissionConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    service: ServiceConfig = Field(default_factory=ServiceConfig)
    log_level: str = "INFO"

    @property
    def socket_path(self) -> Path:
        """Socket location. AF_UNIX paths are limited to ~108 bytes, so a deep
        workspace falls back to the runtime dir (or /tmp) keyed by workspace hash."""
        p = Path(self.service.socket)
        p = p if p.is_absolute() else self.workspace / p
        if len(str(p).encode()) < 100:
            return p
        import hashlib

        digest = hashlib.sha256(str(self.workspace).encode()).hexdigest()[:10]
        base = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp")
        return base / f"swarm-{digest}.sock"

    def save(self, path: Path | None = None) -> Path:
        path = path or self.workspace / "config.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        data = self.model_dump(mode="json")
        data["workspace"] = str(self.workspace)
        path.write_text(yaml.safe_dump(data, sort_keys=False))
        return path


_ENV_MAP: dict[str, tuple[str, ...]] = {
    "SWARM_WORKSPACE": ("workspace",),
    "SWARM_OLLAMA_HOST": ("ollama", "host"),
    "SWARM_OLLAMA_NUM_PARALLEL": ("ollama", "num_parallel"),
    "SWARM_MEMORY_CEILING_PERCENT": ("hardware", "memory_ceiling_percent"),
    "SWARM_TIER_FAST": ("tiers", "fast"),
    "SWARM_TIER_STANDARD": ("tiers", "standard"),
    "SWARM_TIER_DEEP": ("tiers", "deep"),
    "SWARM_PERMISSION_PROFILE": ("permissions", "profile"),
    "SWARM_SEARXNG_URL": ("search", "searxng_url"),
    "SWARM_LOG_LEVEL": ("log_level",),
}


def _set_path(data: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    cur = data
    for key in path[:-1]:
        cur = cur.setdefault(key, {})
    cur[path[-1]] = value


def _deep_merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_settings(
    workspace: Path | str | None = None, overrides: dict[str, Any] | None = None
) -> Settings:
    repo_root = Path(__file__).resolve().parent.parent
    _load_dotenv(repo_root / ".env")

    data: dict[str, Any] = {}
    ws = Path(workspace or os.environ.get("SWARM_WORKSPACE") or repo_root / "workspace")
    data["workspace"] = str(ws)

    cfg_file = ws / "config.yaml"
    if cfg_file.exists():
        loaded = yaml.safe_load(cfg_file.read_text()) or {}
        loaded.pop("workspace", None)
        data = _deep_merge(data, loaded)

    for env, path in _ENV_MAP.items():
        if env in os.environ and os.environ[env] != "":
            _set_path(data, path, os.environ[env])

    if overrides:
        data = _deep_merge(data, overrides)
    return Settings.model_validate(data)
