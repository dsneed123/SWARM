"""Content-addressed artifact storage.

Each artifact is one JSON file under ``artifacts/<first two hex>/<id>.json``
with an index row in SQLite for lookups by task, node, kind or lineage.
Files are never rewritten; a revision is a new artifact pointing at its parent.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from swarm.artifacts.model import Artifact


class ArtifactStore:
    def __init__(self, root: Path, db_path: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.Lock()
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS artifacts (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                task_id TEXT NOT NULL,
                node_id TEXT NOT NULL,
                agent_id TEXT,
                capability TEXT,
                title TEXT,
                confidence REAL,
                created_at TEXT NOT NULL,
                parents TEXT NOT NULL DEFAULT '[]'
            );
            CREATE INDEX IF NOT EXISTS artifacts_task ON artifacts(task_id, node_id);
            """
        )
        self._db.commit()

    def _path(self, artifact_id: str) -> Path:
        h = artifact_id.split("_", 1)[1]
        return self.root / h[:2] / f"{artifact_id}.json"

    def put(self, artifact: Artifact) -> Artifact:
        path = self._path(artifact.id)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(artifact.model_dump_json(indent=1))
            tmp.replace(path)
        p = artifact.provenance
        with self._lock:
            self._db.execute(
                "INSERT OR IGNORE INTO artifacts VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    artifact.id,
                    artifact.kind,
                    p.task_id,
                    p.node_id,
                    p.agent_id,
                    p.capability,
                    artifact.title,
                    artifact.confidence,
                    p.created_at,
                    json.dumps(artifact.parents),
                ),
            )
            self._db.commit()
        return artifact

    def get(self, artifact_id: str) -> Artifact | None:
        path = self._path(artifact_id)
        if not path.exists():
            return None
        return Artifact.model_validate_json(path.read_text())

    def get_many(self, ids: list[str]) -> list[Artifact]:
        out = []
        for i in ids:
            a = self.get(i)
            if a is not None:
                out.append(a)
        return out

    def exists(self, artifact_id: str) -> bool:
        return self._path(artifact_id).exists()

    def for_task(self, task_id: str, kind: str | None = None) -> list[Artifact]:
        q = "SELECT id FROM artifacts WHERE task_id=?"
        args: list = [task_id]
        if kind:
            q += " AND kind=?"
            args.append(kind)
        q += " ORDER BY created_at"
        with self._lock:
            rows = self._db.execute(q, args).fetchall()
        return self.get_many([r[0] for r in rows])

    def for_node(self, task_id: str, node_id: str) -> list[Artifact]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id FROM artifacts WHERE task_id=? AND node_id=? ORDER BY created_at",
                (task_id, node_id),
            ).fetchall()
        return self.get_many([r[0] for r in rows])

    def lineage(self, artifact_id: str) -> list[Artifact]:
        """Walk parents depth-first, oldest last."""
        out: list[Artifact] = []
        seen: set[str] = set()
        stack = [artifact_id]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            a = self.get(cur)
            if a is None:
                continue
            out.append(a)
            stack.extend(a.parents)
        return out

    def ancestry(self, artifact_id: str, limit: int = 200) -> list[Artifact]:
        """Everything that fed into an artifact: parents and provenance inputs, transitively."""
        out: list[Artifact] = []
        seen: set[str] = set()
        stack = [artifact_id]
        while stack and len(out) < limit:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            a = self.get(cur)
            if a is None:
                continue
            out.append(a)
            stack.extend(a.parents)
            stack.extend(a.provenance.inputs)
        return out

    def count(self, task_id: str | None = None) -> int:
        with self._lock:
            if task_id:
                row = self._db.execute(
                    "SELECT COUNT(*) FROM artifacts WHERE task_id=?", (task_id,)
                ).fetchone()
            else:
                row = self._db.execute("SELECT COUNT(*) FROM artifacts").fetchone()
        return int(row[0])

    def close(self) -> None:
        self._db.close()
