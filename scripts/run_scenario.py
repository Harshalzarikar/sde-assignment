"""Run a fresh baseline and controller pass for one scenario, then grade it."""

from __future__ import annotations

import argparse
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


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _run_logged(command: list[str], log_path: Path, env: dict[str, str]) -> int:
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.run(
            command,
            cwd=str(ROOT),
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    return int(process.returncode)


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario_yaml", help="scenario file passed only to sim")
    parser.add_argument(
        "--name",
        default=None,
        help="report/run name (default: scenario filename stem)",
    )
    args = parser.parse_args(argv)

    scenario = Path(args.scenario_yaml)
    if not scenario.is_absolute():
        scenario = (ROOT / scenario).resolve()
    if not scenario.exists():
        raise SystemExit(f"scenario not found: {scenario}")
    name = args.name or scenario.stem

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_root = ROOT / "runs" / f"{name}_{stamp}"
    baseline_dir = run_root / "baseline"
    controller_dir = run_root / "controller"
    db_path = run_root / "controller.db"
    reports_dir = ROOT / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / f"{name}.md"
    report_json = reports_dir / f"{name}.json"
    run_root.mkdir(parents=True, exist_ok=False)
    baseline_dir.mkdir()
    controller_dir.mkdir()

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    python = sys.executable or "python"

    print(f"[scenario] {scenario}")
    print(f"[runs] {run_root}")
    print("[baseline] starting")
    baseline_code = _run_logged(
        [
            python,
            "-m",
            "sim",
            "baseline",
            "--scenario",
            str(scenario),
            "--run-dir",
            str(baseline_dir),
        ],
        run_root / "baseline.log",
        env,
    )
    if baseline_code != 0:
        print(f"[baseline] failed ({baseline_code}); see {run_root / 'baseline.log'}")
        return baseline_code

    port = _free_port()
    sim_url = f"http://127.0.0.1:{port}"
    print(f"[controller] starting simulator on {sim_url}")
    sim_log = (run_root / "sim.log").open("w", encoding="utf-8")
    sim_process = subprocess.Popen(
        [
            python,
            "-m",
            "sim",
            "serve",
            "--scenario",
            str(scenario),
            "--run-dir",
            str(controller_dir),
            "--port",
            str(port),
        ],
        cwd=str(ROOT),
        env=env,
        stdout=sim_log,
        stderr=subprocess.STDOUT,
    )
    controller_code = 1
    try:
        _wait_for_health(sim_url)
        print("[controller] running controller")
        controller_code = _run_logged(
            [
                python,
                "-m",
                "controller.main",
                sim_url,
                "--db",
                str(db_path),
            ],
            run_root / "controller.log",
            env,
        )
    finally:
        if sim_process.poll() is None:
            sim_process.terminate()
            try:
                sim_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                sim_process.kill()
                sim_process.wait(timeout=10)
        sim_log.close()

    if controller_code != 0:
        print(f"[controller] failed ({controller_code}); see {run_root / 'controller.log'}")
        return controller_code

    print(f"[grade] writing {report_path}")
    grade_code = _run_logged(
        [
            python,
            "-m",
            "sim",
            "grade",
            "--run",
            str(controller_dir),
            "--baseline",
            str(baseline_dir),
            "--out",
            str(report_path),
        ],
        run_root / "grade.log",
        env,
    )
    if grade_code != 0:
        print(f"[grade] failed ({grade_code}); see {run_root / 'grade.log'}")
        return grade_code

    json_result = subprocess.run(
        [
            python,
            "-m",
            "sim",
            "grade",
            "--run",
            str(controller_dir),
            "--baseline",
            str(baseline_dir),
            "--json",
        ],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    if json_result.returncode != 0:
        print(f"[grade-json] failed ({json_result.returncode})")
        return json_result.returncode
    report_json.write_text(json_result.stdout, encoding="utf-8")
    try:
        parsed = json.loads(json_result.stdout)
    except json.JSONDecodeError:
        parsed = None
    print(f"[grade] report={report_path}")
    print(f"[grade] json={report_json}")
    if isinstance(parsed, dict):
        print(json.dumps(parsed, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
