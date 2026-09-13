"""Thin SQLite wrapper shared by the stores.

One connection per process, WAL mode, a lock around writes. Stores keep their
own tables; this only centralises connection handling and a generic JSON
document table used for small structured records.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.lock = threading.RLock()
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS documents (
                kind TEXT NOT NULL,
                key TEXT NOT NULL,
                body TEXT NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (kind, key)
            );
            """
        )
        self.conn.commit()

    def execute(self, sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
        with self.lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def query(self, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def script(self, sql: str) -> None:
        with self.lock:
            self.conn.executescript(sql)
            self.conn.commit()

    # --- json documents -------------------------------------------------

    def put_doc(self, kind: str, key: str, body: dict[str, Any]) -> None:
        import time

        self.execute(
            "INSERT INTO documents(kind,key,body,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(kind,key) DO UPDATE SET body=excluded.body, updated_at=excluded.updated_at",
            (kind, key, json.dumps(body), time.time()),
        )

    def get_doc(self, kind: str, key: str) -> dict[str, Any] | None:
        rows = self.query("SELECT body FROM documents WHERE kind=? AND key=?", (kind, key))
        return json.loads(rows[0]["body"]) if rows else None

    def list_docs(self, kind: str) -> dict[str, dict[str, Any]]:
        rows = self.query("SELECT key, body FROM documents WHERE kind=? ORDER BY key", (kind,))
        return {r["key"]: json.loads(r["body"]) for r in rows}

    def delete_doc(self, kind: str, key: str) -> None:
        self.execute("DELETE FROM documents WHERE kind=? AND key=?", (kind, key))

    def close(self) -> None:
        with self.lock:
            self.conn.close()
