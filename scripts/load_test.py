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
    r"seconds=(?P<seconds>[0-9.]+)\s+"
    r"events_per_sec=(?P<rate>[0-9.]+)\s+lag=(?P<lag>\S+)"
)
TICK_RE = re.compile(
    r"\[tick\].*?events=(?P<events>\d+)\s+duration=(?P<duration>[0-9.]+)\s+"
    r"state=(?P<state>[0-9.]+)\s+advance=(?P<advance>[0-9.]+)\s+"
    r"ingest=(?P<ingest>[0-9.]+)\s+supervisor=(?P<supervisor>[0-9.]+)\s+"
    r"detect=(?P<detect>[0-9.]+)\s+governance=(?P<governance>[0-9.]+)"
)


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


def _prevent_sleep() -> int | None:
    if os.name != "nt":
        return None
    es_continuous = 0x80000000
    es_system_required = 0x00000001
    es_display_required = 0x00000002
    try:
        previous = ctypes.windll.kernel32.SetThreadExecutionState(
            es_continuous | es_system_required | es_display_required
        )
    except OSError:
        return None
    print("[power] keeping the machine awake for this run", flush=True)
    return int(previous) if previous is not None else None


def _allow_sleep(previous: int | None) -> None:
    if os.name != "nt":
        return
    es_continuous = 0x80000000
    ctypes.windll.kernel32.SetThreadExecutionState(
        previous if previous is not None else es_continuous
    )


def _env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("CONTROLLER_CRASH_AT", None)
    return env


if os.name == "nt":
    import ctypes.wintypes

    _kernel32 = ctypes.windll.kernel32
    _psapi = ctypes.windll.psapi
    _kernel32.OpenProcess.argtypes = [
        ctypes.wintypes.DWORD,
        ctypes.wintypes.BOOL,
        ctypes.wintypes.DWORD,
    ]
    _kernel32.OpenProcess.restype = ctypes.wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
    _kernel32.CloseHandle.restype = ctypes.wintypes.BOOL
    _kernel32.CreateToolhelp32Snapshot.argtypes = [
        ctypes.wintypes.DWORD,
        ctypes.wintypes.DWORD,
    ]
    _kernel32.CreateToolhelp32Snapshot.restype = ctypes.wintypes.HANDLE

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

    class _ProcessEntry32(ctypes.Structure):
        _fields_ = [
            ("dwSize", ctypes.wintypes.DWORD),
            ("cntUsage", ctypes.wintypes.DWORD),
            ("th32ProcessID", ctypes.wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", ctypes.wintypes.DWORD),
            ("cntThreads", ctypes.wintypes.DWORD),
            ("th32ParentProcessID", ctypes.wintypes.DWORD),
            ("pcPriClassBase", ctypes.wintypes.LONG),
            ("dwFlags", ctypes.wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    _psapi.GetProcessMemoryInfo.argtypes = [
        ctypes.wintypes.HANDLE,
        ctypes.POINTER(_ProcessMemoryCounters),
        ctypes.wintypes.DWORD,
    ]
    _psapi.GetProcessMemoryInfo.restype = ctypes.wintypes.BOOL
else:
    _ProcessMemoryCounters = None


def _rss_mb(pid: int) -> float | None:
    if os.name == "nt":
        process_query_information = 0x0400
        process_vm_read = 0x0010
        handle = _kernel32.OpenProcess(
            process_query_information | process_vm_read, False, int(pid)
        )
        if not handle:
            return None
        try:
            counters = _ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(_ProcessMemoryCounters)
            ok = _psapi.GetProcessMemoryInfo(
                handle, ctypes.byref(counters), counters.cb
            )
            if not ok:
                return None
            return float(counters.WorkingSetSize) / (1024.0 * 1024.0)
        finally:
            _kernel32.CloseHandle(handle)
    status = Path(f"/proc/{int(pid)}/status")
    if not status.exists():
        return None
    for line in status.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("VmRSS:"):
            return float(line.split()[1]) / 1024.0
    return None


def _process_tree(pid: int) -> list[int]:
    if os.name != "nt":
        return [int(pid)]
    snapshot = _kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if snapshot == ctypes.wintypes.HANDLE(-1).value:
        return [int(pid)]
    entries: list[tuple[int, int]] = []
    try:
        entry = _ProcessEntry32()
        entry.dwSize = ctypes.sizeof(_ProcessEntry32)
        ok = _kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            entries.append((int(entry.th32ProcessID), int(entry.th32ParentProcessID)))
            ok = _kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        _kernel32.CloseHandle(snapshot)
    children: dict[int, list[int]] = {}
    for child, parent in entries:
        children.setdefault(parent, []).append(child)
    found: list[int] = []
    pending = [int(pid)]
    while pending:
        current = pending.pop()
        found.append(current)
        pending.extend(children.get(current, []))
    return found


def _tree_peak_mb(pid: int) -> float | None:
    values = [
        value
        for value in (_rss_mb(item) for item in _process_tree(pid))
        if value is not None
    ]
    return max(values) if values else None


def _run_controller(
    python: str,
    url: str,
    db_path: Path,
    log_path: Path,
    stop_after_minutes: int | None = None,
) -> tuple[int, float, float | None]:
    command = [python, "-m", "controller.main", url, "--db", str(db_path)]
    if stop_after_minutes is not None:
        command.extend(["--stop-after-minutes", str(stop_after_minutes)])
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
            value = _tree_peak_mb(process.pid)
            if value is not None:
                peak_mb = max(peak_mb, value)
            time.sleep(0.1)
        process.wait()
        elapsed = time.perf_counter() - started
    return int(process.returncode), elapsed, peak_mb or None


def _lag_text(seconds: object) -> str:
    return "n/a" if seconds is None else f"{float(seconds):.1f} s"


def _mean_phase_ms(rows: list[dict[str, object]], key: str) -> float:
    if not rows:
        return 0.0
    return sum(float(row[key]) for row in rows) / len(rows) * 1000.0


def _parse_log(path: Path) -> dict[str, object]:
    ingest = []
    tick_durations = []
    tick_profiles = []
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
                    "seconds": float(match.group("seconds")),
                    "rate": float(match.group("rate")),
                    "lag_seconds": lag_seconds,
                }
            )
        match = TICK_RE.search(line)
        if match:
            tick_durations.append(float(match.group("duration")))
            tick_profiles.append(
                {
                    "events": int(match.group("events")),
                    "duration": float(match.group("duration")),
                    "state": float(match.group("state")),
                    "advance": float(match.group("advance")),
                    "ingest": float(match.group("ingest")),
                    "supervisor": float(match.group("supervisor")),
                    "detect": float(match.group("detect")),
                    "governance": float(match.group("governance")),
                }
            )
    tick_rows = [row for row in ingest if row["label"] == "tick"]
    event_tick_rows = [row for row in tick_rows if int(row["events"]) > 0]
    lag_values = [
        float(row["lag_seconds"])
        for row in event_tick_rows
        if row["lag_seconds"] is not None
    ]
    return {
        "ingest": ingest,
        "tick_events": sum(int(row["events"]) for row in tick_rows),
        "event_tick_count": len(event_tick_rows),
        "idle_tick_count": len(tick_rows) - len(event_tick_rows),
        "tick_rates": [float(row["rate"]) for row in tick_rows],
        "event_tick_rates": [float(row["rate"]) for row in event_tick_rows],
        "tick_durations": tick_durations,
        "tick_profiles": tick_profiles,
        "max_lag_seconds": max(lag_values) if lag_values else None,
        "final_lag_seconds": lag_values[-1] if lag_values else None,
    }


def _file_size(path: Path) -> int:
    return path.stat().st_size if path.exists() else 0


def _db_size(path: Path) -> int:
    return sum(
        Path(f"{path}{suffix}").stat().st_size
        for suffix in ("", "-wal", "-shm")
        if Path(f"{path}{suffix}").exists()
    )


def _mb_text(value: object) -> str:
    return "not measured" if value is None else f"{float(value):.1f} MB"


def _write_report(
    scenario: Path,
    root: Path,
    metrics: dict[str, object],
    state: dict[str, object],
    chunks: dict[str, list[dict[str, object]]],
    stop_after_minutes: int | None = None,
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
    phase_all = metrics.get("phase_all_ms", {})
    phase_idle = metrics.get("phase_idle_ms", {})
    controller_keys = ("state", "ingest", "supervisor", "detect", "governance")
    controller_all = sum(float(phase_all.get(key, 0.0)) for key in controller_keys)
    controller_idle = sum(float(phase_idle.get(key, 0.0)) for key in controller_keys)
    other_all = float(phase_all.get("duration", 0.0)) - (
        controller_all + float(phase_all.get("advance", 0.0))
    )
    other_idle = float(phase_idle.get("duration", 0.0)) - (
        controller_idle + float(phase_idle.get("advance", 0.0))
    )
    stop_arg = (
        f" --stop-after-minutes {stop_after_minutes}"
        if stop_after_minutes is not None
        else ""
    )
    stop_note = (
        f"Stopped early after {stop_after_minutes} simulated minutes."
        if stop_after_minutes is not None
        else "No early stop."
    )
    if stop_after_minutes == 1440:
        title = f"# Load report — {scenario.stem}: 1 simulated day of 5"
    elif stop_after_minutes is not None:
        title = (
            f"# Load report — {scenario.stem}: "
            f"{stop_after_minutes} simulated minutes of 5 days"
        )
    else:
        title = f"# Load report — {scenario.stem}"
    mean_live = metrics["mean_live_rate"]
    peak_live = metrics["peak_live_rate"]
    live_rate_value = (
        "n/a (no live-event ticks)"
        if mean_live is None
        else f"mean {float(mean_live):.0f} events/s; peak {float(peak_live):.0f} events/s"
    )
    history_share = (
        float(metrics["history_preload_seconds"])
        / float(metrics["controller_wall_seconds"])
        * 100.0
        if float(metrics["controller_wall_seconds"]) > 0
        else 0.0
    )
    finish_text = (
        "Run finished the full scenario."
        if state.get("finished")
        else (
            "Run did not finish the 5-day scenario"
            + (
                f"; it stopped at simulated minute {state.get('sim_minute')} "
                f"by --stop-after-minutes {stop_after_minutes}."
                if stop_after_minutes is not None
                else "."
            )
        )
    )
    lines = [
        title,
        "",
        "## Exact commands",
        "",
        "```",
        f"python scripts/load_test.py {scenario} --work-root {root.parent}{stop_arg}",
        f"python -m sim serve --scenario {scenario} --run-dir {root / 'sim'} --port <free-port>",
        f"python -m controller.main http://127.0.0.1:<port> --db {root / 'controller.db'}{stop_arg}",
        "```",
        "",
        f"No baseline run. {stop_note} One fresh simulator and one fresh controller DB.",
        "",
        "## Run shape",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Simulator startup (health) | {float(metrics['simulator_startup_seconds']):.1f} s |",
        f"| Controller wall time, including preload | {float(metrics['controller_wall_seconds']):.1f} s |",
        f"| History preload events | {metrics['history_preload_events']} |",
        f"| History preload wall time | {float(metrics['history_preload_seconds']):.1f} s |",
        f"| History preload rate | {float(metrics['history_preload_rate']):.0f} events/s |",
        f"| History preload share of controller wall | {history_share:.1f}% |",
        f"| Steady-state ticks | {metrics['tick_count']} |",
        f"| Steady-state wall for ticks | {float(metrics['steady_state_seconds']):.1f} s "
        f"({float(metrics['steady_state_minutes']):.2f} min) |",
        f"| 10-minute steady-state target | "
        f"{'met' if metrics['target_10_min_met'] else 'not met'} |",
        f"| Live events ingested | {metrics['live_events']} |",
        f"| Live-event ticks | {metrics['event_tick_count']} |",
        f"| Idle ticks | {metrics['idle_tick_count']} |",
        f"| Live-event ingest rate | {live_rate_value} |",
        f"| Mean tick duration | {float(metrics['mean_tick_seconds']) * 1000.0:.2f} ms |",
        f"| Slowest tick duration | {float(metrics['slowest_tick_seconds']) * 1000.0:.2f} ms |",
        f"| Max ingest lag (live-event ticks) | {_lag_text(metrics['max_lag_seconds'])} |",
        f"| Final ingest lag (live-event ticks) | {_lag_text(metrics['final_lag_seconds'])} |",
        f"| Peak controller working set | {_mb_text(metrics['peak_memory_mb'])} |",
        f"| Final DB file | {float(metrics['db_main_mb']):.1f} MB |",
        f"| Final WAL | {float(metrics['wal_mb']):.1f} MB |",
        f"| Final SHM | {float(metrics['shm_mb']):.1f} MB |",
        f"| Final DB + WAL + SHM | {float(metrics['db_total_mb']):.1f} MB |",
        f"| Simulated finish | minute {state.get('sim_minute')} of {state.get('end_minute')} |",
        "",
        "## Tick phase breakdown",
        "",
        f"Profiled ticks: {metrics['profile_tick_count']} total, "
        f"{metrics['profile_idle_count']} idle (no events ingested).",
        "",
        "| Phase | All ticks mean | Idle ticks mean |",
        "|---|---|---|",
        f"| POST /sim/advance | {phase_all.get('advance', 0.0):.2f} ms | "
        f"{phase_idle.get('advance', 0.0):.2f} ms |",
        f"| Ingest | {phase_all.get('ingest', 0.0):.2f} ms | "
        f"{phase_idle.get('ingest', 0.0):.2f} ms |",
        f"| Detect | {phase_all.get('detect', 0.0):.2f} ms | "
        f"{phase_idle.get('detect', 0.0):.2f} ms |",
        f"| Governance | {phase_all.get('governance', 0.0):.2f} ms | "
        f"{phase_idle.get('governance', 0.0):.2f} ms |",
        f"| Supervisor edits | {phase_all.get('supervisor', 0.0):.2f} ms | "
        f"{phase_idle.get('supervisor', 0.0):.2f} ms |",
        f"| State reads | {phase_all.get('state', 0.0):.2f} ms | "
        f"{phase_idle.get('state', 0.0):.2f} ms |",
        f"| Other controller work | {other_all:.2f} ms | {other_idle:.2f} ms |",
        f"| Full tick | {phase_all.get('duration', 0.0):.2f} ms | "
        f"{phase_idle.get('duration', 0.0):.2f} ms |",
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
        "## Measured analysis",
        "",
        finish_text,
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
    parser.add_argument(
        "--stop-after-minutes",
        type=int,
        default=None,
        help="stop the controller at this simulated minute (testing only)",
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
    sleep_previous = _prevent_sleep()
    try:
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
                python,
                url,
                db_path,
                log_path,
                stop_after_minutes=args.stop_after_minutes,
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
    finally:
        _allow_sleep(sleep_previous)

    parsed = _parse_log(log_path)
    startup_rows = [
        row for row in parsed["ingest"] if row["label"] == "startup"
    ]
    startup = startup_rows[0] if startup_rows else {}
    tick_rows = [row for row in parsed["ingest"] if row["label"] == "tick"]
    history_events = int(startup.get("events", 0))
    history_seconds = float(startup.get("seconds", 0.0))
    history_rate = history_events / history_seconds if history_seconds > 0 else 0.0
    live_events = sum(int(row["events"]) for row in tick_rows)
    live_rates = [float(value) for value in parsed["event_tick_rates"]]
    tick_durations = list(parsed["tick_durations"])
    tick_profiles = list(parsed["tick_profiles"])
    idle_profiles = [row for row in tick_profiles if int(row["events"]) == 0]
    steady_state_seconds = sum(float(row["duration"]) for row in tick_profiles)
    phase_keys = (
        "duration",
        "state",
        "advance",
        "ingest",
        "supervisor",
        "detect",
        "governance",
    )
    phase_all = {key: _mean_phase_ms(tick_profiles, key) for key in phase_keys}
    phase_idle = {key: _mean_phase_ms(idle_profiles, key) for key in phase_keys}
    metrics = {
        "simulator_startup_seconds": startup_seconds,
        "controller_wall_seconds": wall_seconds,
        "history_preload_events": history_events,
        "history_preload_seconds": history_seconds,
        "history_preload_rate": history_rate,
        "live_events": live_events,
        "tick_count": len(tick_profiles),
        "event_tick_count": parsed["event_tick_count"],
        "idle_tick_count": parsed["idle_tick_count"],
        "steady_state_seconds": steady_state_seconds,
        "steady_state_minutes": steady_state_seconds / 60.0,
        "mean_tick_seconds": (
            sum(tick_durations) / len(tick_durations) if tick_durations else 0.0
        ),
        "slowest_tick_seconds": max(tick_durations) if tick_durations else 0.0,
        "mean_live_rate": sum(live_rates) / len(live_rates) if live_rates else None,
        "peak_live_rate": max(live_rates) if live_rates else None,
        "max_lag_seconds": parsed["max_lag_seconds"],
        "final_lag_seconds": parsed["final_lag_seconds"],
        "peak_memory_mb": peak_mb,
        "db_main_mb": _file_size(db_path) / (1024.0 * 1024.0),
        "wal_mb": _file_size(Path(f"{db_path}-wal")) / (1024.0 * 1024.0),
        "shm_mb": _file_size(Path(f"{db_path}-shm")) / (1024.0 * 1024.0),
        "db_total_mb": _db_size(db_path) / (1024.0 * 1024.0),
        "profile_tick_count": len(tick_profiles),
        "profile_idle_count": len(idle_profiles),
        "phase_all_ms": phase_all,
        "phase_idle_ms": phase_idle,
        "target_10_min_met": steady_state_seconds / 60.0 <= 10.0,
    }
    phase_labels = {
        "advance": "POST /sim/advance",
        "ingest": "ingest",
        "supervisor": "supervisor edits",
        "detect": "detect",
        "governance": "governance",
        "state": "state reads",
    }
    dominant_key = max(phase_labels, key=lambda key: phase_all.get(key, 0.0))
    dominant_label = phase_labels[dominant_key]
    dominant_ms = phase_all.get(dominant_key, 0.0)
    advance_ms = phase_all.get("advance", 0.0)
    controller_ms = sum(
        phase_all.get(key, 0.0)
        for key in ("state", "ingest", "supervisor", "detect", "governance")
    )
    changes = {
        "advance": (
            "the simulator advance dominates; a controller-side change would not "
            "address the measured cost"
        ),
        "ingest": (
            "keep 10k-row chunk transactions and, if chunk fetch memory is still "
            "high, stream the JSON chunk instead of materializing it"
        ),
        "supervisor": (
            "track the last processed config_changed event_id and keep an "
            "events(type, event_id) index instead of rescanning every tick"
        ),
        "detect": (
            "bound detector work to the current window instead of repeatedly "
            "scanning stored history"
        ),
        "governance": "keep proposal reconciliation bounded to non-terminal rows",
        "state": "cache the state/workflow reads used within one tick",
    }
    live_rate_text = (
        "no live-event ticks"
        if metrics["mean_live_rate"] is None
        else (
            f"mean {metrics['mean_live_rate']:.0f} events/s and peak "
            f"{metrics['peak_live_rate']:.0f} events/s"
        )
    )
    ten_x = (
        "No live-event ticks were observed, so a 10x live-event claim is not "
        "supported by this run."
        if metrics["mean_live_rate"] is None
        else (
            f"At 10x the measured live-event rate ({live_rate_text}), linear "
            f"extrapolation puts the first pressure on {dominant_label}: "
            f"{dominant_ms:.1f} ms/tick becomes roughly {dominant_ms * 10:.0f} ms/tick. "
            "This is an extrapolation, not a measured 10x run."
        )
    )
    metrics["interpretation"] = (
        f"Measured dominant phase is {dominant_label} at {dominant_ms:.1f} ms/tick "
        f"(controller phases excluding advance total {controller_ms:.1f} ms/tick; "
        f"POST /sim/advance is {advance_ms:.1f} ms/tick). "
        f"History preload was {history_events} events in {history_seconds:.1f}s "
        f"({history_rate:.0f} events/s), separate from steady state. "
        f"{ten_x} I would change: {changes.get(dominant_key, 'profile the largest phase before changing it')}. "
        f"Steady-state wall time was {metrics['steady_state_minutes']:.1f} min for "
        f"{metrics['tick_count']} ticks against the 10-minute target "
        f"({'met' if metrics['target_10_min_met'] else 'not met'})."
    )

    report_path = reports_dir / "public_03_load.md"
    report = _write_report(
        scenario,
        root,
        metrics,
        state,
        chunks,
        stop_after_minutes=args.stop_after_minutes,
    )
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
