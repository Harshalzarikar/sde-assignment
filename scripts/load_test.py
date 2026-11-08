"""Run one fresh public load scenario and report controller throughput metrics."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
from pathlib import Path
import re
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


INGEST_RE = re.compile(
    r"\[ingest:(?P<label>[^\]]+)\].*?events=(?P<events>\d+)\s+"
    r"events_per_sec=(?P<rate>[0-9.]+)\s+lag=(?P<lag>\S+)"
)
TICK_RE = re.compile(r"\[tick\].*?duration=(?P<duration>[0-9.]+)")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _log_tail(path: Path, lines: int = 40) -> str:
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "(no simulator log)"
    return "\n".join(content[-lines:])


def _wait_for_health(
    process: subprocess.Popen,
    url: str,
    log_path: Path,
    timeout_seconds: float = 600.0,
) -> float:
    started = time.monotonic()
    deadline = started + timeout_seconds
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        exit_code = process.poll()
        if exit_code is not None:
            raise RuntimeError(
                f"simulator exited early with code {exit_code}\n"
                f"--- simulator log tail ---\n{_log_tail(log_path)}"
            )
        try:
            with urllib.request.urlopen(f"{url}/health", timeout=2.0) as response:
                if response.status == 200:
                    elapsed = time.monotonic() - started
                    print(
                        f"[simulator] healthy after {elapsed:.1f}s",
                        flush=True,
                    )
                    return elapsed
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_error = exc
        time.sleep(5.0)
    raise RuntimeError(
        f"simulator at {url} did not become healthy within "
        f"{timeout_seconds:.0f}s: {last_error}\n"
        f"--- simulator log tail ---\n{_log_tail(log_path)}"
    )


def _sim_serve_processes() -> list[str]:
    if os.name == "nt":
        command = (
            "Get-CimInstance Win32_Process | "
            "Where-Object { $_.CommandLine -match 'sim\\s+serve' } | "
            "ForEach-Object { \"$($_.ProcessId) $($_.CommandLine)\" }"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
        )
    else:
        result = subprocess.run(
            ["ps", "-eo", "pid=,args="],
            capture_output=True,
            text=True,
        )
    return [
        line.strip()
        for line in result.stdout.splitlines()
        if "sim serve" in line and "load_test.py" not in line
    ]


def _warn_existing_simulators() -> None:
    existing = _sim_serve_processes()
    if existing:
        print(
            "[warning] another simulator process is already running; "
            "this run will use a separate fresh process:",
            flush=True,
        )
        for line in existing:
            print(f"  {line}", flush=True)


def _env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("CONTROLLER_CRASH_AT", None)
    return env


if os.name == "nt":
    import ctypes.wintypes

    class _ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.wintypes.DWORD),
            ("PageFaultCount", ctypes.wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]
else:
    _ProcessMemoryCounters = None


def _rss_mb(pid: int) -> float | None:
    if os.name == "nt":
        process_query_information = 0x0400
        process_vm_read = 0x0010
        handle = ctypes.windll.kernel32.OpenProcess(
            process_query_information | process_vm_read, False, int(pid)
        )
        if not handle:
            return None
        try:
            counters = _ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(_ProcessMemoryCounters)
            ok = ctypes.windll.psapi.GetProcessMemoryInfo(
                handle, ctypes.byref(counters), counters.cb
            )
            if not ok:
                return None
            return float(counters.WorkingSetSize) / (1024.0 * 1024.0)
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    status = Path(f"/proc/{int(pid)}/status")
    if not status.exists():
        return None
    for line in status.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("VmRSS:"):
            return float(line.split()[1]) / 1024.0
    return None


def _run_controller(
    python: str,
    url: str,
    db_path: Path,
    log_path: Path,
) -> tuple[int, float, float | None]:
    command = [python, "-m", "controller.main", url, "--db", str(db_path)]
    started = time.perf_counter()
    peak_mb = 0.0
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            command,
            cwd=str(ROOT),
            env=_env(),
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        while process.poll() is None:
            value = _rss_mb(process.pid)
            if value is not None:
                peak_mb = max(peak_mb, value)
            time.sleep(0.1)
        process.wait()
        elapsed = time.perf_counter() - started
    return int(process.returncode), elapsed, peak_mb or None


def _parse_log(path: Path) -> dict[str, object]:
    ingest = []
    tick_durations = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = INGEST_RE.search(line)
        if match:
            lag_text = match.group("lag")
            lag_seconds = (
                float(lag_text[:-1]) if lag_text.endswith("s") and lag_text != "n/a" else None
            )
            ingest.append(
                {
                    "label": match.group("label"),
                    "events": int(match.group("events")),
                    "rate": float(match.group("rate")),
                    "lag_seconds": lag_seconds,
                }
            )
        match = TICK_RE.search(line)
        if match:
            tick_durations.append(float(match.group("duration")))
    tick_rows = [row for row in ingest if row["label"] == "tick"]
    lag_values = [
        float(row["lag_seconds"])
        for row in tick_rows
        if row["lag_seconds"] is not None
    ]
    return {
        "ingest": ingest,
        "tick_events": sum(int(row["events"]) for row in tick_rows),
        "tick_rates": [float(row["rate"]) for row in tick_rows],
        "tick_durations": tick_durations,
        "max_lag_seconds": max(lag_values) if lag_values else None,
        "final_lag_seconds": lag_values[-1] if lag_values else None,
    }


def _db_size(path: Path) -> int:
    return sum(
        Path(f"{path}{suffix}").stat().st_size
        for suffix in ("", "-wal", "-shm")
        if Path(f"{path}{suffix}").exists()
    )


def _write_report(
    scenario: Path,
    root: Path,
    metrics: dict[str, object],
    state: dict[str, object],
    chunks: dict[str, list[dict[str, object]]],
) -> str:
    chunk_items = [
        (int(chunk["events"]), chunk_kind, int(chunk["chunk"]), chunk)
        for chunk_kind, items in chunks.items()
        for chunk in items
        if chunk.get("events") is not None
    ]
    largest = max(chunk_items, default=(0, "n/a", 0, {}))
    largest_bytes = 0
    largest_path = largest[3].get("path") if largest[3] else None
    if largest_path:
        chunk_file = root / "sim" / str(largest_path)
        if chunk_file.exists():
            largest_bytes = chunk_file.stat().st_size
    lines = [
        f"# Load report — {scenario.stem}",
        "",
        "## Exact commands",
        "",
        "```",
        f"python scripts/load_test.py {scenario} --work-root {root.parent}",
        f"python -m sim serve --scenario {scenario} --run-dir {root / 'sim'} --port <free-port>",
        f"python -m controller.main http://127.0.0.1:<port> --db {root / 'controller.db'}",
        "```",
        "",
        "No baseline run and no early stop. One fresh simulator and one fresh controller DB.",
        "",
        "## Controller load",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Events ingested | {metrics['total_events']} |",
        f"| Controller wall time | {metrics['wall_seconds']:.1f} s |",
        f"| Overall ingest rate | {metrics['overall_rate']:.0f} events/s |",
        f"| Mean per-tick ingest rate | {metrics['mean_tick_rate']:.0f} events/s |",
        f"| Peak per-tick ingest rate | {metrics['peak_tick_rate']:.0f} events/s |",
        f"| Mean tick duration | {metrics['mean_tick_seconds'] * 1000:.2f} ms |",
        f"| Slowest tick duration | {metrics['slowest_tick_seconds'] * 1000:.2f} ms |",
        f"| Max ingest lag | {metrics['max_lag_seconds']} s |",
        f"| Final ingest lag | {metrics['final_lag_seconds']} s |",
        f"| Peak controller working set | {metrics['peak_memory_mb']:.1f} MB |",
        f"| Final DB size | {metrics['db_size_mb']:.1f} MB |",
        f"| Simulated finish | minute {state.get('sim_minute')} of {state.get('end_minute')} |",
        "",
        "## Chunk sizes",
        "",
        "| Kind | Chunks | Largest chunk |",
        "|---|---|---|",
        f"| history | {len(chunks.get('history', []))} | "
        f"{largest[0] if largest[1] == 'history' else 'n/a'} events |",
        f"| events | {len(chunks.get('events', []))} | "
        f"{largest[0] if largest[1] == 'events' else 'n/a'} events |",
        "",
        f"Largest chunk overall: {largest[0]} events, "
        f"{largest_bytes / 1024.0:.1f} KB ({largest[1]} chunk {largest[2]}).",
        "",
        "## Interpretation",
        "",
        metrics["interpretation"],
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "scenario_yaml",
        nargs="?",
        default="scenarios/public_03_load.yaml",
        help="scenario file passed only to sim",
    )
    parser.add_argument(
        "--work-root",
        default=str(ROOT / "runs"),
        help="directory for simulator and controller run artifacts",
    )
    args = parser.parse_args(argv)
    scenario = Path(args.scenario_yaml)
    if not scenario.is_absolute():
        scenario = (ROOT / scenario).resolve()
    if not scenario.exists():
        raise SystemExit(f"scenario not found: {scenario}")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    work_root = Path(args.work_root)
    if not work_root.is_absolute():
        work_root = (ROOT / work_root).resolve()
    work_root.mkdir(parents=True, exist_ok=True)
    root = work_root / f"load_{stamp}"
    root.mkdir(parents=True, exist_ok=False)
    sim_dir = root / "sim"
    sim_dir.mkdir()
    db_path = root / "controller.db"
    log_path = root / "controller.log"
    reports_dir = ROOT / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    python = sys.executable or "python"
    port = _free_port()
    url = f"http://127.0.0.1:{port}"

    print(f"[scenario] {scenario}")
    print(f"[runs] {root}")
    _warn_existing_simulators()
    sim_log = (root / "sim.log").open("w", encoding="utf-8")
    sim_process = subprocess.Popen(
        [
            python,
            "-m",
            "sim",
            "serve",
            "--scenario",
            str(scenario),
            "--run-dir",
            str(sim_dir),
            "--port",
            str(port),
        ],
        cwd=str(ROOT),
        env=_env(),
        stdout=sim_log,
        stderr=subprocess.STDOUT,
    )
    try:
        startup_seconds = _wait_for_health(
            sim_process, url, root / "sim.log"
        )
        print("[controller] running")
        code, wall_seconds, peak_mb = _run_controller(
            python, url, db_path, log_path
        )
        if code != 0:
            raise SystemExit(f"controller failed: {code}")
        client = SimClient(url, timeout=60, retries=3)
        state = client.get_state()
        chunks = {
            kind: client.list_chunks(kind=kind)
            for kind in ("history", "events")
        }
    finally:
        if sim_process.poll() is None:
            sim_process.terminate()
            try:
                sim_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                sim_process.kill()
                sim_process.wait(timeout=10)
        sim_log.close()

    parsed = _parse_log(log_path)
    total_events = sum(int(row["events"]) for row in parsed["ingest"])
    tick_durations = list(parsed["tick_durations"])
    tick_rates = list(parsed["tick_rates"])
    metrics = {
        "total_events": total_events,
        "wall_seconds": wall_seconds,
        "overall_rate": total_events / wall_seconds if wall_seconds > 0 else 0.0,
        "mean_tick_rate": sum(tick_rates) / len(tick_rates) if tick_rates else 0.0,
        "peak_tick_rate": max(tick_rates) if tick_rates else 0.0,
        "mean_tick_seconds": (
            sum(tick_durations) / len(tick_durations) if tick_durations else 0.0
        ),
        "slowest_tick_seconds": max(tick_durations) if tick_durations else 0.0,
        "max_lag_seconds": parsed["max_lag_seconds"],
        "final_lag_seconds": parsed["final_lag_seconds"],
        "peak_memory_mb": peak_mb or 0.0,
        "db_size_mb": _db_size(db_path) / (1024.0 * 1024.0),
    }
    lag = metrics["max_lag_seconds"]
    slow = metrics["slowest_tick_seconds"]
    if lag is not None and lag > 3600:
        metrics["interpretation"] = (
            "The controller fell behind by more than one simulated hour at the worst tick; "
            "profile before tuning."
        )
    elif slow > 2.0:
        metrics["interpretation"] = (
            "No sustained lag was observed, but at least one tick took over two seconds. "
            "Treat that tick as the first profiling target."
        )
    else:
        metrics["interpretation"] = (
            "The controller kept up: ingest lag stayed bounded and no tick exceeded two seconds."
        )

    report_path = reports_dir / "public_03_load.md"
    report = _write_report(scenario, root, metrics, state, chunks)
    report_path.write_text(report, encoding="utf-8")
    print(
        json.dumps(
            {
                **{key: value for key, value in metrics.items() if key != "interpretation"},
                "finished": state.get("finished"),
                "sim_minute": state.get("sim_minute"),
                "end_minute": state.get("end_minute"),
                "tick_count": len(tick_durations),
                "chunks": {kind: len(items) for kind, items in chunks.items()},
            },
            indent=2,
            sort_keys=True,
        )
    )
    print(f"[report] {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
