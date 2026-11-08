"""Governance lifecycle tests using a fake simulator client."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta

import pytest

from controller.client import SimError
from controller.governance import GovernanceClient, RateLimitError
from controller.store import Store


T0 = datetime.fromisoformat("2027-01-04T11:00:00+05:30")


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _workflow(
    *,
    max_requests: int = 3,
    allowed_dims: list[str] | None = None,
    max_shift: float = 0.5,
    auto_enabled: bool = True,
) -> dict:
    return {
        "workflow_id": "nw_renewal",
        "tenant_id": "northwind_life",
        "objective": {
            "primary": "engagement_rate",
            "guardrail": {"pickup_rate_max_drop": 0.02},
        },
        "governance": {
            "approver_hours": [10, 19],
            "approver_latency_minutes": [30, 240],
            "proposal_ttl_hours": 24,
            "apply_window_hours": 2,
            "max_requests_per_24h": max_requests,
            "base_reject_rate": 0.1,
            "min_validation_pass_rate": 0.97,
            "auto_apply": {
                "enabled": auto_enabled,
                "allowed_dims": allowed_dims
                if allowed_dims is not None
                else ["schedule", "caller_id", "welcome_message", "voice"],
                "max_shift": max_shift,
            },
        },
    }


def _config(caller: dict | None = None, voice: dict | None = None) -> dict:
    return {
        "cohorts": [],
        "allocations": {
            "default": {
                "welcome_message": {"W1": 1.0},
                "voice": voice or {"V1": 1.0},
                "caller_id": caller or {"D1": 0.5, "D2": 0.5},
            }
        },
        "prompt_version": "P1",
        "schedule": {"hour_weights": {}, "lead_preferred_hour": {}},
    }


class FakeClient:
    def __init__(self, workflow: dict, config: dict):
        self.workflow = copy.deepcopy(workflow)
        self.config_record = {
            "workflow_id": workflow["workflow_id"],
            "version": 1,
            "actor": "business_default",
            "applied_at": None,
            "config": copy.deepcopy(config),
        }
        self.approvals: dict[str, dict] = {}
        self.request_calls: list[dict] = []
        self.apply_calls: list[tuple[str, str]] = []
        self.put_calls: list[tuple[str, dict]] = []
        self.request_errors: list[Exception | None] = []
        self.config_versions: list[int] = []
        self.apply_error: Exception | None = None
        self.put_error: Exception | None = None
        self._next_approval = 1

    def get_state(self) -> dict:
        return {"ts": _iso(T0)}

    def get_workflow(self, workflow_id: str) -> dict:
        return copy.deepcopy(self.workflow)

    def get_config(self, workflow_id: str) -> dict:
        if self.config_versions:
            self.config_record["version"] = self.config_versions.pop(0)
        return copy.deepcopy(self.config_record)

    def list_approvals(
        self, workflow_id: str | None = None, status: str | None = None
    ) -> list[dict]:
        result = []
        for approval in self.approvals.values():
            if workflow_id is not None and approval["workflow_id"] != workflow_id:
                continue
            if status is not None and approval["status"] != status:
                continue
            result.append(copy.deepcopy(approval))
        return result

    def get_approval(self, approval_id: str) -> dict:
        return copy.deepcopy(self.approvals[approval_id])

    def request_approval(self, body: dict) -> dict:
        self.request_calls.append(copy.deepcopy(body))
        if self.request_errors:
            error = self.request_errors.pop(0)
            if error is not None:
                raise error
        for approval in self.approvals.values():
            if approval.get("proposal_id") == body["proposal_id"]:
                return copy.deepcopy(approval)
        approval_id = f"apr_{self._next_approval:05d}"
        self._next_approval += 1
        approval = {
            "approval_id": approval_id,
            "workflow_id": body["workflow_id"],
            "proposal_id": body["proposal_id"],
            "status": "pending",
            "created_at": _iso(T0),
            "expires_at": _iso(T0 + timedelta(hours=24)),
            "apply_by": None,
            "reasons": [],
        }
        self.approvals[approval_id] = approval
        return copy.deepcopy(approval)

    def apply_approval(self, workflow_id: str, approval_id: str) -> dict:
        self.apply_calls.append((workflow_id, approval_id))
        if self.apply_error is not None:
            raise self.apply_error
        approval = self.approvals[approval_id]
        approval["status"] = "applied"
        self.config_record["version"] += 1
        return {"version": self.config_record["version"], "approval_id": approval_id}

    def put_config(self, workflow_id: str, body: dict) -> dict:
        self.put_calls.append((workflow_id, copy.deepcopy(body)))
        if self.put_error is not None:
            raise self.put_error
        self.config_record["version"] += 1
        self.config_record["config"] = copy.deepcopy(body["proposed_config"])
        return {"version": self.config_record["version"]}


def _gov(store: Store, client: FakeClient) -> GovernanceClient:
    return GovernanceClient(client, store, log=lambda message: None)


def _propose(
    gov: GovernanceClient,
    config: dict,
    *,
    purpose: str = "test",
    sim_ts: datetime = T0,
    impact_score: float = 0.5,
):
    return gov.propose(
        "nw_renewal",
        config,
        purpose,
        sim_ts=_iso(sim_ts),
        change_summary="shift caller mix",
        diff={"allocations.default.caller_id": config["allocations"]["default"]["caller_id"]},
        affected_leads=250,
        dry_run={"replay_hours": 6},
        impact_score=impact_score,
        rationale="observed caller health",
    )


def test_deterministic_id_and_duplicate_reconciled(tmp_path):
    workflow = _workflow()
    config = _config(caller={"D1": 1.0})
    client = FakeClient(workflow, _config())
    store = Store(tmp_path / "store.db")
    gov = _gov(store, client)

    proposal_id = gov.proposal_id("nw_renewal", 1, config, "duplicate")
    assert proposal_id == gov.proposal_id("nw_renewal", 1, config, "duplicate")
    client.approvals["apr_09000"] = {
        "approval_id": "apr_09000",
        "workflow_id": "nw_renewal",
        "proposal_id": proposal_id,
        "status": "pending",
        "expires_at": _iso(T0 + timedelta(hours=24)),
        "apply_by": None,
        "reasons": [],
    }
    client.request_errors = [SimError(409, {"detail": "duplicate_proposal_id"})]

    result = _propose(gov, config, purpose="duplicate")

    assert result["status"] == "pending"
    assert result["approval_id"] == "apr_09000"
    assert len(client.request_calls) == 1
    assert store.get_proposal(proposal_id)["status"] == "pending"
    store.close()


def test_stale_base_rebuilds_and_reproposes(tmp_path):
    workflow = _workflow()
    config = _config(caller={"D1": 1.0})
    client = FakeClient(workflow, _config())
    client.config_versions = [1, 2]
    client.request_errors = [SimError(409, {"detail": "stale_base_version"}), None]
    store = Store(tmp_path / "store.db")
    gov = _gov(store, client)

    result = _propose(gov, config)

    assert len(client.request_calls) == 2
    assert client.request_calls[0]["base_version"] == 1
    assert client.request_calls[1]["base_version"] == 2
    assert client.request_calls[0]["proposal_id"] != client.request_calls[1]["proposal_id"]
    old = store.get_proposal(client.request_calls[0]["proposal_id"])
    assert old["status"] == "stale"
    assert result["status"] == "pending"
    store.close()


def test_pending_past_expiry_is_marked_expired(tmp_path):
    workflow = _workflow()
    config = _config(caller={"D1": 1.0})
    client = FakeClient(workflow, _config())
    store = Store(tmp_path / "store.db")
    gov = _gov(store, client)

    result = _propose(gov, config)
    approval_id = result["approval_id"]
    client.approvals[approval_id]["expires_at"] = _iso(T0 + timedelta(hours=1))

    gov.reconcile_all(sim_ts=_iso(T0 + timedelta(hours=2)))

    proposal = store.get_proposal(result["proposal_id"])
    assert proposal["status"] == "expired"
    audit = store.conn.execute(
        "SELECT kind, evidence FROM audit ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert audit[0] == "proposal_expired"
    assert "proposal_ttl_elapsed" in audit[1]
    store.close()


def test_approved_past_apply_window_is_marked_lapsed(tmp_path):
    workflow = _workflow()
    config = _config(caller={"D1": 1.0})
    client = FakeClient(workflow, _config())
    store = Store(tmp_path / "store.db")
    gov = _gov(store, client)

    result = _propose(gov, config)
    approval_id = result["approval_id"]
    client.approvals[approval_id]["status"] = "approved"
    client.approvals[approval_id]["apply_by"] = _iso(T0 + timedelta(hours=1))

    gov.reconcile_all(sim_ts=_iso(T0 + timedelta(hours=2)))

    assert store.get_proposal(result["proposal_id"])["status"] == "lapsed"
    assert client.apply_calls == []
    store.close()


def test_24h_cap_blocks_new_request(tmp_path):
    workflow = _workflow(max_requests=3)
    client = FakeClient(workflow, _config())
    store = Store(tmp_path / "store.db")
    gov = _gov(store, client)
    for index in range(3):
        store.save_proposal(
            f"old_{index}",
            "nw_renewal",
            "pending",
            None,
            1,
            {"proposal_id": f"old_{index}"},
            _iso(T0 - timedelta(hours=1)),
        )

    with pytest.raises(RateLimitError):
        _propose(gov, _config(caller={"D1": 1.0}))

    assert client.request_calls == []
    store.close()


def test_closed_approver_hours_queue_then_send_before_reopen(tmp_path):
    workflow = _workflow()
    config = _config(caller={"D1": 1.0})
    client = FakeClient(workflow, _config())
    store = Store(tmp_path / "store.db")
    gov = _gov(store, client)

    queued = _propose(
        gov,
        config,
        sim_ts=datetime.fromisoformat("2027-01-04T20:00:00+05:30"),
    )

    assert queued["status"] == "queued"
    assert client.request_calls == []

    gov.process(sim_ts=_iso(datetime.fromisoformat("2027-01-05T09:45:00+05:30")))

    proposal = store.get_proposal(queued["proposal_id"])
    assert proposal["status"] == "pending"
    assert len(client.request_calls) == 1
    store.close()


def test_crash_between_intent_and_api_recovers_same_id(tmp_path):
    workflow = _workflow()
    config = _config(caller={"D1": 1.0})
    client = FakeClient(workflow, _config())
    store = Store(tmp_path / "store.db")
    gov = _gov(store, client)

    proposal_id = gov.proposal_id("nw_renewal", 1, config, "crash")
    body = gov._make_body(
        workflow,
        proposal_id,
        1,
        config,
        _config(),
        "shift caller mix",
        {"caller_id": {"D1": 1.0}},
        250,
        {"replay_hours": 6},
        0.5,
        "observed caller health",
        None,
    )
    store.save_proposal(
        proposal_id,
        "nw_renewal",
        "intent",
        None,
        1,
        body,
        _iso(T0),
        _iso(T0 + timedelta(hours=24)),
    )

    recovered = _gov(store, client)
    recovered.reconcile_all(sim_ts=_iso(T0))

    assert len(client.request_calls) == 1
    assert client.request_calls[0]["proposal_id"] == proposal_id
    assert store.get_proposal(proposal_id)["status"] == "pending"
    store.close()


def test_auto_apply_allowed_and_blocked(tmp_path):
    workflow = _workflow(allowed_dims=["caller_id"], max_shift=0.2)
    client = FakeClient(workflow, _config())
    store = Store(tmp_path / "store.db")
    gov = _gov(store, client)
    old = _config()

    allowed, reasons = gov.auto_apply_allowed(
        workflow, old, _config(caller={"D1": 0.5, "D2": 0.4, "D3": 0.1})
    )
    assert allowed is True
    assert reasons == []

    blocked, reasons = gov.auto_apply_allowed(
        workflow, old, _config(caller={"D2": 1.0})
    )
    assert blocked is False
    assert "max_shift" in reasons[0]

    allowed, reasons = gov.auto_apply_allowed(
        workflow, old, _config(voice={"V2": 1.0})
    )
    assert allowed is False
    assert "not allowed" in reasons[0]
    store.close()


def test_try_auto_apply_uses_put_only_when_locally_allowed(tmp_path):
    workflow = _workflow(allowed_dims=["caller_id"], max_shift=0.5)
    client = FakeClient(workflow, _config())
    store = Store(tmp_path / "store.db")
    gov = _gov(store, client)

    blocked = gov.try_auto_apply(
        "nw_renewal",
        _config(voice={"V2": 1.0}),
        sim_ts=_iso(T0),
        rationale="bad dimension",
    )
    assert blocked["applied"] is False
    assert client.put_calls == []

    result = gov.try_auto_apply(
        "nw_renewal",
        _config(caller={"D1": 0.5, "D2": 0.4, "D3": 0.1}),
        sim_ts=_iso(T0),
        rationale="spread caller load",
        affected_leads=100,
    )
    assert result["applied"] is True
    assert len(client.put_calls) == 1
    assert client.put_calls[0][1]["proposal_id"] == result["proposal_id"]
    store.close()


def test_proposal_sent_ts_uses_approval_created_at(tmp_path):
    workflow = _workflow()
    client = FakeClient(workflow, _config())
    store = Store(tmp_path / "store.db")
    gov = _gov(store, client)

    result = _propose(gov, _config(caller={"D1": 1.0}))

    proposal = store.get_proposal(result["proposal_id"])
    assert proposal["sent_ts"] == _iso(T0)
    assert proposal["created_ts"] == _iso(T0)
    store.close()


def test_24h_cap_counts_from_actual_send_time(tmp_path):
    client = FakeClient(_workflow(), _config())
    store = Store(tmp_path / "store.db")
    store.save_proposal(
        "recent",
        "nw_renewal",
        "pending",
        None,
        1,
        {"proposal_id": "recent"},
        _iso(T0 - timedelta(hours=1)),
        sent_ts=_iso(T0 - timedelta(hours=23)),
    )
    store.save_proposal(
        "old",
        "nw_renewal",
        "pending",
        None,
        1,
        {"proposal_id": "old"},
        _iso(T0 - timedelta(hours=2)),
        sent_ts=_iso(T0 - timedelta(hours=25)),
    )
    store.save_proposal(
        "legacy",
        "nw_renewal",
        "pending",
        None,
        1,
        {"proposal_id": "legacy"},
        _iso(T0 - timedelta(hours=4)),
    )

    assert store.count_requests_24h("nw_renewal", _iso(T0)) == 2
    store.close()


def test_ttl_infeasible_proposal_is_queued_not_sent(tmp_path):
    workflow = _workflow()
    workflow["governance"]["proposal_ttl_hours"] = 12
    client = FakeClient(workflow, _config())
    store = Store(tmp_path / "store.db")
    gov = _gov(store, client)

    queued = _propose(
        gov,
        _config(caller={"D1": 1.0}),
        sim_ts=datetime.fromisoformat("2027-01-04T18:30:00+05:30"),
    )

    assert queued["status"] == "queued"
    assert client.request_calls == []
    audit = store.conn.execute(
        "SELECT kind, reason FROM audit ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert audit[0] == "proposal_queued"
    assert "TTL" in audit[1]

    gov.process(sim_ts=_iso(datetime.fromisoformat("2027-01-05T09:45:00+05:30")))
    assert store.get_proposal(queued["proposal_id"])["status"] == "pending"
    assert len(client.request_calls) == 1
    store.close()


def test_ttl_feasible_before_reopen_is_sent_early(tmp_path):
    workflow = _workflow()
    workflow["governance"]["proposal_ttl_hours"] = 12
    client = FakeClient(workflow, _config())
    store = Store(tmp_path / "store.db")
    gov = _gov(store, client)

    result = _propose(
        gov,
        _config(caller={"D1": 1.0}),
        sim_ts=datetime.fromisoformat("2027-01-05T09:45:00+05:30"),
    )

    assert result["status"] == "pending"
    assert len(client.request_calls) == 1
    store.close()
