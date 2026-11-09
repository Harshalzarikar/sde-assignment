"""The permission sender. Every change goes through here: an approval request, or an auto-apply the policy allows.

Crash safety: a proposal is written to the notebook (status 'sending') before the HTTP call, with an id derived
from what it changes. After a restart, `reconcile` finishes whatever was half-done, and the simulator's
duplicate-id check stops anything from being sent or applied twice.
"""
import hashlib
import json
from dataclasses import dataclass, field
from typing import Callable

# From stakeholders/04 (Kestrel ops supervisor): at most 2 requests a day, never before 11:00.
# Stricter than the policy, so honouring it is our choice. Safety incidents may still use the policy's full limit.
PREFERENCES = {"kestrel_finance": {"earliest_hour": 11, "max_requests_per_24h": 2}}

OPEN = ("sending", "pending", "approved", "applying")


@dataclass
class Change:
    """One manager's wish. `edit` applies it to a config in place, so wishes can be stacked into one request."""
    name: str
    path: str  # "auto" | "approval"
    edit: Callable[[dict], None]
    summary: str
    rationale: str
    diff: dict = field(default_factory=dict)
    evidence: dict = field(default_factory=dict)
    dry_run: dict = field(default_factory=dict)
    impact: float = 0.3
    affected_leads: int = 0
    urgent: bool = False


def next_working(t: int, hours: list) -> int:
    lo, hi = hours
    day, mod = divmod(t, 1440)
    if mod < lo * 60:
        return day * 1440 + lo * 60
    if mod >= hi * 60:
        return (day + 1) * 1440 + lo * 60
    return t


class Governance:
    def __init__(self, api, store, workflows: dict):
        self.api = api
        self.store = store
        self.wf = workflows
        self._config_cache: dict = {}
        self.checks = []  # callables(proposal row) -> reason to withdraw it, or None

    def current(self, wid: str):
        """(version, config). Cached within a tick: the world only changes when time advances or we apply."""
        if wid not in self._config_cache:
            body = self.api.get_ok(f"/workflows/{wid}/config")
            self._config_cache[wid] = (body["version"], body["config"])
            known = self.store.meta(f"config:{wid}")
            if not known or known["version"] != body["version"]:
                self.store.set_meta(f"config:{wid}", {"version": body["version"], "config": body["config"]})
        version, config = self._config_cache[wid]
        return version, json.loads(json.dumps(config))

    def in_flight(self, wid: str) -> bool:
        return bool(self.store.proposals(wid, OPEN))

    def can_request(self, wid: str, now: int, urgent: bool = False):
        """(ok, reason). Routine requests keep one slot of the daily limit free for incidents."""
        w = self.wf[wid]
        gov = w["governance"]
        pref = PREFERENCES.get(w["tenant_id"], {})
        lo, hi = gov["approver_hours"]
        earliest = lo if urgent else max(lo, pref.get("earliest_hour", lo))
        if not earliest * 60 <= now % 1440 < hi * 60:
            return False, "outside approver hours"
        worst = next_working(now + gov["approver_latency_minutes"][1], [lo, hi])
        if worst > now + gov["proposal_ttl_hours"] * 60:
            return False, "the approver might not answer before the request expires"
        used = self.store.requests_since(wid, now - 1440)
        limit = gov["max_requests_per_24h"]
        cap = limit if urgent else min(limit, pref.get("max_requests_per_24h", limit)) - 1
        if used >= cap:
            return False, f"daily request budget used ({used}/{cap})"
        return True, ""

    def _new_id(self, wid: str, base: int, path: str, config: dict):
        """A fresh id for this exact (workflow, base version, path, config), or None if the simulator already refused
        that config as invalid: sending it again cannot succeed and would only spam the approval log."""
        digest = hashlib.sha1(json.dumps(config, sort_keys=True).encode()).hexdigest()[:8]
        prefix = f"{wid}-v{base}-{path}-{digest}-"
        if any(p["proposal_id"].startswith(prefix) and isinstance(p["reasons"], dict)
               and p["reasons"].get("error") == "invalid_config" for p in self.store.proposals(wid, ("failed",))):
            return None
        return prefix + str(self.store.count_proposals_with_prefix(prefix))

    # ---- sending
    def submit(self, wid: str, now: int, ts: str, changes: list):
        """Sends this tick's wishes: all auto-applicable ones in one PUT, all others in one approval request
        ('batch your changes'). Nothing auto-applies while an approval is pending (it would make it stale),
        unless it is urgent."""
        changes = [c for c in changes if c]
        busy = self.in_flight(wid)
        autos = [c for c in changes if c.path == "auto" and (c.urgent or not busy)]
        if autos:
            version, cfg = self.current(wid)
            for c in autos:
                c.edit(cfg)
            self.auto_apply(wid, now, ts, version, cfg, summary=" | ".join(c.summary for c in autos),
                            rationale=" | ".join(c.rationale for c in autos),
                            evidence={c.name: c.evidence for c in autos})
        approvals = [c for c in changes if c.path == "approval"]
        if not approvals or self.in_flight(wid):
            return
        urgent = any(c.urgent for c in approvals)
        if not self.can_request(wid, now, urgent=urgent)[0]:
            return
        version, cfg = self.current(wid)
        for c in approvals:
            c.edit(cfg)
        diff = {}
        for c in approvals:
            diff.update(c.diff)
        self.request(wid, now, ts, version, cfg, urgent=urgent,
                     summary=" + ".join(c.summary for c in approvals),
                     diff=diff, affected_leads=max(c.affected_leads for c in approvals),
                     dry_run={c.name: c.dry_run for c in approvals},
                     impact=max(c.impact for c in approvals),
                     rationale=" | ".join(f"[{c.name}] {c.rationale}" for c in approvals),
                     evidence={c.name: c.evidence for c in approvals})

    def request(self, wid: str, now: int, ts: str, base: int, config: dict, *, summary: str, diff: dict,
                affected_leads: int, dry_run: dict, impact: float, rationale: str, evidence: dict | None = None,
                urgent: bool = False):
        pid = self._new_id(wid, base, "approval", config)
        if pid is None:
            return None
        body = {"proposal_id": pid, "workflow_id": wid, "base_version": base, "proposed_config": config,
                "change_summary": summary, "diff": diff, "affected_leads": int(affected_leads), "dry_run": dry_run,
                "impact_score": round(min(max(float(impact), 0.0), 1.0), 3), "rationale": rationale}
        if evidence:
            body["evidence"] = evidence
        self.store.add_proposal(pid, wid, "approval", base, now, urgent, body)
        self.store.log_decision(ts, wid, "proposal_created", summary,
                                {"proposal_id": pid, "path": "approval", "urgent": urgent, "diff": diff,
                                 "rationale": rationale, "dry_run": dry_run, "impact_score": body["impact_score"],
                                 "affected_leads": body["affected_leads"]})
        self._send_approval(pid, ts)
        return pid

    def auto_apply(self, wid: str, now: int, ts: str, base: int, config: dict, *, summary: str, rationale: str,
                   evidence: dict | None = None):
        pid = self._new_id(wid, base, "auto", config)
        if pid is None:
            return None
        body = {"proposal_id": pid, "base_version": base, "proposed_config": config, "rationale": rationale,
                "evidence": evidence or {}, "change_summary": summary}
        self.store.add_proposal(pid, wid, "auto", base, now, False, body)
        self.store.log_decision(ts, wid, "proposal_created", summary,
                                {"proposal_id": pid, "path": "auto", "rationale": rationale, "evidence": evidence})
        self._send_auto(pid, ts)
        return pid

    def _send_approval(self, pid: str, ts: str):
        row = self.store.proposal(pid)
        wid = row["workflow_id"]
        status, resp = self.api.post("/approvals", row["body"])
        if status == 200:
            self.store.set_proposal(pid, status="pending", approval_id=resp["approval_id"])
            self.store.log_decision(ts, wid, "approval_requested",
                                    f"{resp['approval_id']} sent, expires {resp['expires_at'][:16]}",
                                    {"proposal_id": pid, "approval_id": resp["approval_id"]})
        elif status == 409 and resp.get("error") == "duplicate_proposal_id":
            self._adopt_from_server(row, ts)
        else:
            self._close(pid, "stale" if resp.get("error") == "stale_base_version" else "failed", ts, resp)

    def _send_auto(self, pid: str, ts: str):
        row = self.store.proposal(pid)
        wid = row["workflow_id"]
        body = {k: v for k, v in row["body"].items() if k != "change_summary"}
        status, resp = self.api.put(f"/workflows/{wid}/config", body)
        if status == 200:
            self._mark_applied(row, resp["version"], ts)
        elif status == 409 and resp.get("error") == "duplicate_proposal_id":
            version = self._version_with(wid, "proposal_id", pid)
            if version:
                self._mark_applied(row, version, ts)
            else:
                self._close(pid, "failed", ts, resp)
        else:
            self._close(pid, "stale" if resp.get("error") == "stale_base_version" else "failed", ts, resp)

    # ---- every tick: follow up on requests
    def tick(self, now: int, ts: str):
        self._config_cache.clear()
        for row in self.store.proposals(None, ("pending", "approved")):
            if row["status"] == "pending":
                status, a = self.api.get(f"/approvals/{row['approval_id']}")
                if status != 200 or a["status"] == "pending":
                    continue
                if a["status"] != "approved":
                    self._sync_status(row, a, ts)
                    continue
                self.store.set_proposal(row["proposal_id"], status="approved")
                self.store.log_decision(ts, row["workflow_id"], "approval_approved",
                                        f"{row['approval_id']} approved at {a['decided_at'][:16]}, "
                                        f"apply by {a['apply_by'][:16]}", {"proposal_id": row["proposal_id"]})
            self._apply(row, ts)

    def _apply(self, row: dict, ts: str):
        pid, wid = row["proposal_id"], row["workflow_id"]
        version, _ = self.current(wid)
        if version != row["base_version"]:
            self._close(pid, "stale", ts, {"current_version": version, "note": "config changed since we asked"})
            return
        for check in self.checks:
            reason = check(row)
            if reason:
                self._close(pid, "withdrawn", ts, {"note": reason})
                return
        self.store.set_proposal(pid, status="applying")
        status, resp = self.api.post(f"/workflows/{wid}/apply", {"approval_id": row["approval_id"]})
        if status == 200:
            self._mark_applied(row, resp["version"], ts)
        elif status == 410:
            self._close(pid, "lapsed", ts, resp)
        elif status == 409 and resp.get("error") == "not_approved":
            self._adopt_from_server(row, ts)
        else:
            self._close(pid, "stale" if resp.get("error") == "stale_base_version" else "failed", ts, resp)

    # ---- after a restart
    def reconcile(self, ts: str):
        for row in self.store.proposals(None, ("sending", "applying")):
            self.store.log_decision(ts, row["workflow_id"], "reconcile",
                                    f"finishing proposal left '{row['status']}' by a restart",
                                    {"proposal_id": row["proposal_id"]})
            if row["status"] == "applying":
                self._adopt_from_server(row, ts)
            elif row["path"] == "approval":
                self._send_approval(row["proposal_id"], ts)
            else:
                self._send_auto(row["proposal_id"], ts)

    # ---- helpers
    def _adopt_from_server(self, row: dict, ts: str):
        pid, wid = row["proposal_id"], row["workflow_id"]
        found = [a for a in self.api.get_ok("/approvals", workflow_id=wid) if a["proposal_id"] == pid]
        if not found:
            self._close(pid, "failed", ts, {"note": "simulator has no record of this proposal"})
            return
        a = found[0]
        if row["approval_id"] != a["approval_id"]:
            self.store.set_proposal(pid, approval_id=a["approval_id"])
            row["approval_id"] = a["approval_id"]
        self._sync_status(row, a, ts)

    def _sync_status(self, row: dict, a: dict, ts: str):
        pid = row["proposal_id"]
        if a["status"] == "applied":
            self._mark_applied(row, self._version_with(row["workflow_id"], "approval_id", a["approval_id"]), ts)
        elif a["status"] in ("pending", "approved"):
            self.store.set_proposal(pid, status=a["status"])
        else:
            self._close(pid, a["status"], ts, a.get("reasons"))

    def _version_with(self, wid: str, key: str, value: str):
        for v in self.api.get_ok(f"/workflows/{wid}/config/history"):
            if v[key] == value:
                return v["version"]
        return None

    def _mark_applied(self, row: dict, version, ts: str):
        self._config_cache.pop(row["workflow_id"], None)
        self.store.set_proposal(row["proposal_id"], status="applied", applied_version=version)
        how = "auto-applied" if row["path"] == "auto" else f"applied via approval {row['approval_id']}"
        self.store.log_decision(ts, row["workflow_id"], "applied",
                                f"{how} as config v{version}: {row['body'].get('change_summary', '')}",
                                {"proposal_id": row["proposal_id"], "version": version})

    def _close(self, pid: str, status: str, ts: str, reasons):
        row = self.store.proposal(pid)
        self.store.set_proposal(pid, status=status, reasons=reasons)
        self.store.log_decision(ts, row["workflow_id"], f"proposal_{status}",
                                f"{row['body'].get('change_summary', '')} -> {status}",
                                {"proposal_id": pid, "reasons": reasons})
        if isinstance(reasons, dict) and reasons.get("error") == "invalid_config":
            self.store.log_decision(ts, row["workflow_id"], "alert",
                                    f"controller bug: the simulator refused our config as invalid ({reasons.get('detail')}); "
                                    f"it will not be resent. Look at the config builder for proposal {pid}.", reasons)
