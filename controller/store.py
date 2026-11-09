"""SQLite persistence for ingested simulator events and controller audit records."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable


DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "controller.db"

SENT_PROPOSAL_STATUSES = (
    "pending",
    "approved",
    "applied",
    "rejected",
    "expired",
    "lapsed",
    "stale",
)


def _resolve_db_path(path: str | Path | None = None) -> Path:
    if path is not None:
        return Path(path)
    configured = os.environ.get("CONTROLLER_DB")
    return Path(configured) if configured else DEFAULT_DB_PATH


class Store:
    """A single-file SQLite store with idempotent event ingestion and a cursor."""

    def __init__(self, path: str | Path | None = None):
        self.path = _resolve_db_path(path)
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
            CREATE INDEX IF NOT EXISTS events_type_event_id
                ON events (type, event_id);
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
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS proposals (
                proposal_id TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL,
                status TEXT NOT NULL,
                approval_id TEXT,
                base_version INTEGER NOT NULL,
                body TEXT NOT NULL,
                created_ts TEXT NOT NULL,
                expires_ts TEXT,
                apply_by_ts TEXT,
                updated_ts TEXT NOT NULL,
                sent_ts TEXT
            );
            CREATE INDEX IF NOT EXISTS proposals_workflow_status
                ON proposals (workflow_id, status);
            """
        )
        columns = {
            row[1] for row in self.conn.execute("PRAGMA table_info(proposals)").fetchall()
        }
        if "sent_ts" not in columns:
            self.conn.execute("ALTER TABLE proposals ADD COLUMN sent_ts TEXT")
        self.conn.commit()

    def save_chunk(self, kind: str, chunk_no: int, events: Iterable[dict[str, Any]]) -> None:
        if kind not in ("events", "history"):
            raise ValueError(f"unknown chunk kind: {kind!r}")

        def flush(batch: list[dict[str, Any]], final: bool) -> None:
            with self.conn:
                for event in batch:
                    try:
                        event_id = event["event_id"]
                    except (KeyError, TypeError) as exc:
                        raise ValueError("event is missing event_id") from exc
                    inserted = self.conn.execute(
                        "INSERT OR IGNORE INTO events "
                        "(event_id, type, ts, workflow_id, payload) "
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
                if final:
                    row = self.conn.execute(
                        'SELECT value FROM "cursor" WHERE key = ?', (kind,)
                    ).fetchone()
                    if row is None or int(chunk_no) > int(row[0]):
                        self.conn.execute(
                            'INSERT INTO "cursor" (key, value) VALUES (?, ?) '
                            'ON CONFLICT(key) DO UPDATE SET value = excluded.value',
                            (kind, int(chunk_no)),
                        )

        batch: list[dict[str, Any]] = []
        for event in events:
            batch.append(event)
            if len(batch) >= 10_000:
                flush(batch, False)
                batch = []
        flush(batch, True)

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

    def max_event_id(self) -> int | None:
        row = self.conn.execute("SELECT MAX(event_id) FROM events").fetchone()
        return int(row[0]) if row is not None and row[0] is not None else None

    def max_cursor(self) -> int | None:
        row = self.conn.execute('SELECT MAX(value) FROM "cursor"').fetchone()
        return int(row[0]) if row is not None and row[0] is not None else None

    def save_proposal(
        self,
        proposal_id: str,
        workflow_id: str,
        status: str,
        approval_id: str | None,
        base_version: int,
        body: dict[str, Any],
        created_ts: str,
        expires_ts: str | None = None,
        apply_by_ts: str | None = None,
        updated_ts: str | None = None,
        sent_ts: str | None = None,
    ) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO proposals "
                "(proposal_id, workflow_id, status, approval_id, base_version, body, "
                "created_ts, expires_ts, apply_by_ts, updated_ts, sent_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(proposal_id) DO UPDATE SET "
                "workflow_id = excluded.workflow_id, "
                "status = excluded.status, "
                "approval_id = COALESCE(excluded.approval_id, proposals.approval_id), "
                "base_version = excluded.base_version, "
                "body = excluded.body, "
                "expires_ts = COALESCE(excluded.expires_ts, proposals.expires_ts), "
                "apply_by_ts = COALESCE(excluded.apply_by_ts, proposals.apply_by_ts), "
                "updated_ts = excluded.updated_ts, "
                "sent_ts = COALESCE(excluded.sent_ts, proposals.sent_ts)",
                (
                    proposal_id,
                    workflow_id,
                    status,
                    approval_id,
                    int(base_version),
                    json.dumps(body, ensure_ascii=False, sort_keys=True),
                    created_ts,
                    expires_ts,
                    apply_by_ts,
                    updated_ts or created_ts,
                    sent_ts,
                ),
            )

    @staticmethod
    def _proposal_row(row: Any) -> dict[str, Any]:
        return {
            "proposal_id": row[0],
            "workflow_id": row[1],
            "status": row[2],
            "approval_id": row[3],
            "base_version": int(row[4]),
            "body": json.loads(row[5]),
            "created_ts": row[6],
            "expires_ts": row[7],
            "apply_by_ts": row[8],
            "updated_ts": row[9],
            "sent_ts": row[10],
        }

    def get_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT proposal_id, workflow_id, status, approval_id, base_version, body, "
            "created_ts, expires_ts, apply_by_ts, updated_ts, sent_ts "
            "FROM proposals WHERE proposal_id = ?",
            (proposal_id,),
        ).fetchone()
        return self._proposal_row(row) if row is not None else None

    def list_proposals(
        self, workflow_id: str | None = None, statuses: list[str] | tuple[str, ...] | None = None
    ) -> list[dict[str, Any]]:
        clauses = []
        params: list[Any] = []
        if workflow_id is not None:
            clauses.append("workflow_id = ?")
            params.append(workflow_id)
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            clauses.append(f"status IN ({placeholders})")
            params.extend(statuses)
        sql = (
            "SELECT proposal_id, workflow_id, status, approval_id, base_version, body, "
            "created_ts, expires_ts, apply_by_ts, updated_ts, sent_ts FROM proposals"
        )
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_ts, proposal_id"
        rows = self.conn.execute(sql, params).fetchall()
        return [self._proposal_row(row) for row in rows]

    def count_requests_24h(self, workflow_id: str, sim_now: str) -> int:
        now = datetime.fromisoformat(str(sim_now))
        cutoff = now - timedelta(hours=24)
        placeholders = ", ".join("?" for _ in SENT_PROPOSAL_STATUSES)
        rows = self.conn.execute(
            "SELECT COALESCE(sent_ts, created_ts) FROM proposals "
            f"WHERE workflow_id = ? AND status IN ({placeholders})",
            (workflow_id, *SENT_PROPOSAL_STATUSES),
        ).fetchall()
        count = 0
        for row in rows:
            try:
                created = datetime.fromisoformat(str(row[0]))
            except ValueError:
                continue
            if created >= cutoff:
                count += 1
        return count

    def set_meta(self, key: str, value: Any) -> None:
        text = value if isinstance(value, str) else json.dumps(
            value, ensure_ascii=False, sort_keys=True
        )
        with self.conn:
            self.conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, text),
            )

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row is not None else None

    def delete_meta_prefix(self, prefix: str) -> None:
        with self.conn:
            self.conn.execute(
                "DELETE FROM meta WHERE key LIKE ?", (f"{prefix}%",)
            )

    def list_events(
        self, event_type: str, after_event_id: int | None = None
    ) -> list[dict[str, Any]]:
        sql = (
            "SELECT event_id, type, ts, workflow_id, payload FROM events "
            "WHERE type = ?"
        )
        params: list[Any] = [event_type]
        if after_event_id is not None:
            sql += " AND event_id > ?"
            params.append(int(after_event_id))
        sql += " ORDER BY event_id"
        rows = self.conn.execute(sql, params).fetchall()
        return [
            {
                "event_id": int(row[0]),
                "type": row[1],
                "ts": row[2],
                "workflow_id": row[3],
                "payload": json.loads(row[4]) if row[4] else {},
            }
            for row in rows
        ]

    def record_world_fingerprint(self, state: dict[str, Any]) -> None:
        fingerprint = {
            key: state.get(key)
            for key in ("scenario", "end_minute", "tick_minutes")
        }
        self.set_meta("world", fingerprint)
        self.set_meta("last_state", state)

    def world_reset_reason(self, state: dict[str, Any]) -> str | None:
        current_last = state.get("last_event_id")
        db_last = self.max_event_id()
        if (
            current_last is not None
            and db_last is not None
            and int(current_last) < db_last
        ):
            return f"sim last_event_id {current_last} < db max event_id {db_last}"

        chunks = state.get("chunks")
        db_cursor = self.max_cursor()
        if chunks is not None and db_cursor is not None and int(chunks) < db_cursor:
            return f"sim chunks {chunks} < db cursor {db_cursor}"
        return None

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


def _archive_database(path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    archive = path.with_name(f"{path.name}.{stamp}.bak")
    for suffix in ("", "-wal", "-shm"):
        source = Path(f"{path}{suffix}")
        if source.exists():
            source.replace(Path(f"{archive}{suffix}"))
    return archive


def open_store(
    state: dict[str, Any],
    path: str | Path | None = None,
    log: Callable[[str], None] = print,
) -> Store:
    db_path = _resolve_db_path(path)
    reset_reason: str | None = None
    archive: Path | None = None

    if db_path.exists():
        existing = Store(db_path)
        try:
            reset_reason = existing.world_reset_reason(state)
        finally:
            existing.close()

        if reset_reason is not None:
            archive = _archive_database(db_path)
            log(
                f"[store] simulator reset detected ({reset_reason}); "
                f"archived {db_path.name} -> {archive.name}"
            )

    store = Store(db_path)
    store.record_world_fingerprint(state)
    if reset_reason is not None:
        store.write_audit(
            state.get("ts"),
            None,
            "sim_reset",
            reset_reason,
            {"archive": str(archive), "state": state},
        )
        log(f"[store] starting fresh database at {store.path}")
    else:
        log(
            f"[store] resuming database at {store.path} "
            f"(history cursor={store.get_cursor('history')}, "
            f"events cursor={store.get_cursor('events')})"
        )
    return store
