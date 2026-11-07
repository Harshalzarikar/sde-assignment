"""SQLite persistence for ingested simulator events and controller audit records."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable


DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "controller.db"


class Store:
    """A single-file SQLite store with idempotent event ingestion and a cursor."""

    def __init__(self, path: str | Path = DEFAULT_DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS "cursor" (
                key TEXT PRIMARY KEY,
                value INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                event_id INTEGER PRIMARY KEY,
                type TEXT,
                ts TEXT,
                workflow_id TEXT,
                payload TEXT
            );
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sim_ts TEXT,
                workflow_id TEXT,
                kind TEXT,
                reason TEXT,
                evidence TEXT
            );
            """
        )
        self.conn.commit()

    def save_chunk(self, kind: str, chunk_no: int, events: Iterable[dict[str, Any]]) -> None:
        if kind not in ("events", "history"):
            raise ValueError(f"unknown chunk kind: {kind!r}")

        with self.conn:
            for event in events:
                try:
                    event_id = event["event_id"]
                except (KeyError, TypeError) as exc:
                    raise ValueError("event is missing event_id") from exc
                self.conn.execute(
                    "INSERT OR IGNORE INTO events (event_id, type, ts, workflow_id, payload) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        int(event_id),
                        event.get("type"),
                        event.get("ts"),
                        event.get("workflow_id"),
                        json.dumps(event, ensure_ascii=False, sort_keys=True),
                    ),
                )

            row = self.conn.execute(
                'SELECT value FROM "cursor" WHERE key = ?', (kind,)
            ).fetchone()
            if row is None or int(chunk_no) > int(row[0]):
                self.conn.execute(
                    'INSERT INTO "cursor" (key, value) VALUES (?, ?) '
                    'ON CONFLICT(key) DO UPDATE SET value = excluded.value',
                    (kind, int(chunk_no)),
                )

    def get_cursor(self, kind: str) -> int | None:
        row = self.conn.execute(
            'SELECT value FROM "cursor" WHERE key = ?', (kind,)
        ).fetchone()
        return int(row[0]) if row is not None else None

    def write_audit(
        self,
        sim_ts: str | None,
        workflow_id: str | None,
        kind: str,
        reason: str,
        evidence: Any = None,
    ) -> int:
        if evidence is None or isinstance(evidence, str):
            evidence_text = evidence
        else:
            evidence_text = json.dumps(evidence, ensure_ascii=False, sort_keys=True)
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO audit (sim_ts, workflow_id, kind, reason, evidence) "
                "VALUES (?, ?, ?, ?, ?)",
                (sim_ts, workflow_id, kind, reason, evidence_text),
            )
        return int(cursor.lastrowid)

    def count_events(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()
