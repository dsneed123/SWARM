"""Persistent memory, kept deliberately small and separated by kind.

* World knowledge: facts with sources and retrieval timestamps, harvested
  conservatively from finished tasks (only well-sourced, high-quality
  evidence). Retrieved by keyword search; never injected wholesale.
* Failure log: what went wrong with which model/capability and what recovery
  was chosen, so routing can avoid repeating it.

Model performance knowledge lives in ``ModelProfile`` and workflow knowledge
in ``WorkflowKnowledge``; task state lives in ``TaskStore``.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

from swarm.core.db import Database

_WORD = re.compile(r"[a-zA-Z0-9]{3,}")


@dataclass
class Fact:
    id: int
    statement: str
    source: str | None
    retrieved_at: str | None
    confidence: float
    task_id: str | None
    tags: str
    created_at: float

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "statement": self.statement, "source": self.source, "retrieved_at": self.retrieved_at,
                "confidence": self.confidence, "task_id": self.task_id, "tags": self.tags}


class WorldKnowledge:
    MAX_FACTS = 5000

    def __init__(self, db: Database) -> None:
        self.db = db
        db.script(
            """
            CREATE TABLE IF NOT EXISTS facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                statement TEXT NOT NULL,
                source TEXT,
                retrieved_at TEXT,
                confidence REAL NOT NULL,
                task_id TEXT,
                tags TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                UNIQUE(statement, source)
            );
            """
        )
        self.fts = False
        try:
            db.script(
                "CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(statement, tags, content='facts', content_rowid='id');"
                "CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN "
                "INSERT INTO facts_fts(rowid, statement, tags) VALUES (new.id, new.statement, new.tags); END;"
                "CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN "
                "INSERT INTO facts_fts(facts_fts, rowid, statement, tags) VALUES ('delete', old.id, old.statement, old.tags); END;"
            )
            self.fts = True
        except Exception:  # noqa: BLE001 - sqlite built without fts5
            self.fts = False

    def add(self, statement: str, *, source: str | None, retrieved_at: str | None, confidence: float,
            task_id: str | None = None, tags: list[str] | None = None) -> bool:
        statement = " ".join(statement.split())[:600]
        if len(statement) < 15 or confidence < 0.6:
            return False
        try:
            cur = self.db.execute(
                "INSERT OR IGNORE INTO facts(statement, source, retrieved_at, confidence, task_id, tags, created_at) VALUES (?,?,?,?,?,?,?)",
                (statement, source, retrieved_at, confidence, task_id, " ".join(tags or []), time.time()),
            )
        except Exception:  # noqa: BLE001
            return False
        return cur.rowcount > 0

    def search(self, query: str, limit: int = 8) -> list[Fact]:
        words = _WORD.findall(query)[:12]
        if not words:
            return []
        rows: list = []
        if self.fts:
            q = " OR ".join(f'"{w}"' for w in words)
            try:
                rows = self.db.query(
                    "SELECT f.* FROM facts f JOIN facts_fts s ON s.rowid = f.id WHERE facts_fts MATCH ? "
                    "ORDER BY bm25(facts_fts), f.confidence DESC LIMIT ?", (q, limit))
            except Exception:  # noqa: BLE001
                rows = []
        if not rows:
            like = " OR ".join("statement LIKE ?" for _ in words)
            rows = self.db.query(f"SELECT * FROM facts WHERE {like} ORDER BY confidence DESC LIMIT ?",
                                 [f"%{w}%" for w in words] + [limit])
        return [Fact(r["id"], r["statement"], r["source"], r["retrieved_at"], r["confidence"], r["task_id"], r["tags"], r["created_at"]) for r in rows]

    def count(self) -> int:
        return int(self.db.query("SELECT COUNT(*) AS n FROM facts")[0]["n"])

    def prune(self) -> int:
        n = self.count()
        if n <= self.MAX_FACTS:
            return 0
        excess = n - self.MAX_FACTS
        self.db.execute("DELETE FROM facts WHERE id IN (SELECT id FROM facts ORDER BY confidence ASC, created_at ASC LIMIT ?)", (excess,))
        return excess


class FailureLog:
    MAX = 1000

    def __init__(self, db: Database) -> None:
        self.db = db
        db.script(
            """
            CREATE TABLE IF NOT EXISTS failures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                task_id TEXT,
                node_id TEXT,
                model TEXT,
                capability TEXT,
                kind TEXT NOT NULL,
                action TEXT NOT NULL,
                message TEXT
            );
            """
        )

    def record(self, *, task_id: str | None, node_id: str | None, model: str | None, capability: str | None,
               kind: str, action: str, message: str) -> None:
        self.db.execute(
            "INSERT INTO failures(ts, task_id, node_id, model, capability, kind, action, message) VALUES (?,?,?,?,?,?,?,?)",
            (time.time(), task_id, node_id, model, capability, kind, action, message[:500]),
        )
        self.db.execute("DELETE FROM failures WHERE id NOT IN (SELECT id FROM failures ORDER BY id DESC LIMIT ?)", (self.MAX,))

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.query("SELECT * FROM failures ORDER BY id DESC LIMIT ?", (limit,))]

    def count_for(self, model: str, capability: str | None = None, since_s: float = 7 * 86400) -> int:
        cutoff = time.time() - since_s
        if capability:
            row = self.db.query("SELECT COUNT(*) AS n FROM failures WHERE model=? AND capability=? AND ts>?", (model, capability, cutoff))
        else:
            row = self.db.query("SELECT COUNT(*) AS n FROM failures WHERE model=? AND ts>?", (model, cutoff))
        return int(row[0]["n"])
