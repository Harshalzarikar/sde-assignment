"""Local testing only: start the simulator, run the controller to the end, grade it, and check its counts.

  python devtools/run_local.py --scenario scenarios/public_01_dev.yaml --name public_01
  python devtools/run_local.py --scenario scenarios/public_01_dev.yaml --name public_01 --crash-after 3

--crash-after N kills the controller (SIGKILL) after N seconds, then restarts it, to prove it resumes correctly.
--days N runs only N simulated days (load measurement), then prints the controller's health table; no grading.
--crash-at-minute M makes the controller hard-exit once simulated time reaches minute M (D0 00:00 = 0), then
restarts it. Repeatable, so it can target a moment such as 'an approval is pending'.
Count check: the controller's notebook must agree exactly with the simulator's own truth file. This harness may
read the run directory; the controller itself never does.
"""
import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "devtools"))
from sim.grader import grade, to_markdown  # noqa: E402
from sim.scenario import load_scenario  # noqa: E402
from sim.world import World  # noqa: E402
from sim_server_stdlib import start  # noqa: E402


def ensure_baseline(scenario: str, base_dir: Path):
    if (base_dir / "_truth" / "aggregates.json").exists():
        return
    print(f"running baseline into {base_dir} ...")
    import shutil
    shutil.rmtree(base_dir, ignore_errors=True)
    w = World(load_scenario(scenario), base_dir)
    while not w.state()["finished"]:
        w.advance(1440)


def run_controller(url: str, db: Path, crash_after: float | None, crash_at_minute: int | None,
                   stop_at_minute: int | None = None) -> int:
    cmd = [sys.executable, "-u", "-m", "controller", "run", "--url", url, "--db", str(db)]
    if stop_at_minute is not None:
        env = {**os.environ, "CONTROLLER_STOP_AT_MINUTE": str(stop_at_minute)}
        return subprocess.Popen(cmd, cwd=ROOT, env=env).wait()
    if crash_at_minute is not None:
        env = {**os.environ, "CONTROLLER_CRASH_AT_MINUTE": str(crash_at_minute)}
        code = subprocess.Popen(cmd, cwd=ROOT, env=env).wait()
        print(f"\n*** controller exited with code {code} at the planned crash; restarting it ***\n")
    proc = subprocess.Popen(cmd, cwd=ROOT)
    if crash_after:
        try:
            proc.wait(timeout=crash_after)
            print("controller finished before the planned crash; try a smaller --crash-after")
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            print(f"\n*** killed the controller with SIGKILL after {crash_after}s; restarting it ***\n")
            proc = subprocess.Popen(cmd, cwd=ROOT)
    return proc.wait()


def check_counts(db: Path, run_dir: Path) -> bool:
    truth = json.loads((run_dir / "_truth" / "aggregates.json").read_text())
    want = defaultdict(lambda: [0, 0, 0])
    for d in truth["days"]:
        t = want[(d["workflow_id"], d["day"])]
        t[0] += d["dials"]
        t[1] += d["connected"]
        t[2] += d["engaged"]
    got = {(w, day): [a, b, c] for w, day, a, b, c in sqlite3.connect(db).execute(
        "SELECT workflow_id, day, SUM(dials), SUM(connected), SUM(engaged) FROM call_counts "
        "WHERE day >= 0 GROUP BY workflow_id, day")}
    bad = [(k, want[k], got.get(k)) for k in sorted(set(want) | set(got)) if want.get(k) != got.get(k)]
    for k, w, g in bad[:10]:
        print(f"  MISMATCH {k}: simulator={w} notebook={g}")
    return not bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--crash-after", type=float)
    ap.add_argument("--crash-at-minute", type=int)
    ap.add_argument("--days", type=int)
    a = ap.parse_args()

    base_dir, run_dir = ROOT / "runs" / f"base_{a.name}", ROOT / "runs" / f"mine_{a.name}"
    db = ROOT / "state" / f"{a.name}.db"
    if a.days is None:
        ensure_baseline(a.scenario, base_dir)

    db.parent.mkdir(exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)

    t0 = time.time()
    server, url = start(a.scenario, str(run_dir))
    print(f"simulator ready at {url} ({time.time() - t0:.1f}s)")
    t1 = time.time()
    code = run_controller(url, db, a.crash_after, a.crash_at_minute,
                          a.days * 1440 if a.days is not None else None)
    server.shutdown()
    print(f"controller exit code {code}, wall time {time.time() - t1:.1f}s")
    if a.days is not None:
        subprocess.run([sys.executable, "-m", "controller", "health", "--db", str(db)], cwd=ROOT)
        sys.exit(code)

    report = ROOT / "reports" / f"{a.name}.md"
    report.parent.mkdir(exist_ok=True)
    report.write_text(to_markdown(grade(str(run_dir), str(base_dir))))
    print(f"grade report: {report.relative_to(ROOT)}")
    ok = check_counts(db, run_dir)
    print("count check:", "MATCH - the notebook agrees exactly with the simulator" if ok else "MISMATCH")
    sys.exit(0 if code == 0 and ok else 1)


if __name__ == "__main__":
    main()
