"""The notebook: everything the controller knows lives in one SQLite file, so a restart picks up exactly where it stopped."""
import json
import sqlite3

from .clock import minute_of

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS workflows (workflow_id TEXT PRIMARY KEY, info TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS leads (
  lead_id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, campaign_id TEXT NOT NULL,
  person_id TEXT NOT NULL, grp TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS call_counts (
  workflow_id TEXT NOT NULL, day INTEGER NOT NULL, hour INTEGER NOT NULL,
  config_version INTEGER NOT NULL, cohort_id TEXT NOT NULL,
  welcome TEXT NOT NULL, voice TEXT NOT NULL, caller_id TEXT NOT NULL, prompt TEXT NOT NULL,
  dials INTEGER NOT NULL, connected INTEGER NOT NULL, engaged INTEGER NOT NULL, halluc_flags INTEGER NOT NULL,
  PRIMARY KEY (workflow_id, day, hour, config_version, cohort_id, welcome, voice, caller_id, prompt)
);
CREATE TABLE IF NOT EXISTS seg_counts (
  workflow_id TEXT NOT NULL, day INTEGER NOT NULL, feature TEXT NOT NULL, value TEXT NOT NULL,
  config_version INTEGER NOT NULL, cohort_id TEXT NOT NULL, welcome TEXT NOT NULL, voice TEXT NOT NULL,
  dials INTEGER NOT NULL, connected INTEGER NOT NULL, engaged INTEGER NOT NULL,
  PRIMARY KEY (workflow_id, day, feature, value, config_version, cohort_id, welcome, voice)
);
CREATE TABLE IF NOT EXISTS world_events (
  event_id INTEGER PRIMARY KEY, type TEXT NOT NULL, workflow_id TEXT, ts TEXT NOT NULL, body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, workflow_id TEXT, kind TEXT NOT NULL,
  summary TEXT NOT NULL, details TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS loop_metrics (
  sim_minute INTEGER PRIMARY KEY, wall_ts REAL NOT NULL, events INTEGER NOT NULL, chunks_behind INTEGER NOT NULL,
  advance_s REAL NOT NULL, ingest_s REAL NOT NULL, decide_s REAL NOT NULL, credits REAL, rss_mb REAL
);
CREATE TABLE IF NOT EXISTS proposals (
  proposal_id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, path TEXT NOT NULL, status TEXT NOT NULL,
  base_version INTEGER NOT NULL, created_minute INTEGER NOT NULL, urgent INTEGER NOT NULL,
  approval_id TEXT, applied_version INTEGER, reasons TEXT, body TEXT NOT NULL
);
"""

PROPOSAL_COLS = ("proposal_id", "workflow_id", "path", "status", "base_version", "created_minute", "urgent",
                 "approval_id", "applied_version", "reasons", "body")

COUNT_KEY = "workflow_id, day, hour, config_version, cohort_id, welcome, voice, caller_id, prompt"
UPSERT_COUNTS = (
    f"INSERT INTO call_counts ({COUNT_KEY}, dials, connected, engaged, halluc_flags) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
    f"ON CONFLICT ({COUNT_KEY}) DO UPDATE SET dials = dials + excluded.dials, "
    "connected = connected + excluded.connected, engaged = engaged + excluded.engaged, "
    "halluc_flags = halluc_flags + excluded.halluc_flags"
)
SEG_KEY = "workflow_id, day, feature, value, config_version, cohort_id, welcome, voice"
UPSERT_SEG = (
    f"INSERT INTO seg_counts ({SEG_KEY}, dials, connected, engaged) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
    f"ON CONFLICT ({SEG_KEY}) DO UPDATE SET dials = dials + excluded.dials, "
    "connected = connected + excluded.connected, engaged = engaged + excluded.engaged"
)


class Store:
    def __init__(self, path: str):
        self.path = path
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    def meta(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, key: str, value):
        with self.db:
            self._set_meta(key, value)

    def _set_meta(self, key: str, value):
        self.db.execute("INSERT INTO meta VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (key, json.dumps(value)))

    def last_chunk(self) -> int:
        return self.meta("last_chunk", -1)

    def matches_world(self, state: dict) -> bool:
        """False when this notebook was written against a different simulator run (a new world)."""
        scenario = self.meta("scenario")
        if scenario is None:
            return True
        return (scenario == state["scenario"]
                and self.meta("last_sim_minute", 0) <= state["sim_minute"]
                and self.last_chunk() < state["chunks"])

    def save_chunk(self, chunk_no: int, counts: dict, seg: dict, leads: list, world_events: list):
        """All or nothing: a chunk's counts and the note 'chunk N is done' are committed together."""
        with self.db:
            self.db.executemany(UPSERT_COUNTS, [key + tuple(c) for key, c in counts.items()])
            self.db.executemany(UPSERT_SEG, [key + tuple(c) for key, c in seg.items()])
            self.db.executemany("INSERT OR IGNORE INTO leads VALUES (?,?,?,?,?)", leads)
            self.db.executemany("INSERT OR IGNORE INTO world_events VALUES (?,?,?,?,?)", world_events)
            self._set_meta("last_chunk", chunk_no)

    def save_metrics(self, row: tuple):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO loop_metrics VALUES (?,?,?,?,?,?,?,?,?)", row)

    def lead_group(self, lead_id: str):
        row = self.db.execute("SELECT grp FROM leads WHERE lead_id = ?", (lead_id,)).fetchone()
        return row[0] if row else None

    def save_workflows(self, workflows: list):
        with self.db:
            self.db.executemany(
                "INSERT INTO workflows VALUES (?, ?) ON CONFLICT(workflow_id) DO UPDATE SET info = excluded.info",
                [(w["workflow_id"], json.dumps(w)) for w in workflows])

    def log_decision(self, ts: str, workflow_id, kind: str, summary: str, details: dict | None = None):
        with self.db:
            self.db.execute("INSERT INTO decisions (ts, workflow_id, kind, summary, details) VALUES (?,?,?,?,?)",
                            (ts, workflow_id, kind, summary, json.dumps(details or {})))

    # ---- proposals (every change the controller asks for or makes)
    def add_proposal(self, pid: str, wid: str, path: str, base_version: int, now: int, urgent: bool, body: dict):
        with self.db:
            self.db.execute(
                "INSERT INTO proposals (proposal_id, workflow_id, path, status, base_version, created_minute, urgent, body)"
                " VALUES (?,?,?,?,?,?,?,?)", (pid, wid, path, "sending", base_version, now, int(urgent), json.dumps(body)))

    def set_proposal(self, pid: str, **fields):
        if "reasons" in fields:
            fields["reasons"] = json.dumps(fields["reasons"])
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.db:
            self.db.execute(f"UPDATE proposals SET {cols} WHERE proposal_id = ?", (*fields.values(), pid))

    def proposals(self, wid: str | None = None, statuses: tuple | None = None) -> list[dict]:
        sql, args = f"SELECT {', '.join(PROPOSAL_COLS)} FROM proposals WHERE 1=1", []
        if wid:
            sql += " AND workflow_id = ?"
            args.append(wid)
        if statuses:
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            args += list(statuses)
        rows = []
        for r in self.db.execute(sql + " ORDER BY created_minute, proposal_id", args):
            d = dict(zip(PROPOSAL_COLS, r))
            d["body"] = json.loads(d["body"])
            d["reasons"] = json.loads(d["reasons"]) if d["reasons"] else None
            rows.append(d)
        return rows

    def proposal(self, pid: str) -> dict:
        return next(p for p in self.proposals() if p["proposal_id"] == pid)

    def count_proposals_with_prefix(self, prefix: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM proposals WHERE proposal_id LIKE ?",
                               (prefix + "%",)).fetchone()[0]

    def requests_since(self, wid: str, minute: int) -> int:
        """Approval requests the simulator registered for this workflow after `minute` (they all count to its limit)."""
        return self.db.execute(
            "SELECT COUNT(*) FROM proposals WHERE workflow_id = ? AND path = 'approval' "
            "AND approval_id IS NOT NULL AND created_minute > ?", (wid, minute)).fetchone()[0]

    # ---- questions the managers ask about the counts
    def caller_stats(self, wid: str, since_hour: int, until_hour: int | None = None, min_version: int = 0) -> dict:
        """{caller_id: (dials, connected)} for calls in absolute hours [since_hour, until_hour) (hour 0 = D0 00:00),
        made under config version >= min_version."""
        sql = ("SELECT caller_id, SUM(dials), SUM(connected) FROM call_counts WHERE workflow_id = ? AND day >= ? "
               "AND day * 24 + hour >= ? AND config_version >= ?")
        args = [wid, since_hour // 24 - 1, since_hour, min_version]
        if until_hour is not None:
            sql += " AND day * 24 + hour < ?"
            args.append(until_hour)
        return {d: (n, c) for d, n, c in self.db.execute(sql + " GROUP BY caller_id", args).fetchall()}

    def prompt_stats(self, wid: str, since_hour: int | None = None, history: bool = False) -> dict:
        """{prompt: {connected, flags, engaged, last_hour}} for calls after `since_hour`, or during history only."""
        sql = ("SELECT prompt, SUM(connected), SUM(halluc_flags), SUM(engaged), MAX(day * 24 + hour) "
               "FROM call_counts WHERE workflow_id = ?")
        args = [wid]
        if history:
            sql += " AND day < 0"
        if since_hour is not None:
            sql += " AND day >= ? AND day * 24 + hour >= ?"
            args += [since_hour // 24 - 1, since_hour]
        return {p: {"connected": c, "flags": f, "engaged": e, "last_hour": lh}
                for p, c, f, e, lh in self.db.execute(sql + " GROUP BY prompt", args).fetchall()}

    def arm_stats(self, wid: str, dim: str, since_hour: int = 0) -> list:
        """[(stratum, option, connected, engaged)] for live calls since `since_hour`. A stratum is one config version
        and cohort: inside it options were assigned at random, so they can be compared fairly."""
        col = {"welcome_message": "welcome", "voice": "voice"}[dim]
        sql = (f"SELECT config_version, cohort_id, {col}, SUM(connected), SUM(engaged) FROM call_counts "
               f"WHERE workflow_id = ? AND day >= ? AND day * 24 + hour >= ? GROUP BY config_version, cohort_id, {col}")
        return [((v, c), opt, n, e) for v, c, opt, n, e in
                self.db.execute(sql, (wid, max(since_hour // 24 - 1, 0), since_hour))]

    def segment_stats(self, wid: str, dim: str, since_day: int = 0) -> dict:
        """{(feature, value): [(stratum, option, connected, engaged)]} for live calls since `since_day`."""
        col = {"welcome_message": "welcome", "voice": "voice"}[dim]
        out = {}
        for f, val, v, c, opt, n, e in self.db.execute(
                f"SELECT feature, value, config_version, cohort_id, {col}, SUM(connected), SUM(engaged) FROM seg_counts "
                f"WHERE workflow_id = ? AND day >= ? GROUP BY feature, value, config_version, cohort_id, {col}",
                (wid, since_day)):
            out.setdefault((f, val), []).append(((v, c), opt, n, e))
        return out

    def segment_sizes(self, wid: str) -> dict:
        """{(feature, value): share of answered calls} over history: how big each group is."""
        rows = self.db.execute("SELECT feature, value, SUM(connected) FROM seg_counts WHERE workflow_id = ? AND day < 0 "
                               "GROUP BY feature, value", (wid,)).fetchall()
        totals = {}
        for f, _, n in rows:
            totals[f] = totals.get(f, 0) + n
        return {(f, v): n / totals[f] for f, v, n in rows if totals[f]}

    def active_leads(self, wid: str, now: int, cadence_days: int, d0) -> int:
        """Leads in campaigns still inside their cadence (an upper bound on who a change can reach today)."""
        return sum(ev["leads"] for ev in self.world_events(wid, "campaign_created", limit=cadence_days + 1)
                   if now - minute_of(ev["ts"], d0) < cadence_days * 1440)

    def world_events(self, wid: str, kind: str, limit: int = 50) -> list[dict]:
        rows = self.db.execute("SELECT body FROM world_events WHERE workflow_id = ? AND type = ? "
                               "ORDER BY event_id DESC LIMIT ?", (wid, kind, limit)).fetchall()
        return [json.loads(r[0]) for r in rows]
