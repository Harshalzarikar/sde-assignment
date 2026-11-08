"""Caller-ID health detector tests using synthetic aggregate counts."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta
import json

from controller.detect import CallerIdHealthDetector, DetectorConfig
from controller.governance import GovernanceClient
from controller.store import Store


T0 = datetime.fromisoformat("2027-01-04T12:00:00+05:30")
WORKFLOW_ID = "wf_test"

DETECTOR_CONFIG = DetectorConfig(
    window_hours=1,
    windows_required=2,
    min_dials=50,
    min_baseline_dials=50,
    baseline_hours=2,
    alpha=0.01,
    min_drop=0.10,
    exploration_share=0.05,
    min_recovery_dials=10,
    recovery_windows=2,
    recovery_tolerance=0.03,
    recovery_step=0.20,
    cooldown_hours=0,
)


def _workflow() -> dict:
    return {
        "workflow_id": WORKFLOW_ID,
        "objective": {
            "primary": "pickup_rate",
            "guardrail": {"pickup_rate_max_drop": 0.02},
        },
        "governance": {
            "approver_hours": [0, 24],
            "approver_latency_minutes": [1, 5],
            "proposal_ttl_hours": 24,
            "apply_window_hours": 2,
            "max_requests_per_24h": 5,
            "auto_apply": {
                "enabled": True,
                "allowed_dims": ["caller_id"],
                "max_shift": 0.5,
            },
        },
    }


def _config(caller: dict | None = None) -> dict:
    return {
        "cohorts": [],
        "allocations": {
            "default": {
                "welcome_message": {"W1": 1.0},
                "voice": {"V1": 1.0},
                "caller_id": caller or {"D1": 0.45, "D2": 0.45, "D3": 0.10},
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
        self.put_calls: list[dict] = []
        self.request_calls: list[dict] = []

    def get_state(self) -> dict:
        return {"ts": T0.isoformat()}

    def get_workflows(self) -> list[dict]:
        return [copy.deepcopy(self.workflow)]

    def get_workflow(self, workflow_id: str) -> dict:
        return copy.deepcopy(self.workflow)

    def get_config(self, workflow_id: str) -> dict:
        return copy.deepcopy(self.config_record)

    def put_config(self, workflow_id: str, body: dict) -> dict:
        self.put_calls.append(copy.deepcopy(body))
        self.config_record["version"] += 1
        self.config_record["config"] = copy.deepcopy(body["proposed_config"])
        return {"version": self.config_record["version"]}

    def list_approvals(self, workflow_id=None, status=None) -> list[dict]:
        return []

    def request_approval(self, body: dict) -> dict:
        self.request_calls.append(copy.deepcopy(body))
        return {
            "approval_id": "apr_test",
            "workflow_id": body["workflow_id"],
            "proposal_id": body["proposal_id"],
            "status": "pending",
            "created_at": T0.isoformat(),
            "expires_at": (T0 + timedelta(hours=24)).isoformat(),
            "apply_by": None,
            "reasons": [],
        }

    def apply_approval(self, workflow_id: str, approval_id: str) -> dict:
        raise AssertionError("detector should use auto-apply in these tests")


def _add_counts(
    store: Store,
    caller_id: str,
    hour: datetime,
    dials: int,
    connected: int,
    engaged: int = 0,
) -> None:
    store.conn.execute(
        "INSERT INTO counts "
        "(workflow_id, cohort_id, dim, variant, hour_bucket, dials, connected, engaged) "
        "VALUES (?, 'default', 'caller_id', ?, ?, ?, ?, ?) "
        "ON CONFLICT(workflow_id, cohort_id, dim, variant, hour_bucket) "
        "DO UPDATE SET dials = excluded.dials, connected = excluded.connected, "
        "engaged = excluded.engaged",
        (
            WORKFLOW_ID,
            caller_id,
            hour.strftime("%Y-%m-%dT%H"),
            dials,
            connected,
            engaged,
        ),
    )


def _add_hour(
    store: Store,
    hour: datetime,
    d3_connected: int,
    d3_dials: int = 500,
    others_connected: int = 300,
    others_dials: int = 500,
) -> None:
    for caller_id in ("D1", "D2"):
        _add_counts(store, caller_id, hour, others_dials, others_connected)
    _add_counts(store, "D3", hour, d3_dials, d3_connected)


def _seed_baseline(store: Store) -> None:
    for offset in (3, 4):
        _add_hour(store, T0 - timedelta(hours=offset), d3_connected=300)


def _detector(
    store: Store,
    workflow: dict | None = None,
    config: dict | None = None,
) -> tuple[CallerIdHealthDetector, FakeClient]:
    client = FakeClient(workflow or _workflow(), config or _config())
    governance = GovernanceClient(client, store, log=lambda message: None)
    detector = CallerIdHealthDetector(
        client, store, governance, DETECTOR_CONFIG, log=lambda message: None
    )
    return detector, client


def _weights(client: FakeClient) -> dict[str, float]:
    return client.config_record["config"]["allocations"]["default"]["caller_id"]


def test_healthy_caller_takes_no_action(tmp_path):
    store = Store(tmp_path / "store.db")
    _seed_baseline(store)
    for offset in (1, 2):
        _add_hour(store, T0 - timedelta(hours=offset), d3_connected=300)
    detector, client = _detector(store)

    actions = detector.tick(T0.isoformat())

    assert actions == []
    assert client.put_calls == []
    assert _weights(client)["D3"] == 0.10
    store.close()


def test_daily_no_action_audit_written_once_per_day(tmp_path):
    store = Store(tmp_path / "store.db")
    _seed_baseline(store)
    for offset in (1, 2):
        _add_hour(store, T0 - timedelta(hours=offset), d3_connected=300)
    detector, client = _detector(store)

    detector.tick(T0.isoformat())
    detector.tick((T0 + timedelta(minutes=15)).isoformat())
    detector.tick((T0 + timedelta(hours=1)).isoformat())
    rows = store.conn.execute(
        "SELECT kind, evidence FROM audit WHERE kind = 'caller_id_daily_check'"
    ).fetchall()
    assert len(rows) == 1
    evidence = json.loads(rows[0][1])
    assert evidence["decision"] == "no_action"
    assert evidence["callers_compared"] == ["D1", "D2", "D3"]
    assert evidence["caller_evidence"][2]["dials"] > 0
    assert "p_vs_healthy" in evidence["caller_evidence"][2]
    assert "p_vs_baseline" in evidence["caller_evidence"][2]
    assert len(evidence["caller_evidence"][2]["windows"]) == 2

    detector.tick((T0 + timedelta(days=1)).isoformat())
    rows = store.conn.execute(
        "SELECT COUNT(*) FROM audit WHERE kind = 'caller_id_daily_check'"
    ).fetchone()
    assert rows[0] == 2
    assert client.put_calls == []
    store.close()


def test_clear_bad_caller_shifts_to_exploration_share(tmp_path):
    store = Store(tmp_path / "store.db")
    _seed_baseline(store)
    for offset in (1, 2):
        _add_hour(store, T0 - timedelta(hours=offset), d3_connected=50)
    detector, client = _detector(store)

    actions = detector.tick(T0.isoformat())

    assert len(actions) == 1
    assert actions[0]["action"] == "fix"
    assert actions[0]["status"] == "applied"
    assert abs(_weights(client)["D3"] - 0.05) < 1e-9
    audit = store.conn.execute(
        "SELECT kind FROM audit ORDER BY id"
    ).fetchall()
    kinds = [row[0] for row in audit]
    assert "caller_id_degraded" in kinds
    assert "caller_id_fix_applied" in kinds
    store.close()


def test_single_hour_blip_takes_no_action(tmp_path):
    store = Store(tmp_path / "store.db")
    _seed_baseline(store)
    _add_hour(store, T0 - timedelta(hours=1), d3_connected=300)
    _add_hour(store, T0 - timedelta(hours=2), d3_connected=50)
    detector, client = _detector(store)

    actions = detector.tick(T0.isoformat())

    assert actions == []
    assert client.put_calls == []
    store.close()


def test_small_sample_takes_no_action(tmp_path):
    store = Store(tmp_path / "store.db")
    _seed_baseline(store)
    for offset in (1, 2):
        _add_hour(
            store,
            T0 - timedelta(hours=offset),
            d3_connected=2,
            d3_dials=20,
        )
    detector, client = _detector(store)

    actions = detector.tick(T0.isoformat())

    assert actions == []
    assert client.put_calls == []
    store.close()


def test_exploration_share_scales_with_volume_to_keep_min_dials(tmp_path):
    store = Store(tmp_path / "store.db")
    for offset in (3, 4):
        for caller_id in ("D1", "D2", "D3"):
            _add_counts(store, caller_id, T0 - timedelta(hours=offset), 100, 60)
    for offset in (1, 2):
        for caller_id in ("D1", "D2"):
            _add_counts(store, caller_id, T0 - timedelta(hours=offset), 100, 60)
        _add_counts(store, "D3", T0 - timedelta(hours=offset), 100, 10)
    detector, client = _detector(
        store, config=_config({"D1": 0.25, "D2": 0.25, "D3": 0.50})
    )

    actions = detector.tick(T0.isoformat())

    assert actions[0]["status"] == "applied"
    expected_share = DETECTOR_CONFIG.min_dials / 300.0
    assert abs(_weights(client)["D3"] - expected_share) < 1e-9
    assert expected_share > DETECTOR_CONFIG.exploration_share
    store.close()


def test_shift_over_max_shift_routes_to_approval_not_put(tmp_path):
    store = Store(tmp_path / "store.db")
    _seed_baseline(store)
    for offset in (1, 2):
        _add_hour(store, T0 - timedelta(hours=offset), d3_connected=50)
    workflow = _workflow()
    workflow["governance"]["auto_apply"]["max_shift"] = 0.20
    detector, client = _detector(
        store, workflow, _config({"D1": 0.25, "D2": 0.25, "D3": 0.50})
    )

    actions = detector.tick(T0.isoformat())

    assert len(actions) == 1
    assert actions[0]["status"] == "pending"
    assert client.put_calls == []
    assert len(client.request_calls) == 1
    store.close()


def test_single_caller_logs_once(tmp_path):
    store = Store(tmp_path / "store.db")
    client = FakeClient(_workflow(), _config({"D1": 1.0}))
    governance = GovernanceClient(client, store, log=lambda message: None)
    messages: list[str] = []
    detector = CallerIdHealthDetector(
        client, store, governance, DETECTOR_CONFIG, log=messages.append
    )

    detector.tick(T0.isoformat())
    detector.tick((T0 + timedelta(minutes=15)).isoformat())

    matches = [
        message
        for message in messages
        if "no alternative caller_id to compare" in message
    ]
    assert len(matches) == 1
    store.close()


def test_disabled_auto_apply_routes_fix_through_approval(tmp_path):
    store = Store(tmp_path / "store.db")
    _seed_baseline(store)
    for offset in (1, 2):
        _add_hour(store, T0 - timedelta(hours=offset), d3_connected=50)
    workflow = _workflow()
    workflow["governance"]["auto_apply"] = {
        "enabled": False,
        "allowed_dims": [],
        "max_shift": 0.0,
    }
    detector, client = _detector(store, workflow)

    actions = detector.tick(T0.isoformat())

    assert len(actions) == 1
    assert actions[0]["status"] == "pending"
    assert client.put_calls == []
    assert len(client.request_calls) == 1
    store.close()


def test_supervisor_caller_edit_resets_recovery_state(tmp_path):
    store = Store(tmp_path / "store.db")
    _seed_baseline(store)
    for offset in (1, 2):
        _add_hour(store, T0 - timedelta(hours=offset), d3_connected=50)
    detector, client = _detector(store)
    governance = detector.governance
    governance.detector_reset = detector.reset_workflow

    actions = detector.tick(T0.isoformat())
    assert actions[0]["status"] == "applied"
    assert detector._load_state(WORKFLOW_ID, "D3") is not None

    client.config_record["version"] = 2
    client.config_record["config"]["allocations"]["default"]["caller_id"] = {
        "D1": 0.40,
        "D2": 0.40,
        "D3": 0.20,
    }
    store.save_chunk(
        "events",
        0,
        [
            {
                "event_id": 9100,
                "type": "config_changed",
                "ts": T0.isoformat(),
                "workflow_id": WORKFLOW_ID,
                "version": 2,
                "actor": "supervisor",
                "changed_dims": ["caller_id"],
            }
        ],
    )

    governance.process_supervisor_edits(sim_ts=(T0 + timedelta(minutes=15)).isoformat())

    assert detector._load_state(WORKFLOW_ID, "D3") is None
    baseline = store.get_meta(f"caller_health_baseline:{WORKFLOW_ID}")
    assert baseline is not None and '"config_version": 2' in baseline
    assert client.config_record["config"]["allocations"]["default"]["caller_id"] == {
        "D1": 0.40,
        "D2": 0.40,
        "D3": 0.20,
    }
    store.close()


def test_recovery_raises_weight_after_consecutive_windows(tmp_path):
    store = Store(tmp_path / "store.db")
    _seed_baseline(store)
    for offset in (1, 2):
        _add_hour(store, T0 - timedelta(hours=offset), d3_connected=50)
    detector, client = _detector(store)

    actions = detector.tick(T0.isoformat())
    assert actions[0]["status"] == "applied"
    assert abs(_weights(client)["D3"] - 0.05) < 1e-9

    later = T0 + timedelta(hours=2)
    for offset in (1, 2):
        _add_hour(
            store,
            later - timedelta(hours=offset),
            d3_connected=12,
            d3_dials=20,
        )

    actions = detector.tick(later.isoformat())

    assert len(actions) == 1
    assert actions[0]["action"] == "recovery"
    assert abs(_weights(client)["D3"] - 0.10) < 1e-9
    store.close()
