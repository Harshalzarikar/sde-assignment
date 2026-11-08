"""Controller entry point: ingest, advance, decide, and report on simulated time."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import os
import sys
import time
from typing import Any

from .client import ScenarioFinished, SimClient
from .ingest import ingest_new_chunks
from .store import Store, open_store


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts))
    except ValueError:
        return None


def _chunk_range(chunks: list[dict[str, Any]]) -> tuple[int, int] | None:
    firsts = [int(c["first_event_id"]) for c in chunks if c.get("first_event_id") is not None]
    lasts = [int(c["last_event_id"]) for c in chunks if c.get("last_event_id") is not None]
    if not firsts or not lasts:
        return None
    return min(firsts), max(lasts)


def _report_event_ranges(client: SimClient, store: Store) -> tuple[int, int] | None:
    history = _chunk_range(client.list_chunks("history"))
    live = _chunk_range(client.list_chunks("events"))
    print(f"[event-ids] history={history} live={live}", flush=True)
    if history and live and history[1] >= live[0]:
        store.write_audit(
            None,
            None,
            "event_id_overlap",
            "history and live event_id ranges overlap; composite key required",
            {"history": history, "live": live},
        )
        raise RuntimeError(
            f"history/live event_id ranges overlap: history={history} live={live}"
        )
    return live


def _lag_text(sim_ts: str | None, last_event_ts: str | None) -> str:
    now = _parse_ts(sim_ts)
    last = _parse_ts(last_event_ts)
    if now is None or last is None:
        return "n/a"
    return f"{(now - last).total_seconds():.0f}s"


def _timed_ingest(
    client: SimClient, store: Store, label: str
) -> tuple[dict[str, int], float]:
    started = time.perf_counter()
    ingested = ingest_new_chunks(client, store)
    elapsed = time.perf_counter() - started
    return ingested, elapsed


def _log_ingest(
    label: str,
    ingested: dict[str, int],
    elapsed: float,
    state: dict[str, Any],
    store: Store,
) -> None:
    count = sum(ingested.values())
    rate = count / elapsed if elapsed > 0 else 0.0
    by_kind = ",".join(f"{kind}:{n}" for kind, n in sorted(ingested.items())) or "none"
    print(
        f"[ingest:{label}] sim={state.get('ts')} chunks={by_kind} events={count} "
        f"events_per_sec={rate:.0f} lag={_lag_text(state.get('ts'), store.last_event_ts())}",
        flush=True,
    )


def decide(
    client: SimClient, store: Store, workflows: list[dict[str, Any]], sim_ts: str
) -> None:
    """Decision hook; detection and governance are added in later tasks."""
    return None


def _hourly_summary(
    store: Store, workflows: list[dict[str, Any]], sim_ts: str | None
) -> None:
    now = _parse_ts(sim_ts)
    if now is None:
        return
    hour_bucket = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H")
    for workflow in workflows:
        wid = workflow["workflow_id"]
        totals = store.hour_totals(wid, hour_bucket)
        dials = totals["dials"]
        connected = totals["connected"]
        engaged = totals["engaged"]
        pickup = connected / dials if dials else 0.0
        engagement = engaged / connected if connected else 0.0
        print(
            f"[summary] {hour_bucket} {wid} dials={dials} "
            f"pickup={pickup:.3f} engagement={engagement:.3f}",
            flush=True,
        )


def run(
    client: SimClient,
    store: Store,
    tick: int = 15,
    stop_after_minutes: int | None = None,
) -> None:
    state = client.get_state()
    workflows = client.get_workflows()
    live_range = _report_event_ranges(client, store)
    live_range_seen = live_range is not None

    ingested, elapsed = _timed_ingest(client, store, "startup")
    _log_ingest("startup", ingested, elapsed, state, store)

    while True:
        state = client.get_state()
        if state.get("finished"):
            print(f"[done] scenario finished at {state.get('ts')}", flush=True)
            break

        try:
            result = client.advance(tick)
        except ScenarioFinished:
            print("[done] scenario finished (409)", flush=True)
            break

        ingested, elapsed = _timed_ingest(client, store, "tick")
        state = client.get_state()
        _log_ingest("tick", ingested, elapsed, state, store)

        if not live_range_seen and store.get_cursor("events") is not None:
            _report_event_ranges(client, store)
            live_range_seen = True

        decide(client, store, workflows, state.get("ts", ""))

        if int(state.get("sim_minute", 0)) % 60 == 0:
            _hourly_summary(store, workflows, state.get("ts"))

        if result.get("finished") or state.get("finished"):
            print(f"[done] scenario finished at {state.get('ts')}", flush=True)
            break
        if stop_after_minutes is not None and int(state.get("sim_minute", 0)) >= stop_after_minutes:
            print(f"[done] stopping after {stop_after_minutes} simulated minutes", flush=True)
            break


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the outcome-loop controller")
    parser.add_argument(
        "sim_url",
        nargs="?",
        default=os.environ.get("SIM_URL", "http://127.0.0.1:8700"),
    )
    parser.add_argument("--db", default=None, help="SQLite path (default controller.db)")
    parser.add_argument("--tick", type=int, default=15, help="simulated minutes per advance")
    parser.add_argument(
        "--stop-after-minutes",
        type=int,
        default=None,
        help="stop early at this simulated minute (testing only)",
    )
    args = parser.parse_args(argv)

    client = SimClient(args.sim_url, timeout=60, retries=3)
    store: Store | None = None
    try:
        state = client.get_state()
        store = open_store(state, args.db)
        run(client, store, tick=args.tick, stop_after_minutes=args.stop_after_minutes)
    finally:
        if store is not None:
            store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
