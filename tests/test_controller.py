"""Tests where a bug would be expensive. Run: python -m pytest -q tests

The last test drives the real simulator (standard-library server) through a hard crash and a restart.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "devtools"))

from controller.callerid import step_toward, total_variation, trial_verdict  # noqa: E402
from controller.experiments import compare  # noqa: E402
from controller.governance import Change, Governance  # noqa: E402
from controller.prompts import poisson_tail, spike_p  # noqa: E402
from controller.segments import compile_cohorts, difference_z  # noqa: E402
from controller.store import Store  # noqa: E402


# ---- statistics
def test_compare_single_stratum_matches_plain_difference():
    rows = [((1, "default"), "B", 1000, 600), ((1, "default"), "A", 1000, 500)]
    c = compare(rows, "B", "A")
    assert c["lift"] == pytest.approx(0.2, abs=1e-3)
    assert c["z"] > 4


def test_compare_is_stratified_against_simpsons_paradox():
    # B is better inside each config version, but most of B's calls fell in the low-engagement version.
    rows = [((1, "default"), "A", 900, 540), ((1, "default"), "B", 100, 65),
            ((2, "default"), "A", 100, 20), ((2, "default"), "B", 900, 225)]
    pooled_b, pooled_a = (65 + 225) / 1000, (540 + 20) / 1000
    assert pooled_b < pooled_a
    assert compare(rows, "B", "A")["lift"] > 0


def test_compare_ignores_strata_where_only_one_option_ran():
    rows = [((1, "default"), "A", 500, 250), ((2, "default"), "B", 500, 400), ((3, "default"), "A", 10, 5)]
    assert compare(rows, "B", "A") is None


def test_poisson_tail_and_spike_rule():
    assert poisson_tail(0, 3.0) == pytest.approx(1.0)
    assert poisson_tail(20, 5.0) < 1e-5
    assert spike_p(1000, 12, 0.01) is None          # 1.2x baseline: not a spike
    assert spike_p(1000, 40, 0.01) < 1e-8           # 4x baseline on 1000 calls: a spike


def test_group_difference_needs_the_lift_to_differ_inside_vs_outside():
    inside = {"lift": 0.20, "se_lift": 0.05}
    assert difference_z(inside, {"lift": 0.18, "se_lift": 0.04}) < 1      # better for everyone: not a group effect
    assert difference_z(inside, {"lift": -0.05, "se_lift": 0.04}) > 2     # better only inside: a group effect


def test_trial_number_must_reach_80_percent_of_established_pickup():
    est = {"D1": (3000, 900), "D2": (3000, 900)}
    assert trial_verdict({**est, "D3": (300, 60)}, "D3", ["D1", "D2"])[0] == "flagged"
    assert trial_verdict({**est, "D3": (300, 88)}, "D3", ["D1", "D2"])[0] == "healthy"


def test_step_toward_respects_max_shift():
    cur, target = {"V1": 1.0}, {"V1": 0.5, "V2": 0.5}
    step = step_toward(cur, target, 0.2)
    assert total_variation(cur, step) <= 0.2 + 1e-9
    assert step_toward(cur, {"V1": 0.9, "V2": 0.1}, 0.2) == {"V1": 0.9, "V2": 0.1}


# ---- cohorts
def test_compile_cohorts_puts_the_overlap_first():
    rules = {"voice": {"stage": "rule", "feature": "age_band", "values": ["60+"], "winner": "V3", "control": "V1"},
             "welcome_message": {"stage": "trial", "feature": "lang", "values": ["hi"], "winner": "W2",
                                 "control": "W1"}}
    cohorts = compile_cohorts(rules)
    assert [c[0] for c in cohorts] == ["grp_age_60plus__lang_hi", "grp_age_60plus", "grp_lang_hi"]
    assert cohorts[0][1] == {"age_band": ["60+"], "lang": ["hi"]}
    assert cohorts[0][2] == {"voice": {"V3": 0.8, "V1": 0.2}, "welcome_message": {"W2": 0.5, "W1": 0.5}}


def test_compile_cohorts_same_feature_uses_the_intersection():
    rules = {"voice": {"stage": "rule", "feature": "age_band", "values": ["46-60", "60+"], "winner": "V3",
                       "control": "V1"},
             "welcome_message": {"stage": "rule", "feature": "age_band", "values": ["18-30"], "winner": "W2",
                                 "control": "W1"}}
    assert [c[0] for c in compile_cohorts(rules)] == ["grp_age_46-60_60plus", "grp_age_18-30"]


def test_compile_cohorts_both_rules_on_the_same_group_give_unique_ids():
    # public_03 arav_cancel_2: greeting and voice trials both on value_band=mid produced 'grp_value_mid' twice (422).
    mid = {"stage": "trial", "feature": "value_band", "values": ["mid"]}
    rules = {"voice": {**mid, "winner": "V4", "control": "V1"}, "welcome_message": {**mid, "winner": "W2", "control": "W1"}}
    assert compile_cohorts(rules) == [("grp_value_mid", {"value_band": ["mid"]},
                                       {"voice": {"V4": 0.5, "V1": 0.5}, "welcome_message": {"W2": 0.5, "W1": 0.5}})]
    rules["voice"]["values"] = ["high", "mid"]
    assert [(c[0], c[1]) for c in compile_cohorts(rules)] == [("grp_value_mid", {"value_band": ["mid"]}),
                                                              ("grp_value_high", {"value_band": ["high"]})]


# ---- governance: batching and withdrawal
class FakeApi:
    def __init__(self, version=3):
        self.version, self.calls = version, []
        self.config = {"cohorts": [], "prompt_version": "P1", "schedule": {"hour_weights": {}, "lead_preferred_hour": {}},
                       "allocations": {"default": {"caller_id": {"D1": 1.0}, "voice": {"V1": 1.0},
                                                   "welcome_message": {"W1": 1.0}}}}

    def get_ok(self, path, **kw):
        if path.endswith("/config"):
            return {"version": self.version, "config": self.config}
        if path == "/approvals":
            return []
        raise AssertionError(path)

    def get(self, path, **kw):
        if path.startswith("/approvals/"):
            return 200, {"status": "approved", "decided_at": "2027-01-04T11:00:00+05:30",
                         "apply_by": "2027-01-04T13:00:00+05:30"}
        raise AssertionError(path)

    def put(self, path, body):
        self.calls.append(("PUT", body))
        self.version += 1
        return 200, {"version": self.version}

    def post(self, path, body):
        self.calls.append(("POST", path, body))
        if getattr(self, "invalid", False):
            return 422, {"error": "invalid_config", "detail": ["cohorts[2].id must be a unique non-empty string"]}
        if path == "/approvals":
            return 200, {"approval_id": "apr_00001", "expires_at": "2027-01-05T10:00:00+05:30"}
        self.version += 1
        return 200, {"version": self.version}


WF = {"w": {"tenant_id": "t", "governance": {"approver_hours": [10, 19], "approver_latency_minutes": [30, 240],
                                             "proposal_ttl_hours": 24, "max_requests_per_24h": 3,
                                             "auto_apply": {"enabled": True, "allowed_dims": ["caller_id"],
                                                            "max_shift": 0.5}}}}


def _change(name, path, dim, alloc):
    def edit(c):
        c["allocations"]["default"][dim] = alloc
    return Change(name, path, edit, summary=f"{name} change", rationale="r", diff={f"allocations.default.{dim}": 1},
                  dry_run={"m": 1}, impact=0.4, affected_leads=10)


def test_submit_sends_one_put_and_one_batched_approval(tmp_path):
    api, store = FakeApi(), Store(str(tmp_path / "n.db"))
    gov = Governance(api, store, WF)
    gov.submit("w", 600, "2027-01-04T10:00:00+05:30", [
        _change("caller_id", "auto", "caller_id", {"D1": 0.5, "D2": 0.5}),
        _change("experiment", "approval", "voice", {"V1": 0.9, "V2": 0.1}),
        _change("groups", "approval", "welcome_message", {"W1": 0.9, "W2": 0.1})])
    puts = [c for c in api.calls if c[0] == "PUT"]
    posts = [c for c in api.calls if c[0] == "POST"]
    assert len(puts) == 1 and len(posts) == 1
    body = posts[0][2]
    assert body["base_version"] == 4  # built on top of the auto-applied config
    assert body["proposed_config"]["allocations"]["default"]["voice"] == {"V1": 0.9, "V2": 0.1}
    assert body["proposed_config"]["allocations"]["default"]["welcome_message"] == {"W1": 0.9, "W2": 0.1}
    assert set(body["dry_run"]) == {"experiment", "groups"}
    # a second submit while the approval is pending sends nothing new
    gov.submit("w", 615, "2027-01-04T10:15:00+05:30", [_change("experiment", "approval", "voice", {"V1": 0.8, "V2": 0.2})])
    assert len([c for c in api.calls if c[0] == "POST"]) == 1


def test_approved_change_is_withdrawn_when_evidence_moved_on(tmp_path):
    api, store = FakeApi(), Store(str(tmp_path / "n.db"))
    gov = Governance(api, store, WF)
    gov.checks.append(lambda row: "evidence changed")
    gov.submit("w", 600, "2027-01-04T10:00:00+05:30", [_change("experiment", "approval", "voice", {"V1": 0.9, "V2": 0.1})])
    gov.tick(660, "2027-01-04T11:00:00+05:30")
    assert store.proposals("w")[0]["status"] == "withdrawn"
    assert not [c for c in api.calls if c[0] == "POST" and c[1].endswith("/apply")]


def test_a_config_refused_as_invalid_is_not_resent(tmp_path):
    api, store = FakeApi(), Store(str(tmp_path / "n.db"))
    api.invalid = True
    gov = Governance(api, store, WF)
    for minute in (600, 615, 630):
        gov.submit("w", minute, "2027-01-04T10:00:00+05:30",
                   [_change("groups", "approval", "voice", {"V1": 0.5, "V2": 0.5})])
    assert len([c for c in api.calls if c[0] == "POST"]) == 1
    kinds = [k for (k,) in store.db.execute("SELECT kind FROM decisions")]
    assert kinds.count("alert") == 1


# ---- notebook
def test_chunk_and_bookmark_are_saved_together(tmp_path):
    store = Store(str(tmp_path / "n.db"))
    key = ("w", 0, 10, 1, "default", "W1", "V1", "D1", "P1")
    store.save_chunk(0, {key: [10, 3, 1, 0]}, {}, [], [])
    with pytest.raises(Exception):
        store.save_chunk(1, {key: [10, 3, 1, 0]}, {}, [("bad",)], [])  # malformed lead row -> whole chunk rolls back
    assert store.last_chunk() == 0
    assert store.db.execute("SELECT SUM(dials) FROM call_counts").fetchone()[0] == 10


# ---- crash and restart against the real simulator
@pytest.mark.slow
def test_crash_mid_run_then_restart_finishes_with_exact_counts(tmp_path):
    from sim_server_stdlib import start
    server, url = start("scenarios/public_01_dev.yaml", str(tmp_path / "run"))
    db = tmp_path / "n.db"
    cmd = [sys.executable, "-m", "controller", "run", "--url", url, "--db", str(db)]
    crashed = subprocess.run(cmd, cwd=ROOT, env={**os.environ, "CONTROLLER_CRASH_AT_MINUTE": "660"},
                             capture_output=True, text=True)
    assert crashed.returncode == 137
    resumed = subprocess.run(cmd, cwd=ROOT, env={**os.environ, "CONTROLLER_STOP_AT_MINUTE": "1440"},
                             capture_output=True, text=True)
    assert resumed.returncode == 0, resumed.stderr
    server.shutdown()
    want = {}
    for f in sorted((tmp_path / "run" / "events").glob("*.jsonl")):
        for line in f.read_text().splitlines():
            ev = json.loads(line)
            if ev["type"] == "call":
                want[ev["workflow_id"]] = want.get(ev["workflow_id"], 0) + 1
    store = Store(str(db))
    got = dict(store.db.execute("SELECT workflow_id, SUM(dials) FROM call_counts WHERE day >= 0 GROUP BY 1"))
    assert got == want
    applied = [p["proposal_id"] for p in store.proposals(statuses=("applied",))]
    assert len(applied) == len(set(applied))
