"""Proposal lifecycle, approval reconciliation, and auto-apply policy checks."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any, Callable

from .client import SimClient, SimError
from .store import Store


NON_TERMINAL_STATUSES = ("intent", "queued", "pending", "approved")
DISTRIBUTION_DIMS = ("welcome_message", "voice", "caller_id")
CHANGE_DIMS = (
    "cohorts",
    "welcome_message",
    "voice",
    "caller_id",
    "prompt_version",
    "schedule",
)


class GovernanceError(Exception):
    """Base class for governance failures."""


class RateLimitError(GovernanceError):
    """The per-workflow 24h request cap would be exceeded."""


class ProposalPolicyError(GovernanceError):
    """A proposal does not satisfy the workflow policy."""


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts))
    except ValueError:
        return None


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _reason_text(body: Any) -> str:
    if isinstance(body, dict):
        return str(body.get("detail") or body.get("error") or body)
    return str(body)


def _has_reason(body: Any, reason: str) -> bool:
    return reason in _reason_text(body)


def _add_hours(ts: str | None, hours: Any) -> str | None:
    base = _parse_ts(ts)
    if base is None or hours is None:
        return None
    return (base + timedelta(hours=float(hours))).isoformat()


class GovernanceClient:
    """Coordinates proposals through the simulator's governance boundary."""

    def __init__(
        self,
        client: SimClient,
        store: Store,
        log: Callable[[str], None] = print,
        queue_lead_minutes: int = 30,
    ):
        self.client = client
        self.store = store
        self.log = log
        self.queue_lead_minutes = int(queue_lead_minutes)

    def proposal_id(
        self,
        workflow_id: str,
        base_version: int,
        proposed_config: dict[str, Any],
        purpose: str,
    ) -> str:
        material = "|".join(
            [
                str(workflow_id),
                str(int(base_version)),
                str(purpose),
                _canonical(proposed_config),
            ]
        )
        return "prop_" + hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _sim_ts(self, sim_ts: str | None) -> str:
        if sim_ts:
            return sim_ts
        return str(self.client.get_state().get("ts"))

    def _purpose_key(self, proposal_id: str) -> str:
        return f"proposal_purpose:{proposal_id}"

    def _remember_purpose(self, proposal_id: str, purpose: str) -> None:
        self.store.set_meta(self._purpose_key(proposal_id), purpose)

    def _purpose_for(self, proposal_id: str) -> str:
        return self.store.get_meta(self._purpose_key(proposal_id)) or "repropose"

    def _audit(
        self,
        sim_ts: str | None,
        workflow_id: str | None,
        kind: str,
        reason: str,
        evidence: Any,
    ) -> None:
        self.store.write_audit(sim_ts, workflow_id, kind, reason, evidence)

    def _should_send_now(self, sim_dt: datetime | None, approver_hours: Any) -> bool:
        if sim_dt is None:
            return True
        if not isinstance(approver_hours, (list, tuple)) or len(approver_hours) != 2:
            return True
        start, end = int(approver_hours[0]), int(approver_hours[1])
        hour = sim_dt.hour + sim_dt.minute / 60.0
        if start <= hour < end:
            return True
        next_start = sim_dt.replace(hour=start, minute=0, second=0, microsecond=0)
        if sim_dt >= next_start:
            next_start += timedelta(days=1)
        return (next_start - sim_dt) <= timedelta(minutes=self.queue_lead_minutes)

    def _make_body(
        self,
        workflow: dict[str, Any],
        proposal_id: str,
        base_version: int,
        proposed_config: dict[str, Any],
        old_config: dict[str, Any],
        change_summary: str,
        diff: Any,
        affected_leads: int,
        dry_run: Any,
        impact_score: float,
        rationale: str,
        evidence: dict[str, Any] | None,
    ) -> dict[str, Any]:
        governance = workflow.get("governance") or {}
        if not change_summary:
            raise ProposalPolicyError("change_summary is required")
        if not diff:
            raise ProposalPolicyError("diff is required")
        if not dry_run:
            raise ProposalPolicyError("dry_run is required")
        if not rationale:
            raise ProposalPolicyError("rationale is required")
        if not isinstance(affected_leads, int) or affected_leads < 0:
            raise ProposalPolicyError("affected_leads must be a non-negative integer")
        if not (0.0 <= float(impact_score) <= 1.0):
            raise ProposalPolicyError("impact_score must be in [0, 1]")

        prompt_changed = proposed_config.get("prompt_version") != old_config.get(
            "prompt_version"
        )
        if prompt_changed:
            validation_id = (evidence or {}).get("validation_run_id")
            if not validation_id:
                raise ProposalPolicyError(
                    "prompt changes require evidence.validation_run_id"
                )

        ttl = governance.get("proposal_ttl_hours")
        body: dict[str, Any] = {
            "proposal_id": proposal_id,
            "workflow_id": workflow["workflow_id"],
            "base_version": int(base_version),
            "proposed_config": proposed_config,
            "change_summary": change_summary,
            "diff": diff,
            "affected_leads": int(affected_leads),
            "dry_run": dry_run,
            "impact_score": float(impact_score),
            "rationale": rationale,
        }
        if evidence:
            body["evidence"] = evidence
        if ttl is not None:
            body["expires_in_hours"] = min(float(ttl), float(ttl))
        return body

    def _find_approval(
        self, workflow_id: str, proposal_id: str, approvals: list[dict[str, Any]] | None = None
    ) -> dict[str, Any] | None:
        if approvals is None:
            approvals = self.client.list_approvals(workflow_id=workflow_id)
        for approval in approvals:
            if approval.get("proposal_id") == proposal_id:
                return approval
        return None

    def _approval_status(self, approval: dict[str, Any]) -> str:
        return str(approval.get("status") or "pending")

    def _approval_id(self, approval: dict[str, Any]) -> str | None:
        value = approval.get("approval_id") or approval.get("id")
        return str(value) if value is not None else None

    def _record_approval(
        self,
        proposal: dict[str, Any],
        approval: dict[str, Any],
        sim_ts: str,
    ) -> dict[str, Any]:
        proposal_id = proposal["proposal_id"]
        workflow_id = proposal["workflow_id"]
        status = self._approval_status(approval)
        approval_id = self._approval_id(approval) or proposal.get("approval_id")
        expires_ts = (
            approval.get("expires_at")
            or approval.get("expires_ts")
            or proposal.get("expires_ts")
        )
        apply_by_ts = (
            approval.get("apply_by")
            or approval.get("apply_by_ts")
            or proposal.get("apply_by_ts")
        )
        reasons = approval.get("reasons") or approval.get("reason") or []

        now = _parse_ts(sim_ts)
        expiry = _parse_ts(expires_ts)
        if status == "pending" and now is not None and expiry is not None and now >= expiry:
            status = "expired"
            reasons = list(reasons) + ["proposal_ttl_elapsed"]

        self.store.save_proposal(
            proposal_id,
            workflow_id,
            status,
            approval_id,
            proposal["base_version"],
            proposal["body"],
            proposal["created_ts"],
            expires_ts,
            apply_by_ts,
            sim_ts,
        )
        self._audit(
            sim_ts,
            workflow_id,
            f"proposal_{status}",
            f"approval {approval_id} is {status}",
            {"approval_id": approval_id, "status": status, "reasons": reasons},
        )
        updated = self.store.get_proposal(proposal_id)
        if status == "approved":
            return self._apply_approved(updated, sim_ts)
        return updated

    def _apply_approved(self, proposal: dict[str, Any], sim_ts: str) -> dict[str, Any]:
        proposal_id = proposal["proposal_id"]
        workflow_id = proposal["workflow_id"]
        approval_id = proposal.get("approval_id")
        if not approval_id:
            self._audit(
                sim_ts,
                workflow_id,
                "apply_skipped",
                "approved proposal has no approval_id",
                {},
            )
            return proposal

        now = _parse_ts(sim_ts)
        apply_by = _parse_ts(proposal.get("apply_by_ts"))
        if now is not None and apply_by is not None and now > apply_by:
            self.store.save_proposal(
                proposal_id,
                workflow_id,
                "lapsed",
                approval_id,
                proposal["base_version"],
                proposal["body"],
                proposal["created_ts"],
                proposal.get("expires_ts"),
                proposal.get("apply_by_ts"),
                sim_ts,
            )
            self._audit(
                sim_ts,
                workflow_id,
                "proposal_lapsed",
                "apply window elapsed before applying",
                {"approval_id": approval_id, "apply_by": proposal.get("apply_by_ts")},
            )
            return self.store.get_proposal(proposal_id)

        try:
            result = self.client.apply_approval(workflow_id, approval_id)
        except SimError as exc:
            if exc.status == 409 and _has_reason(exc.body, "stale_base_version"):
                return self._handle_stale(proposal, sim_ts)
            if exc.status in (404, 410):
                self.store.save_proposal(
                    proposal_id,
                    workflow_id,
                    "lapsed",
                    approval_id,
                    proposal["base_version"],
                    proposal["body"],
                    proposal["created_ts"],
                    proposal.get("expires_ts"),
                    proposal.get("apply_by_ts"),
                    sim_ts,
                )
                self._audit(
                    sim_ts,
                    workflow_id,
                    "proposal_lapsed",
                    _reason_text(exc.body),
                    {"approval_id": approval_id, "status": exc.status, "body": exc.body},
                )
                return self.store.get_proposal(proposal_id)
            self._audit(
                sim_ts,
                workflow_id,
                "apply_error",
                _reason_text(exc.body),
                {"approval_id": approval_id, "status": exc.status, "body": exc.body},
            )
            return proposal

        self.store.save_proposal(
            proposal_id,
            workflow_id,
            "applied",
            approval_id,
            proposal["base_version"],
            proposal["body"],
            proposal["created_ts"],
            proposal.get("expires_ts"),
            proposal.get("apply_by_ts"),
            sim_ts,
        )
        self._audit(
            sim_ts,
            workflow_id,
            "proposal_applied",
            "approved config applied",
            {"approval_id": approval_id, "response": result},
        )
        return self.store.get_proposal(proposal_id)

    def _handle_stale(
        self, proposal: dict[str, Any], sim_ts: str, depth: int = 0
    ) -> dict[str, Any]:
        proposal_id = proposal["proposal_id"]
        workflow_id = proposal["workflow_id"]
        self.store.save_proposal(
            proposal_id,
            workflow_id,
            "stale",
            proposal.get("approval_id"),
            proposal["base_version"],
            proposal["body"],
            proposal["created_ts"],
            proposal.get("expires_ts"),
            proposal.get("apply_by_ts"),
            sim_ts,
        )
        self._audit(
            sim_ts,
            workflow_id,
            "proposal_stale",
            "base_version changed before proposal completed",
            {"proposal_id": proposal_id, "base_version": proposal["base_version"]},
        )
        if depth >= 1:
            return self.store.get_proposal(proposal_id)

        current = self.client.get_config(workflow_id)
        new_base = int(current["version"])
        purpose = self._purpose_for(proposal_id)
        body = dict(proposal["body"])
        new_id = self.proposal_id(
            workflow_id, new_base, body["proposed_config"], purpose
        )
        body["proposal_id"] = new_id
        body["base_version"] = new_base
        self._remember_purpose(new_id, purpose)
        self._audit(
            sim_ts,
            workflow_id,
            "proposal_replanned",
            "rebuilt proposal on current config version",
            {"old_proposal_id": proposal_id, "new_proposal_id": new_id, "base_version": new_base},
        )
        self.store.save_proposal(
            new_id,
            workflow_id,
            "intent",
            None,
            new_base,
            body,
            sim_ts,
        )
        return self._send_intent(
            new_id,
            workflow_id,
            body,
            sim_ts,
            sim_ts,
            depth=depth + 1,
        )

    def _send_intent(
        self,
        proposal_id: str,
        workflow_id: str,
        body: dict[str, Any],
        created_ts: str,
        sim_ts: str,
        depth: int = 0,
    ) -> dict[str, Any]:
        try:
            response = self.client.request_approval(body)
        except SimError as exc:
            if exc.status == 409 and _has_reason(exc.body, "duplicate_proposal_id"):
                approval = self._find_approval(workflow_id, proposal_id)
                if approval is not None:
                    self._audit(
                        sim_ts,
                        workflow_id,
                        "proposal_duplicate_reconciled",
                        "duplicate id matched an existing approval",
                        {"proposal_id": proposal_id, "approval": approval},
                    )
                    proposal = self.store.get_proposal(proposal_id)
                    return self._record_approval(proposal, approval, sim_ts)
                self._audit(
                    sim_ts,
                    workflow_id,
                    "proposal_duplicate_unmatched",
                    "duplicate id returned but no approval was listed",
                    {"proposal_id": proposal_id},
                )
                return self.store.get_proposal(proposal_id)
            if exc.status == 409 and _has_reason(exc.body, "stale_base_version"):
                proposal = self.store.get_proposal(proposal_id)
                return self._handle_stale(proposal, sim_ts, depth=depth)
            self._audit(
                sim_ts,
                workflow_id,
                "proposal_send_error",
                _reason_text(exc.body),
                {"proposal_id": proposal_id, "status": exc.status, "body": exc.body},
            )
            raise

        approval_id = self._approval_id(response)
        status = self._approval_status(response)
        expires_ts = response.get("expires_at") or response.get("expires_ts")
        apply_by_ts = response.get("apply_by") or response.get("apply_by_ts")
        self.store.save_proposal(
            proposal_id,
            workflow_id,
            status,
            approval_id,
            body["base_version"],
            body,
            created_ts,
            expires_ts,
            apply_by_ts,
            sim_ts,
        )
        self._audit(
            sim_ts,
            workflow_id,
            f"proposal_{status}",
            f"approval request returned {status}",
            {"approval_id": approval_id, "status": status, "response": response},
        )
        proposal = self.store.get_proposal(proposal_id)
        if status == "approved":
            return self._apply_approved(proposal, sim_ts)
        return proposal

    def _maybe_send_existing(
        self,
        proposal: dict[str, Any],
        sim_ts: str,
        governance: dict[str, Any],
    ) -> dict[str, Any]:
        status = proposal["status"]
        if status not in ("intent", "queued"):
            return proposal
        if not self._should_send_now(
            _parse_ts(sim_ts), governance.get("approver_hours")
        ):
            if status != "queued":
                self.store.save_proposal(
                    proposal["proposal_id"],
                    proposal["workflow_id"],
                    "queued",
                    proposal.get("approval_id"),
                    proposal["base_version"],
                    proposal["body"],
                    proposal["created_ts"],
                    proposal.get("expires_ts"),
                    proposal.get("apply_by_ts"),
                    sim_ts,
                )
                self._audit(
                    sim_ts,
                    proposal["workflow_id"],
                    "proposal_queued",
                    "approver hours closed",
                    {"proposal_id": proposal["proposal_id"]},
                )
                return self.store.get_proposal(proposal["proposal_id"])
            return proposal

        max_requests = governance.get("max_requests_per_24h")
        if (
            max_requests is not None
            and self.store.count_requests_24h(proposal["workflow_id"], sim_ts)
            >= int(max_requests)
        ):
            self._audit(
                sim_ts,
                proposal["workflow_id"],
                "proposal_rate_limited",
                "24h request cap reached while queued",
                {"proposal_id": proposal["proposal_id"], "cap": max_requests},
            )
            return proposal

        self.store.save_proposal(
            proposal["proposal_id"],
            proposal["workflow_id"],
            "intent",
            proposal.get("approval_id"),
            proposal["base_version"],
            proposal["body"],
            proposal["created_ts"],
            proposal.get("expires_ts"),
            proposal.get("apply_by_ts"),
            sim_ts,
        )
        return self._send_intent(
            proposal["proposal_id"],
            proposal["workflow_id"],
            proposal["body"],
            proposal["created_ts"],
            sim_ts,
        )

    def propose(
        self,
        workflow_id: str,
        proposed_config: dict[str, Any],
        purpose: str,
        *,
        sim_ts: str | None = None,
        change_summary: str,
        diff: Any,
        affected_leads: int,
        dry_run: Any,
        impact_score: float,
        rationale: str,
        evidence: dict[str, Any] | None = None,
        base_version: int | None = None,
        old_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        sim_ts = self._sim_ts(sim_ts)
        workflow = self.client.get_workflow(workflow_id)
        governance = workflow.get("governance") or {}
        current = self.client.get_config(workflow_id)
        if old_config is None:
            old_config = current["config"]
        if base_version is None or int(base_version) != int(current["version"]):
            base_version = int(current["version"])

        proposal_id = self.proposal_id(
            workflow_id, int(base_version), proposed_config, purpose
        )
        self._remember_purpose(proposal_id, purpose)
        existing = self.store.get_proposal(proposal_id)
        if existing is not None:
            if existing["status"] in ("pending", "approved"):
                self.reconcile_all(workflow_id=workflow_id, sim_ts=sim_ts)
                return self.store.get_proposal(proposal_id) or existing
            if existing["status"] in ("intent", "queued"):
                return self._maybe_send_existing(existing, sim_ts, governance)
            return existing

        max_requests = governance.get("max_requests_per_24h")
        if (
            max_requests is not None
            and self.store.count_requests_24h(workflow_id, sim_ts)
            >= int(max_requests)
        ):
            self._audit(
                sim_ts,
                workflow_id,
                "proposal_rate_limited",
                "24h request cap reached",
                {
                    "proposal_id": proposal_id,
                    "cap": max_requests,
                    "count": self.store.count_requests_24h(workflow_id, sim_ts),
                },
            )
            raise RateLimitError(
                f"{workflow_id} has reached {max_requests} requests in 24h"
            )

        body = self._make_body(
            workflow,
            proposal_id,
            int(base_version),
            proposed_config,
            old_config,
            change_summary,
            diff,
            affected_leads,
            dry_run,
            impact_score,
            rationale,
            evidence,
        )

        if not self._should_send_now(_parse_ts(sim_ts), governance.get("approver_hours")):
            self.store.save_proposal(
                proposal_id,
                workflow_id,
                "queued",
                None,
                int(base_version),
                body,
                sim_ts,
                _add_hours(sim_ts, governance.get("proposal_ttl_hours")),
            )
            self._audit(
                sim_ts,
                workflow_id,
                "proposal_queued",
                "approver hours closed; queued until the window reopens",
                {"proposal_id": proposal_id, "approver_hours": governance.get("approver_hours")},
            )
            return self.store.get_proposal(proposal_id)

        self.store.save_proposal(
            proposal_id,
            workflow_id,
            "intent",
            None,
            int(base_version),
            body,
            sim_ts,
            _add_hours(sim_ts, governance.get("proposal_ttl_hours")),
        )
        self._audit(
            sim_ts,
            workflow_id,
            "proposal_intent",
            "intent recorded before API call",
            {"proposal_id": proposal_id, "base_version": base_version},
        )
        return self._send_intent(
            proposal_id, workflow_id, body, sim_ts, sim_ts
        )

    def reconcile_all(
        self, workflow_id: str | None = None, sim_ts: str | None = None
    ) -> list[dict[str, Any]]:
        sim_ts = self._sim_ts(sim_ts)
        proposals = self.store.list_proposals(
            workflow_id=workflow_id, statuses=NON_TERMINAL_STATUSES
        )
        by_workflow: dict[str, list[dict[str, Any]]] = {}
        for proposal in proposals:
            by_workflow.setdefault(proposal["workflow_id"], []).append(proposal)

        reconciled: list[dict[str, Any]] = []
        for wid, group in by_workflow.items():
            workflow = self.client.get_workflow(wid)
            governance = workflow.get("governance") or {}
            approvals = self.client.list_approvals(workflow_id=wid)
            for proposal in group:
                approval = self._find_approval(wid, proposal["proposal_id"], approvals)
                if approval is not None:
                    reconciled.append(self._record_approval(proposal, approval, sim_ts))
                    continue
                if proposal["status"] in ("intent", "queued"):
                    updated = self._maybe_send_existing(proposal, sim_ts, governance)
                    reconciled.append(updated)
                    continue
                self._audit(
                    sim_ts,
                    wid,
                    "reconcile_missing_approval",
                    "non-terminal proposal not found in approval list",
                    {"proposal_id": proposal["proposal_id"], "status": proposal["status"]},
                )
                reconciled.append(proposal)
        return reconciled

    def process(self, sim_ts: str | None = None) -> list[dict[str, Any]]:
        return self.reconcile_all(sim_ts=sim_ts)

    def _allocations(self, config: dict[str, Any], cohort_id: str, dim: str) -> dict[str, float]:
        allocations = config.get("allocations") or {}
        entry = allocations.get(cohort_id)
        if not isinstance(entry, dict) or dim not in entry:
            entry = allocations.get("default") or {}
        distribution = entry.get(dim) if isinstance(entry, dict) else None
        if not isinstance(distribution, dict):
            return {}
        return {str(k): float(v) for k, v in distribution.items()}

    def _tv_distance(self, old: dict[str, float], new: dict[str, float]) -> float:
        keys = set(old) | set(new)
        old_total = sum(old.values())
        new_total = sum(new.values())
        total = 0.0
        for key in keys:
            old_p = old.get(key, 0.0) / old_total if old_total else 0.0
            new_p = new.get(key, 0.0) / new_total if new_total else 0.0
            total += abs(old_p - new_p)
        return 0.5 * total

    def _changed_dims(
        self, old_cfg: dict[str, Any], new_cfg: dict[str, Any]
    ) -> set[str]:
        changed: set[str] = set()
        if old_cfg.get("cohorts") != new_cfg.get("cohorts"):
            changed.add("cohorts")
        old_alloc = old_cfg.get("allocations") or {}
        new_alloc = new_cfg.get("allocations") or {}
        cohorts = set(old_alloc) | set(new_alloc)
        for dim in DISTRIBUTION_DIMS:
            for cohort_id in cohorts:
                if self._allocations(old_cfg, cohort_id, dim) != self._allocations(
                    new_cfg, cohort_id, dim
                ):
                    changed.add(dim)
                    break
        if old_cfg.get("prompt_version") != new_cfg.get("prompt_version"):
            changed.add("prompt_version")
        if old_cfg.get("schedule") != new_cfg.get("schedule"):
            changed.add("schedule")
        return changed

    def auto_apply_allowed(
        self,
        workflow: dict[str, Any],
        old_cfg: dict[str, Any],
        new_cfg: dict[str, Any],
    ) -> tuple[bool, list[str]]:
        governance = workflow.get("governance") or {}
        policy = governance.get("auto_apply") or {}
        if not policy.get("enabled"):
            return False, ["auto_apply is disabled"]
        allowed = set(policy.get("allowed_dims") or [])
        changed = self._changed_dims(old_cfg, new_cfg)
        if not changed:
            return False, ["no changed dimensions"]
        blocked = sorted(changed - allowed)
        if blocked:
            return False, [f"dimensions not allowed: {', '.join(blocked)}"]

        max_shift = float(policy.get("max_shift") or 0.0)
        violations: list[str] = []
        old_alloc = old_cfg.get("allocations") or {}
        new_alloc = new_cfg.get("allocations") or {}
        cohorts = set(old_alloc) | set(new_alloc)
        for dim in DISTRIBUTION_DIMS:
            if dim not in changed:
                continue
            for cohort_id in cohorts:
                old_dist = self._allocations(old_cfg, cohort_id, dim)
                new_dist = self._allocations(new_cfg, cohort_id, dim)
                tv = self._tv_distance(old_dist, new_dist)
                if tv > max_shift + 1e-9:
                    violations.append(
                        f"{cohort_id}.{dim} shift {tv:.3f} > max_shift {max_shift:.3f}"
                    )
        if violations:
            return False, violations
        return True, []

    def try_auto_apply(
        self,
        workflow_id: str,
        proposed_config: dict[str, Any],
        *,
        sim_ts: str | None = None,
        purpose: str = "auto_apply",
        rationale: str,
        evidence: dict[str, Any] | None = None,
        affected_leads: int = 0,
    ) -> dict[str, Any]:
        sim_ts = self._sim_ts(sim_ts)
        workflow = self.client.get_workflow(workflow_id)
        current = self.client.get_config(workflow_id)
        allowed, reasons = self.auto_apply_allowed(
            workflow, current["config"], proposed_config
        )
        if not allowed:
            self._audit(
                sim_ts,
                workflow_id,
                "auto_apply_blocked",
                "local policy check refused auto-apply",
                {
                    "reasons": reasons,
                    "affected_leads": affected_leads,
                    "combined_config": proposed_config,
                },
            )
            return {"applied": False, "status": "blocked", "reasons": reasons}

        proposal_id = self.proposal_id(
            workflow_id, int(current["version"]), proposed_config, purpose
        )
        existing = self.store.get_proposal(proposal_id)
        if existing is not None and existing["status"] == "applied":
            return {"applied": True, "status": "applied", "proposal_id": proposal_id}
        if existing is not None and existing["status"] == "intent":
            body = existing["body"]
            created_ts = existing["created_ts"]
        else:
            body = {
                "proposal_id": proposal_id,
                "base_version": int(current["version"]),
                "proposed_config": proposed_config,
                "rationale": rationale,
            }
            if evidence:
                body["evidence"] = evidence
            created_ts = sim_ts
            self.store.save_proposal(
                proposal_id,
                workflow_id,
                "intent",
                None,
                int(current["version"]),
                body,
                created_ts,
            )
            self._audit(
                sim_ts,
                workflow_id,
                "auto_apply_intent",
                "intent recorded before auto-apply PUT",
                {
                    "proposal_id": proposal_id,
                    "base_version": current["version"],
                    "affected_leads": affected_leads,
                },
            )

        try:
            result = self.client.put_config(workflow_id, body)
        except SimError as exc:
            if exc.status == 409 and _has_reason(exc.body, "stale_base_version"):
                proposal = self.store.get_proposal(proposal_id)
                return self._handle_stale(proposal, sim_ts)
            status = "blocked" if exc.status == 403 else "error"
            self.store.save_proposal(
                proposal_id,
                workflow_id,
                status,
                None,
                int(body["base_version"]),
                body,
                created_ts,
                updated_ts=sim_ts,
            )
            self._audit(
                sim_ts,
                workflow_id,
                "auto_apply_error",
                _reason_text(exc.body),
                {"proposal_id": proposal_id, "status": exc.status, "body": exc.body},
            )
            return {"applied": False, "status": status, "reasons": [exc.body]}

        self.store.save_proposal(
            proposal_id,
            workflow_id,
            "applied",
            None,
            int(body["base_version"]),
            body,
            created_ts,
            updated_ts=sim_ts,
        )
        self._audit(
            sim_ts,
            workflow_id,
            "auto_apply_applied",
            "auto-apply permitted by local policy check",
            {"proposal_id": proposal_id, "response": result, "affected_leads": affected_leads},
        )
        return {"applied": True, "status": "applied", "proposal_id": proposal_id, "response": result}
