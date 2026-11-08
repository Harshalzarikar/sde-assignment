"""SQLite persistence for ingested simulator events and controller audit records."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
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
            CREATE TABLE IF NOT EXISTS counts (
                workflow_id TEXT NOT NULL,
                cohort_id TEXT NOT NULL,
                dim TEXT NOT NULL,
                variant TEXT NOT NULL,
                hour_bucket TEXT NOT NULL,
                dials INTEGER NOT NULL DEFAULT 0,
                connected INTEGER NOT NULL DEFAULT 0,
                engaged INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (workflow_id, cohort_id, dim, variant, hour_bucket)
            );
            CREATE INDEX IF NOT EXISTS counts_workflow_hour
                ON counts (workflow_id, hour_bucket);
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
                inserted = self.conn.execute(
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
                if inserted.rowcount == 1 and event.get("type") == "call":
                    self._upsert_counts(event)

            row = self.conn.execute(
                'SELECT value FROM "cursor" WHERE key = ?', (kind,)
            ).fetchone()
            if row is None or int(chunk_no) > int(row[0]):
                self.conn.execute(
                    'INSERT INTO "cursor" (key, value) VALUES (?, ?) '
                    'ON CONFLICT(key) DO UPDATE SET value = excluded.value',
                    (kind, int(chunk_no)),
                )

    @staticmethod
    def _hour_bucket(ts: Any) -> str:
        if not ts:
            return ""
        try:
            return datetime.fromisoformat(str(ts)).strftime("%Y-%m-%dT%H")
        except ValueError:
            return str(ts)[:13]

    def _upsert_counts(self, event: dict[str, Any]) -> None:
        workflow_id = event.get("workflow_id")
        cohort_id = event.get("cohort_id") or "default"
        variant = event.get("variant") or {}
        hour_bucket = self._hour_bucket(event.get("ts"))
        connected = 1 if event.get("connected") else 0
        engaged = 1 if event.get("engaged") else 0
        for dim in ("welcome_message", "voice", "caller_id", "prompt_version"):
            value = variant.get(dim)
            if value is None:
                continue
            self.conn.execute(
                "INSERT INTO counts "
                "(workflow_id, cohort_id, dim, variant, hour_bucket, dials, connected, engaged) "
                "VALUES (?, ?, ?, ?, ?, 1, ?, ?) "
                "ON CONFLICT(workflow_id, cohort_id, dim, variant, hour_bucket) "
                "DO UPDATE SET "
                "dials = dials + 1, "
                "connected = connected + excluded.connected, "
                "engaged = engaged + excluded.engaged",
                (
                    workflow_id,
                    str(cohort_id),
                    dim,
                    str(value),
                    hour_bucket,
                    connected,
                    engaged,
                ),
            )

    def hour_totals(
        self, workflow_id: str, hour_bucket: str, dim: str = "caller_id"
    ) -> dict[str, int]:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(dials), 0), COALESCE(SUM(connected), 0), "
            "COALESCE(SUM(engaged), 0) FROM counts "
            "WHERE workflow_id = ? AND hour_bucket = ? AND dim = ?",
            (workflow_id, hour_bucket, dim),
        ).fetchone()
        return {"dials": int(row[0]), "connected": int(row[1]), "engaged": int(row[2])}

    def last_event_ts(self) -> str | None:
        row = self.conn.execute(
            "SELECT ts FROM events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        return row[0] if row is not None else None

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
