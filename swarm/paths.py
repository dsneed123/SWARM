"""Workspace layout. Everything the swarm persists lives under one root.

    workspace/
      config.yaml         user configuration (optional)
      swarm.sock          service socket
      state.db            tasks, nodes, agents, memory, profiles (SQLite)
      artifacts/          content-addressed artifact JSON, sharded by hash prefix
      workflows/          user-defined reusable workflows (YAML)
      files/              sandboxed file area agents may read/write
      logs/               service log
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Workspace:
    root: Path

    @property
    def db(self) -> Path:
        return self.root / "state.db"

    @property
    def artifacts(self) -> Path:
        return self.root / "artifacts"

    @property
    def workflows(self) -> Path:
        return self.root / "workflows"

    @property
    def files(self) -> Path:
        return self.root / "files"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    def ensure(self) -> Workspace:
        for p in (self.root, self.artifacts, self.workflows, self.files, self.logs):
            p.mkdir(parents=True, exist_ok=True)
        return self

    def resolve_inside(self, sub: Path, relative: str) -> Path:
        """Resolve ``relative`` under ``sub`` and refuse anything escaping it."""
        base = sub.resolve()
        target = (base / relative).resolve()
        if base != target and base not in target.parents:
            raise PermissionError(f"path escapes the workspace: {relative}")
        return target
