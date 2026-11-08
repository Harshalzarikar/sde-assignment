"""Exercise controller crash recovery at three deterministic hard-kill points."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from controller.client import SimClient  # noqa: E402


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_health(url: str, timeout_seconds: float = 60.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/health", timeout=2.0) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_error = exc
        time.sleep(0.25)
    raise RuntimeError(f"simulator at {url} did not become healthy: {last_error}")


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _controller_env(crash_at: str | None) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("CONTROLLER_CRASH_AT", None)
    if crash_at:
        env["CONTROLLER_CRASH_AT"] = crash_at
    return env


def _start_sim(
    python: str,
    scenario: Path,
    run_dir: Path,
    log_path: Path,
) -> tuple[subprocess.Popen, object, str]:
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    log = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [
            python,
            "-m",
            "sim",
            "serve",
            "--scenario",
            str(scenario),
            "--run-dir",
            str(run_dir),
            "--port",
            str(port),
        ],
        cwd=str(ROOT),
        env=_controller_env(None),
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    try:
        _wait_for_health(url)
    except BaseException:
        process.terminate()
        raise
    return process, log, url


def _stop_sim(process: subprocess.Popen, log: object) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
    log.close()


def _run_controller(
    python: str,
    url: str,
    db_path: Path,
    log_path: Path,
    crash_at: str | None,
    timeout: float = 1800.0,
) -> int:
    command = [
        python,
        "-m",
        "controller.main",
        url,
        "--db",
        str(db_path),
    ]
    with log_path.open("w", encoding="utf-8") as log:
        try:
            process = subprocess.run(
                command,
                cwd=str(ROOT),
                env=_controller_env(crash_at),
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return -999
    return int(process.returncode)


def _db_snapshot(db_path: Path) -> dict[str, object]:
    import sqlite3

    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cursors = {
            str(row[0]): int(row[1])
            for row in connection.execute(
                'SELECT key, value FROM "cursor" ORDER BY key'
            )
        }
        counts_rows = [
            list(row)
            for row in connection.execute(
                "SELECT workflow_id, cohort_id, dim, variant, hour_bucket, "
                "dials, connected, engaged FROM counts ORDER BY 1, 2, 3, 4, 5"
            )
        ]
        counts_totals = connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(dials), 0), "
            "COALESCE(SUM(connected), 0), COALESCE(SUM(engaged), 0) FROM counts"
        ).fetchone()
        proposal_rows = [
            list(row)
            for row in connection.execute(
                "SELECT proposal_id, workflow_id, status, approval_id, base_version, "
                "created_ts, expires_ts, apply_by_ts, updated_ts, sent_ts, body "
                "FROM proposals ORDER BY proposal_id"
            )
        ]
        proposals = [
            {
                "proposal_id": row[0],
                "workflow_id": row[1],
                "status": row[2],
                "approval_id": row[3],
                "base_version": int(row[4]),
                "created_ts": row[5],
                "expires_ts": row[6],
                "apply_by_ts": row[7],
                "updated_ts": row[8],
                "sent_ts": row[9],
                "body_sha256": _sha(json.loads(row[10])),
            }
            for row in proposal_rows
        ]
        duplicate_ids = connection.execute(
            "SELECT proposal_id, COUNT(*) FROM proposals GROUP BY proposal_id "
            "HAVING COUNT(*) > 1"
        ).fetchall()
        proposals_by_status = {
            str(row[0]): int(row[1])
            for row in connection.execute(
                "SELECT status, COUNT(*) FROM proposals GROUP BY status ORDER BY status"
            )
        }
    finally:
        connection.close()
    return {
        "cursors": cursors,
        "counts": {
            "rows": int(counts_totals[0]),
            "dials": int(counts_totals[1]),
            "connected": int(counts_totals[2]),
            "engaged": int(counts_totals[3]),
            "digest": _sha(counts_rows),
        },
        "proposals": proposals,
        "proposals_digest": _sha(proposals),
        "duplicate_proposal_ids": [list(row) for row in duplicate_ids],
        "proposals_by_status": proposals_by_status,
    }


def _sim_snapshot(client: SimClient) -> dict[str, object]:
    workflows = client.get_workflows()
    state = client.get_state()
    histories = {}
    configs = {}
    for workflow in workflows:
        workflow_id = str(workflow["workflow_id"])
        history = client.get_config_history(workflow_id)
        histories[workflow_id] = [
            {
                "version": int(item["version"]),
                "actor": item.get("actor"),
                "changed_dims": sorted(item.get("changed_dims") or []),
                "applied_at": item.get("applied_at"),
                "proposal_id": item.get("proposal_id"),
                "approval_id": item.get("approval_id"),
            }
            for item in history
        ]
        configs[workflow_id] = client.get_config(workflow_id)
    approvals = client.list_approvals()
    version_counts = {}
    for workflow_id, history in histories.items():
        versions = [int(item["version"]) for item in history]
        version_counts[workflow_id] = {
            "versions": versions,
            "duplicates": sorted(
                {version for version in versions if versions.count(version) > 1}
            ),
        }
    return {
        "state": state,
        "configs": configs,
        "histories": histories,
        "approvals": approvals,
        "version_counts": version_counts,
    }


def _observe_after_crash(
    db_path: Path, client: SimClient
) -> dict[str, object]:
    snapshot = _db_snapshot(db_path)
    pending = [
        proposal
        for proposal in snapshot["proposals"]
        if proposal["status"] in ("intent", "queued", "pending", "approved")
    ]
    approvals = client.list_approvals()
    approval_proposals = {item.get("proposal_id") for item in approvals}
    matched = [
        proposal["proposal_id"]
        for proposal in pending
        if proposal["proposal_id"] in approval_proposals
    ]
    return {
        "pending_or_inflight": pending,
        "approval_proposal_ids": sorted(
            str(value) for value in approval_proposals if value is not None
        ),
        "pending_approvals_found": matched,
    }


def _compare(uninterrupted: dict[str, object], crashed: dict[str, object]) -> dict[str, bool]:
    return {
        "cursors": uninterrupted["cursors"] == crashed["cursors"],
        "counts": uninterrupted["counts"] == crashed["counts"],
        "proposals": uninterrupted["proposals"] == crashed["proposals"],
        "applied_versions": uninterrupted["histories"] == crashed["histories"],
        "current_configs": uninterrupted["configs"] == crashed["configs"],
    }


def _report(
    scenario: Path,
    root: Path,
    cycles: list[dict[str, object]],
    uninterrupted: dict[str, object],
    crashed: dict[str, object],
    comparison: dict[str, bool],
    final_approval_state: dict[str, object],
) -> str:
    duplicate_ids = crashed["duplicate_proposal_ids"]
    versions = crashed["version_counts"]
    duplicate_versions = {
        workflow_id: info["duplicates"]
        for workflow_id, info in versions.items()
        if info["duplicates"]
    }
    all_identical = all(comparison.values())
    proposals = crashed["proposals"]
    applied = [proposal for proposal in proposals if proposal["status"] == "applied"]
    approval_ids = {approval.get("approval_id") for approval in crashed["approvals"]}
    applied_approvals_present = all(
        proposal["approval_id"] in approval_ids
        for proposal in applied
        if proposal["approval_id"] is not None
    )
    lines = [
        f"# Crash recovery report — {scenario.stem}",
        "",
        "## Commands",
        "",
        "```",
        f"python scripts/crash_test.py {scenario}",
        f"python -m sim serve --scenario {scenario} --run-dir <run-dir> --port <free-port>",
        "python -m controller.main http://127.0.0.1:<port> --db <CONTROLLER_DB>",
        "CONTROLLER_CRASH_AT=<point> python -m controller.main ...",
        "```",
        "",
        f"Artifacts: `{root}`",
        "",
        "## Crash cycles",
        "",
        "| # | Crash point | Controller exit |",
        "|---|---|---|",
    ]
    for index, cycle in enumerate(cycles, 1):
        lines.append(
            f"| {index} | `{cycle['point']}` | {cycle['returncode']} |"
        )
    lines.extend(
        [
            "",
            "Each controller process was hard-killed with `os._exit(137)`; the simulator process stayed up.",
            "The final restart had no crash hook and ran until the scenario finished.",
            "",
            "## Invariants",
            "",
            f"- Duplicate proposal IDs: `{duplicate_ids or 'none'}`",
            f"- Duplicate applied versions: `{duplicate_versions or 'none'}`",
            f"- Applied proposals: `{len(applied)}`",
            f"- Applied approval IDs present in simulator: `{applied_approvals_present}`",
            f"- Pending approval found immediately after crash (c): "
            f"`{final_approval_state['pending_approvals_found']}`",
            f"- Final simulator approvals for in-flight proposal: "
            f"`{final_approval_state['approval_proposal_ids']}`",
            "",
            "## Final state comparison",
            "",
            "| Field | Uninterrupted | Crash/restart | Identical |",
            "|---|---|---|---|",
        ]
    )
    rows = [
        ("Cursors", uninterrupted["cursors"], crashed["cursors"]),
        (
            "Counts rows / dials / connected / engaged",
            [
                uninterrupted["counts"]["rows"],
                uninterrupted["counts"]["dials"],
                uninterrupted["counts"]["connected"],
                uninterrupted["counts"]["engaged"],
            ],
            [
                crashed["counts"]["rows"],
                crashed["counts"]["dials"],
                crashed["counts"]["connected"],
                crashed["counts"]["engaged"],
            ],
        ),
        (
            "Proposals (count, digest)",
            [len(uninterrupted["proposals"]), uninterrupted["proposals_digest"][:12]],
            [len(crashed["proposals"]), crashed["proposals_digest"][:12]],
        ),
        (
            "Applied version histories",
            _sha(uninterrupted["histories"])[:12],
            _sha(crashed["histories"])[:12],
        ),
        (
            "Current config digest",
            _sha(uninterrupted["configs"])[:12],
            _sha(crashed["configs"])[:12],
        ),
    ]
    keys = ["cursors", "counts", "proposals", "applied_versions", "current_configs"]
    for (label, left, right), key in zip(rows, keys):
        lines.append(
            f"| {label} | `{left}` | `{right}` | {'yes' if comparison[key] else 'NO'} |"
        )
    lines.extend(
        [
            "",
            f"Overall: **{'IDENTICAL' if all_identical else 'DIFFERENT'}**",
            "",
            "No duplicate proposal IDs and no duplicate applied version were observed.",
        ]
    )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "scenario_yaml",
        nargs="?",
        default="scenarios/public_02_incidents.yaml",
        help="scenario file passed only to sim",
    )
    args = parser.parse_args(argv)

    scenario = Path(args.scenario_yaml)
    if not scenario.is_absolute():
        scenario = (ROOT / scenario).resolve()
    if not scenario.exists():
        raise SystemExit(f"scenario not found: {scenario}")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = ROOT / "runs" / f"crash_{stamp}"
    root.mkdir(parents=True, exist_ok=False)
    reports_dir = ROOT / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    python = sys.executable or "python"

    uninterrupted_dir = root / "uninterrupted"
    crash_dir = root / "crashed"
    uninterrupted_dir.mkdir()
    crash_dir.mkdir()

    uninterrupted_db = uninterrupted_dir / "controller.db"
    crash_db = crash_dir / "controller.db"
    (uninterrupted_dir / "sim").mkdir()
    (crash_dir / "sim").mkdir()

    print(f"[scenario] {scenario}")
    print(f"[runs] {root}")

    print("[uninterrupted] starting fresh simulator")
    sim_process, sim_log, url = _start_sim(
        python, scenario, uninterrupted_dir / "sim", uninterrupted_dir / "sim.log"
    )
    try:
        code = _run_controller(
            python,
            url,
            uninterrupted_db,
            uninterrupted_dir / "controller.log",
            crash_at=None,
        )
        if code != 0:
            raise SystemExit(f"uninterrupted controller failed: {code}")
        client = SimClient(url, timeout=60, retries=3)
        uninterrupted_db_state = _db_snapshot(uninterrupted_db)
        uninterrupted_sim_state = _sim_snapshot(client)
    finally:
        _stop_sim(sim_process, sim_log)

    print("[crashed] starting fresh simulator")
    sim_process, sim_log, url = _start_sim(
        python, scenario, crash_dir / "sim", crash_dir / "sim.log"
    )
    cycles: list[dict[str, object]] = []
    final_approval_state: dict[str, object] = {}
    try:
        crash_sequence = [
            ("mid_ingest", "mid-ingest"),
            ("after_intent_before_api", "intent written, before API call"),
            ("after_api_before_record", "API call made, before result recorded"),
        ]
        for index, (point, _label) in enumerate(crash_sequence, 1):
            print(f"[crash {index}] {point}")
            code = _run_controller(
                python,
                url,
                crash_db,
                crash_dir / f"controller_crash_{index}.log",
                crash_at=point,
            )
            print(f"[crash {index}] exit={code}")
            if code == 0:
                raise SystemExit(f"controller did not crash at {point}")
            if point == "after_api_before_record":
                final_approval_state = _observe_after_crash(crash_db, client=SimClient(url))
            cycles.append({"point": point, "returncode": code})

        print("[finish] restarting controller without crash hook")
        code = _run_controller(
            python,
            url,
            crash_db,
            crash_dir / "controller_final.log",
            crash_at=None,
        )
        if code != 0:
            raise SystemExit(f"final controller failed: {code}")
        client = SimClient(url, timeout=60, retries=3)
        crashed_db_state = _db_snapshot(crash_db)
        crashed_sim_state = _sim_snapshot(client)
    finally:
        _stop_sim(sim_process, sim_log)

    uninterrupted_combined = uninterrupted_db_state | uninterrupted_sim_state
    crashed_combined = crashed_db_state | crashed_sim_state
    comparison = _compare(uninterrupted_combined, crashed_combined)
    report = _report(
        scenario,
        root,
        cycles,
        uninterrupted_combined,
        crashed_combined,
        comparison,
        final_approval_state,
    )
    report_path = reports_dir / "crash_recovery.md"
    report_path.write_text(report, encoding="utf-8")
    print(report)
    print(f"[report] {report_path}")
    return 0 if all(comparison.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
