"""Caller-ID health detection and allocation repair."""

from __future__ import annotations

import copy
import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from .client import SimClient, SimError
from .governance import (
    GovernanceClient,
    GovernanceError,
    ProposalPolicyError,
    RateLimitError,
)
from .store import Store


CALLER_STATE_PREFIX = "caller_health"
TERMINAL_BAD_PROPOSAL_STATUSES = {
    "rejected",
    "expired",
    "lapsed",
    "stale",
    "blocked",
    "error",
}


@dataclass(frozen=True)
class DetectorConfig:
    # One config block so the detector's statistical and repair policy is explicit.
    # 2h windows balance noise against a same-day response; 3 windows require 6h of
    # evidence so a single bad hour never triggers a change.
    window_hours: int = 2
    windows_required: int = 3
    # Require enough calls in both the flagged ID and the healthy pool before testing.
    min_dials: int = 100
    min_baseline_dials: int = 100
    # Baseline compares against the preceding 72h, long enough to cover weekday mix.
    baseline_hours: int = 72
    # 1% two-sided significance and an 8pp drop keep the test conservative.
    alpha: float = 0.01
    min_drop: float = 0.08
    # Keep the flagged ID at 5% rather than zero so recovery remains observable.
    exploration_share: float = 0.05
    # Recovery needs a lower sample floor because the ID is intentionally exploring.
    min_recovery_dials: int = 20
    recovery_windows: int = 3
    recovery_tolerance: float = 0.03
    # 10pp steps are individually small relative to the tightest max_shift (0.2).
    recovery_step: float = 0.10
    # One day between changes prevents flip-flopping between down and up steps.
    cooldown_hours: int = 24


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts))
    except ValueError:
        return None


def _two_proportion_z(x1: int, n1: int, x2: int, n2: int) -> tuple[float, float]:
    if n1 <= 0 or n2 <= 0:
        return 0.0, 1.0
    p1 = x1 / n1
    p2 = x2 / n2
    pooled = (x1 + x2) / (n1 + n2)
    variance = pooled * (1.0 - pooled) * (1.0 / n1 + 1.0 / n2)
    if variance <= 0:
        return 0.0, 1.0
    z = (p1 - p2) / math.sqrt(variance)
    p_value = math.erfc(abs(z) / math.sqrt(2.0))
    return z, p_value


def _metric(totals: dict[str, int], name: str) -> float | None:
    if name == "pickup_rate":
        return (
            totals["connected"] / totals["dials"]
            if totals["dials"] > 0
            else None
        )
    if name == "engagement_rate":
        return (
            totals["engaged"] / totals["connected"]
            if totals["connected"] > 0
            else None
        )
    return None


class CallerIdHealthDetector:
    """Compare caller-ID pickup rates and repair persistently unhealthy IDs."""

    def __init__(
        self,
        client: SimClient,
        store: Store,
        governance: GovernanceClient,
        config: DetectorConfig | None = None,
        log: Callable[[str], None] = print,
    ):
        self.client = client
        self.store = store
        self.governance = governance
        self.config = config or DetectorConfig()
        self.log = log

    def tick(
        self, sim_ts: str | None, workflow_ids: list[str] | None = None
    ) -> list[dict[str, Any]]:
        now = _parse_ts(sim_ts)
        if now is None:
            return []
        if workflow_ids is None:
            workflow_ids = [
                str(workflow["workflow_id"])
                for workflow in self.client.get_workflows()
            ]
        actions: list[dict[str, Any]] = []
        for workflow_id in workflow_ids:
            try:
                actions.extend(self.evaluate_workflow(workflow_id, sim_ts))
            except (SimError, GovernanceError) as exc:
                self._audit(
                    sim_ts,
                    workflow_id,
                    "caller_id_detector_error",
                    str(exc),
                    {"workflow_id": workflow_id},
                )
                self.log(f"[detect] {workflow_id} skipped: {exc}")
        return actions

    def evaluate_workflow(
        self, workflow_id: str, sim_ts: str
    ) -> list[dict[str, Any]]:
        now = _parse_ts(sim_ts)
        if now is None:
            return []
        workflow = self.client.get_workflow(workflow_id)
        config_record = self.client.get_config(workflow_id)
        config = config_record["config"]
        windows = self._windows(now)
        baseline_start = windows[0][0] - timedelta(hours=self.config.baseline_hours)
        baseline_end = windows[0][0]
        baseline = self._totals(workflow_id, baseline_start, baseline_end)
        window_totals = [
            self._totals(workflow_id, start, end) for start, end in windows
        ]
        recent_start, recent_end = windows[0][0], windows[-1][1]
        recent = self._totals(workflow_id, recent_start, recent_end)

        actions: list[dict[str, Any]] = []
        for caller_id in sorted(self._current_callers(config)):
            state = self._load_state(workflow_id, caller_id)
            if state is not None:
                state = self._refresh_state(workflow_id, caller_id, state, sim_ts)
            if state is not None:
                if state.get("status") == "active":
                    action = self._recover(
                        workflow,
                        config,
                        config_record,
                        caller_id,
                        state,
                        windows,
                        recent,
                        sim_ts,
                    )
                    if action is not None:
                        actions.append(action)
                continue

            evidence = self._detect(
                caller_id, window_totals, baseline, windows
            )
            if evidence is None:
                continue
            self._audit(
                sim_ts,
                workflow_id,
                "caller_id_degraded",
                f"caller {caller_id} pickup stayed below healthy peers and baseline",
                evidence,
            )
            action = self._fix(
                workflow,
                config,
                config_record,
                caller_id,
                evidence,
                recent,
                sim_ts,
            )
            if action is not None:
                actions.append(action)
        return actions

    def _windows(self, now: datetime) -> list[tuple[datetime, datetime]]:
        current_hour = now.replace(minute=0, second=0, microsecond=0)
        result = []
        for index in range(self.config.windows_required - 1, -1, -1):
            end = current_hour - timedelta(hours=index * self.config.window_hours)
            start = end - timedelta(hours=self.config.window_hours)
            result.append((start, end))
        return result

    def _totals(
        self, workflow_id: str, start: datetime, end: datetime
    ) -> dict[str, dict[str, int]]:
        rows = self.store.conn.execute(
            "SELECT variant, COALESCE(SUM(dials), 0), COALESCE(SUM(connected), 0), "
            "COALESCE(SUM(engaged), 0) FROM counts "
            "WHERE workflow_id = ? AND dim = 'caller_id' "
            "AND hour_bucket >= ? AND hour_bucket < ? "
            "GROUP BY variant",
            (
                workflow_id,
                start.strftime("%Y-%m-%dT%H"),
                end.strftime("%Y-%m-%dT%H"),
            ),
        ).fetchall()
        return {
            str(row[0]): {
                "dials": int(row[1]),
                "connected": int(row[2]),
                "engaged": int(row[3]),
            }
            for row in rows
        }

    def _detect(
        self,
        caller_id: str,
        window_totals: list[dict[str, dict[str, int]]],
        baseline: dict[str, dict[str, int]],
        windows: list[tuple[datetime, datetime]],
    ) -> dict[str, Any] | None:
        base = baseline.get(caller_id)
        if base is None or base["dials"] < self.config.min_baseline_dials:
            return None
        base_pickup = base["connected"] / base["dials"]
        evidence_windows: list[dict[str, Any]] = []

        for totals, (start, end) in zip(window_totals, windows):
            current = totals.get(caller_id)
            if current is None or current["dials"] < self.config.min_dials:
                return None
            others = {
                "dials": sum(item["dials"] for item in totals.values())
                - current["dials"],
                "connected": sum(
                    item["connected"] for item in totals.values()
                )
                - current["connected"],
                "engaged": sum(item["engaged"] for item in totals.values())
                - current["engaged"],
            }
            if others["dials"] < self.config.min_dials:
                return None
            pickup = current["connected"] / current["dials"]
            other_pickup = others["connected"] / others["dials"]
            z, p_value = _two_proportion_z(
                current["connected"],
                current["dials"],
                others["connected"],
                others["dials"],
            )
            z_base, p_base = _two_proportion_z(
                current["connected"],
                current["dials"],
                base["connected"],
                base["dials"],
            )
            if (
                z >= 0
                or p_value >= self.config.alpha
                or other_pickup - pickup < self.config.min_drop
                or z_base >= 0
                or p_base >= self.config.alpha
                or base_pickup - pickup < self.config.min_drop
            ):
                return None
            evidence_windows.append(
                {
                    "window_start": start.isoformat(),
                    "window_end": end.isoformat(),
                    "dials": current["dials"],
                    "connected": current["connected"],
                    "pickup_rate": round(pickup, 6),
                    "healthy_pickup_rate": round(other_pickup, 6),
                    "baseline_pickup_rate": round(base_pickup, 6),
                    "z_vs_healthy": round(z, 6),
                    "p_vs_healthy": round(p_value, 8),
                    "z_vs_baseline": round(z_base, 6),
                    "p_vs_baseline": round(p_base, 8),
                }
            )

        return {
            "caller_id": caller_id,
            "baseline_rate": round(base_pickup, 6),
            "windows": evidence_windows,
        }

    def _current_callers(self, config: dict[str, Any]) -> set[str]:
        callers: set[str] = set()
        allocations = config.get("allocations") or {}
        for entry in allocations.values():
            if not isinstance(entry, dict):
                continue
            distribution = entry.get("caller_id")
            if isinstance(distribution, dict):
                callers.update(str(key) for key in distribution)
        return callers

    def _fix(
        self,
        workflow: dict[str, Any],
        config: dict[str, Any],
        config_record: dict[str, Any],
        caller_id: str,
        evidence: dict[str, Any],
        recent: dict[str, dict[str, int]],
        sim_ts: str,
    ) -> dict[str, Any] | None:
        new_config, info = self._reduce_config(config, caller_id)
        if new_config is None:
            return None
        allowed, reason = self._guardrail_gate(
            workflow,
            recent,
            caller_id,
            info["removed_fraction"],
            direction="down",
        )
        if not allowed:
            self._save_state(
                workflow["workflow_id"],
                caller_id,
                {
                    "status": "cooling",
                    "last_action_ts": sim_ts,
                    "reason": reason,
                },
            )
            self._audit(
                sim_ts,
                workflow["workflow_id"],
                "caller_id_fix_blocked",
                reason,
                evidence,
            )
            return None

        rationale = (
            f"caller {caller_id} pickup is persistently below healthy peers and "
            f"its own baseline; reduce it to exploration share and move the weight "
            "to healthy caller IDs"
        )
        result = self._apply(
            workflow,
            config,
            config_record,
            new_config,
            caller_id,
            evidence,
            recent,
            sim_ts,
            rationale,
            purpose="caller_id_health",
        )
        state = {
            "caller_id": caller_id,
            "last_action_ts": sim_ts,
            "baseline_rate": evidence["baseline_rate"],
            "original_shares": info["original_shares"],
            "changed_keys": info["changed_keys"],
            "reason": rationale,
        }
        if result.get("status") == "applied":
            state.update({"status": "active", "applied_ts": sim_ts, "measure_from_ts": sim_ts})
        elif result["status"] == "pending":
            state.update(
                {
                    "status": "pending",
                    "proposal_id": result.get("proposal_id"),
                }
            )
        else:
            state["status"] = "cooling"
        self._save_state(workflow["workflow_id"], caller_id, state)
        return {
            "workflow_id": workflow["workflow_id"],
            "caller_id": caller_id,
            "action": "fix",
            "status": result["status"],
            "proposal_id": result.get("proposal_id"),
            "evidence": evidence,
        }

    def _recover(
        self,
        workflow: dict[str, Any],
        config: dict[str, Any],
        config_record: dict[str, Any],
        caller_id: str,
        state: dict[str, Any],
        windows: list[tuple[datetime, datetime]],
        recent: dict[str, dict[str, int]],
        sim_ts: str,
    ) -> dict[str, Any] | None:
        if not self._cooldown_elapsed(state, sim_ts):
            return None
        measure_from = _parse_ts(
            state.get("measure_from_ts") or state.get("applied_ts")
        )
        if measure_from is None:
            return None
        post_windows = [window for window in windows if window[0] >= measure_from]
        if len(post_windows) < self.config.recovery_windows:
            return None
        recovery_windows = post_windows[-self.config.recovery_windows :]
        evidence = self._recovery_evidence(
            workflow["workflow_id"],
            caller_id,
            recovery_windows,
            state.get("baseline_rate"),
        )
        if evidence is None:
            return None

        new_config, info = self._raise_config(config, caller_id, state)
        if new_config is None:
            self._clear_state(workflow["workflow_id"], caller_id)
            self._audit(
                sim_ts,
                workflow["workflow_id"],
                "caller_id_recovered",
                f"caller {caller_id} restored to its original share",
                evidence,
            )
            return {
                "workflow_id": workflow["workflow_id"],
                "caller_id": caller_id,
                "action": "recovered",
                "status": "complete",
            }

        allowed, reason = self._guardrail_gate(
            workflow,
            recent,
            caller_id,
            info["step_fraction"],
            direction="up",
        )
        if not allowed:
            state["last_action_ts"] = sim_ts
            state["reason"] = reason
            self._save_state(workflow["workflow_id"], caller_id, state)
            self._audit(
                sim_ts,
                workflow["workflow_id"],
                "caller_id_recovery_blocked",
                reason,
                evidence,
            )
            return None

        rationale = (
            f"caller {caller_id} pickup has recovered for "
            f"{self.config.recovery_windows} consecutive windows; raise its share "
            "by one small policy-safe step"
        )
        result = self._apply(
            workflow,
            config,
            config_record,
            new_config,
            caller_id,
            evidence,
            recent,
            sim_ts,
            rationale,
            purpose="caller_id_recovery",
        )
        if result.get("status") == "applied":
            state.update(
                {
                    "status": "active",
                    "last_action_ts": sim_ts,
                    "measure_from_ts": sim_ts,
                }
            )
        elif result["status"] == "pending":
            state.update(
                {
                    "status": "pending",
                    "last_action_ts": sim_ts,
                    "proposal_id": result.get("proposal_id"),
                }
            )
        else:
            state["last_action_ts"] = sim_ts
            state["status"] = "cooling"
        self._save_state(workflow["workflow_id"], caller_id, state)
        return {
            "workflow_id": workflow["workflow_id"],
            "caller_id": caller_id,
            "action": "recovery",
            "status": result["status"],
            "proposal_id": result.get("proposal_id"),
            "evidence": evidence,
        }

    def _recovery_evidence(
        self,
        workflow_id: str,
        caller_id: str,
        windows: list[tuple[datetime, datetime]],
        baseline_rate: Any,
    ) -> dict[str, Any] | None:
        if baseline_rate is None:
            return None
        evidence_windows: list[dict[str, Any]] = []
        for start, end in windows:
            totals = self._totals(workflow_id, start, end)
            current = totals.get(caller_id)
            if current is None or current["dials"] < self.config.min_recovery_dials:
                return None
            others = {
                "dials": sum(item["dials"] for item in totals.values())
                - current["dials"],
                "connected": sum(
                    item["connected"] for item in totals.values()
                )
                - current["connected"],
            }
            if others["dials"] < self.config.min_dials:
                return None
            pickup = current["connected"] / current["dials"]
            healthy = others["connected"] / others["dials"]
            if (
                pickup < float(baseline_rate) - self.config.recovery_tolerance
                or pickup < healthy - self.config.recovery_tolerance
            ):
                return None
            evidence_windows.append(
                {
                    "window_start": start.isoformat(),
                    "window_end": end.isoformat(),
                    "dials": current["dials"],
                    "connected": current["connected"],
                    "pickup_rate": round(pickup, 6),
                    "healthy_pickup_rate": round(healthy, 6),
                    "baseline_pickup_rate": round(float(baseline_rate), 6),
                }
            )
        return {
            "caller_id": caller_id,
            "windows": evidence_windows,
            "recovery_tolerance": self.config.recovery_tolerance,
        }

    def _distribution_items(
        self, config: dict[str, Any]
    ) -> list[tuple[str, dict[str, Any]]]:
        result: list[tuple[str, dict[str, Any]]] = []
        allocations = config.get("allocations") or {}
        for key, entry in allocations.items():
            if not isinstance(entry, dict):
                continue
            distribution = entry.get("caller_id")
            if isinstance(distribution, dict):
                result.append((str(key), distribution))
        return result

    def _caller_diff(
        self, old_config: dict[str, Any], new_config: dict[str, Any]
    ) -> dict[str, Any]:
        old_distributions = dict(self._distribution_items(old_config))
        diff: dict[str, Any] = {}
        for key, distribution in self._distribution_items(new_config):
            old = old_distributions.get(key) or {}
            if {name: float(value) for name, value in old.items()} != {
                name: float(value) for name, value in distribution.items()
            }:
                diff[f"allocations.{key}.caller_id"] = distribution
        if not diff:
            diff = {
                f"allocations.{key}.caller_id": distribution
                for key, distribution in self._distribution_items(new_config)
            }
        return diff

    def _reduce_config(
        self, config: dict[str, Any], caller_id: str
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        new_config = copy.deepcopy(config)
        original_shares: dict[str, float] = {}
        changed_keys: list[str] = []
        for key, distribution in self._distribution_items(new_config):
            total = sum(float(weight) for weight in distribution.values())
            if total <= 0 or caller_id not in distribution:
                continue
            current = float(distribution[caller_id]) / total
            if current <= self.config.exploration_share + 1e-9:
                continue
            target_weight = self.config.exploration_share * total
            removed = float(distribution[caller_id]) - target_weight
            others = {
                name: float(weight)
                for name, weight in distribution.items()
                if name != caller_id and float(weight) > 0
            }
            others_total = sum(others.values())
            if others_total <= 0:
                continue
            for name, weight in others.items():
                distribution[name] = weight + removed * (weight / others_total)
            distribution[caller_id] = target_weight
            original_shares[key] = current
            changed_keys.append(key)
        if not changed_keys:
            return None, None
        default_key = "default" if "default" in original_shares else changed_keys[0]
        removed_fraction = (
            original_shares[default_key] - self.config.exploration_share
        )
        return new_config, {
            "original_shares": original_shares,
            "changed_keys": changed_keys,
            "removed_fraction": removed_fraction,
        }

    def _raise_config(
        self,
        config: dict[str, Any],
        caller_id: str,
        state: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        new_config = copy.deepcopy(config)
        original_shares = state.get("original_shares") or {}
        steps: dict[str, dict[str, float]] = {}
        for key, distribution in self._distribution_items(new_config):
            if key not in original_shares:
                continue
            total = sum(float(weight) for weight in distribution.values())
            if total <= 0 or caller_id not in distribution:
                continue
            current = float(distribution[caller_id]) / total
            original = float(original_shares[key])
            if current >= original - 1e-9:
                continue
            target = min(original, current + self.config.recovery_step)
            delta = (target - current) * total
            others = {
                name: float(weight)
                for name, weight in distribution.items()
                if name != caller_id and float(weight) > 0
            }
            others_total = sum(others.values())
            if others_total <= 0:
                continue
            for name, weight in others.items():
                distribution[name] = max(0.0, weight - delta * (weight / others_total))
            distribution[caller_id] = float(distribution[caller_id]) + delta
            steps[key] = {
                "from_share": current,
                "to_share": target,
                "original_share": original,
            }
        if not steps:
            return None, None
        step_fraction = max(
            info["to_share"] - info["from_share"] for info in steps.values()
        )
        return new_config, {"steps": steps, "step_fraction": step_fraction}

    def _guardrail_gate(
        self,
        workflow: dict[str, Any],
        recent: dict[str, dict[str, int]],
        caller_id: str,
        fraction: float,
        *,
        direction: str,
    ) -> tuple[bool, str]:
        guardrail = (workflow.get("objective") or {}).get("guardrail") or {}
        metric_name = None
        max_drop = None
        for key, value in guardrail.items():
            if str(key).endswith("_max_drop"):
                metric_name = str(key)[: -len("_max_drop")]
                max_drop = float(value)
                break
        if metric_name is None or max_drop is None:
            return True, ""
        if metric_name not in ("pickup_rate", "engagement_rate"):
            return True, ""

        flagged = recent.get(caller_id)
        if flagged is None:
            return False, f"no {metric_name} evidence for caller {caller_id}"
        healthy = {
            "dials": sum(item["dials"] for item in recent.values())
            - flagged["dials"],
            "connected": sum(item["connected"] for item in recent.values())
            - flagged["connected"],
            "engaged": sum(item["engaged"] for item in recent.values())
            - flagged["engaged"],
        }
        flagged_metric = _metric(flagged, metric_name)
        healthy_metric = _metric(healthy, metric_name)
        if flagged_metric is None or healthy_metric is None:
            return False, f"insufficient {metric_name} data for guardrail check"
        if direction == "down":
            expected = fraction * (healthy_metric - flagged_metric)
        else:
            expected = fraction * (flagged_metric - healthy_metric)
        if expected < -max_drop - 1e-9:
            return (
                False,
                f"expected {metric_name} change {expected:.4f} would exceed "
                f"max drop {max_drop:.4f}",
            )
        return True, ""

    def _apply(
        self,
        workflow: dict[str, Any],
        config: dict[str, Any],
        config_record: dict[str, Any],
        new_config: dict[str, Any],
        caller_id: str,
        evidence: dict[str, Any],
        recent: dict[str, dict[str, int]],
        sim_ts: str,
        rationale: str,
        *,
        purpose: str,
    ) -> dict[str, Any]:
        workflow_id = workflow["workflow_id"]
        allowed, _ = self.governance.auto_apply_allowed(
            workflow, config, new_config
        )
        if allowed:
            result = self.governance.try_auto_apply(
                workflow_id,
                new_config,
                sim_ts=sim_ts,
                purpose=purpose,
                rationale=rationale,
                evidence=evidence,
                affected_leads=int(sum(item["dials"] for item in recent.values())),
            )
            if result.get("applied"):
                self._audit(
                    sim_ts,
                    workflow_id,
                    "caller_id_fix_applied",
                    rationale,
                    evidence,
                )
                return {
                    "status": "applied",
                    "proposal_id": result.get("proposal_id"),
                }

        diff = self._caller_diff(config, new_config)
        dry_run = {
            "window_hours": self.config.window_hours,
            "windows_required": self.config.windows_required,
            "caller_id": caller_id,
            "evidence": evidence,
        }
        impact = min(1.0, max(0.0, float(self.config.min_drop)))
        try:
            proposal = self.governance.propose(
                workflow_id,
                new_config,
                f"{purpose}_approval",
                sim_ts=sim_ts,
                change_summary=(
                    f"shift caller weight away from {caller_id} toward healthy IDs"
                ),
                diff=diff,
                affected_leads=int(sum(item["dials"] for item in recent.values())),
                dry_run=dry_run,
                impact_score=impact,
                rationale=rationale,
                evidence=evidence,
                old_config=config,
            )
        except RateLimitError as exc:
            self._audit(
                sim_ts,
                workflow_id,
                "caller_id_fix_rate_limited",
                str(exc),
                evidence,
            )
            return {"status": "rate_limited", "proposal_id": None}
        except (ProposalPolicyError, SimError) as exc:
            self._audit(
                sim_ts,
                workflow_id,
                "caller_id_fix_error",
                str(exc),
                evidence,
            )
            return {"status": "error", "proposal_id": None}

        status = str(proposal.get("status"))
        if status == "applied":
            self._audit(
                sim_ts,
                workflow_id,
                "caller_id_fix_applied",
                rationale,
                evidence,
            )
            return {"status": "applied", "proposal_id": proposal.get("proposal_id")}
        if status in ("pending", "queued", "approved", "intent"):
            self._audit(
                sim_ts,
                workflow_id,
                "caller_id_fix_proposed",
                rationale,
                evidence,
            )
            return {"status": "pending", "proposal_id": proposal.get("proposal_id")}
        self._audit(
            sim_ts,
            workflow_id,
            "caller_id_fix_rejected",
            f"approval path returned {status}",
            evidence,
        )
        return {"status": "rejected", "proposal_id": proposal.get("proposal_id")}

    def _cooldown_elapsed(self, state: dict[str, Any], sim_ts: str) -> bool:
        now = _parse_ts(sim_ts)
        last = _parse_ts(state.get("last_action_ts"))
        if now is None or last is None:
            return True
        return now >= last + timedelta(hours=self.config.cooldown_hours)

    def _refresh_state(
        self,
        workflow_id: str,
        caller_id: str,
        state: dict[str, Any],
        sim_ts: str,
    ) -> dict[str, Any] | None:
        status = state.get("status")
        if status == "pending":
            proposal = self.store.get_proposal(state.get("proposal_id"))
            if proposal is None:
                state["status"] = "cooling"
                self._save_state(workflow_id, caller_id, state)
                return state
            if proposal["status"] == "applied":
                applied_ts = proposal.get("updated_ts") or sim_ts
                state.update(
                    {
                        "status": "active",
                        "applied_ts": applied_ts,
                        "measure_from_ts": applied_ts,
                        "last_action_ts": applied_ts,
                    }
                )
                self._save_state(workflow_id, caller_id, state)
                return state
            if proposal["status"] in TERMINAL_BAD_PROPOSAL_STATUSES:
                state["status"] = "cooling"
                self._save_state(workflow_id, caller_id, state)
                return state
            return state
        if status == "cooling":
            if self._cooldown_elapsed(state, sim_ts):
                self._clear_state(workflow_id, caller_id)
                return None
            return state
        return state

    def _state_key(self, workflow_id: str, caller_id: str) -> str:
        return f"{CALLER_STATE_PREFIX}:{workflow_id}:{caller_id}"

    def _load_state(
        self, workflow_id: str, caller_id: str
    ) -> dict[str, Any] | None:
        raw = self.store.get_meta(self._state_key(workflow_id, caller_id))
        if not raw:
            return None
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def _save_state(
        self, workflow_id: str, caller_id: str, state: dict[str, Any]
    ) -> None:
        self.store.set_meta(self._state_key(workflow_id, caller_id), state)

    def _clear_state(self, workflow_id: str, caller_id: str) -> None:
        self.store.set_meta(self._state_key(workflow_id, caller_id), "")

    def _audit(
        self,
        sim_ts: str | None,
        workflow_id: str | None,
        kind: str,
        reason: str,
        evidence: Any,
    ) -> None:
        self.store.write_audit(sim_ts, workflow_id, kind, reason, evidence)
