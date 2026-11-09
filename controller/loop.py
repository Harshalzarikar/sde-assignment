"""The main loop: move simulated time forward, read what happened, decide what to change."""
import datetime as dt
import os
import sys
import time

try:
    import resource
except ImportError:  # Windows has no resource module; RSS is reported as 0.0 there
    resource = None

from .api import Api
from .callerid import CallerIdManager
from .experiments import ExperimentManager
from .governance import Governance
from .guardrail import GuardrailWatch
from .ingest import Ingestor
from .prompts import PromptManager
from .segments import SegmentManager
from .store import Store

STEP_MINUTES = 15
CRASH_AT_ENV = "CONTROLLER_CRASH_AT_MINUTE"  # test-only: hard-exit once simulated time reaches this minute
STOP_AT_ENV = "CONTROLLER_STOP_AT_MINUTE"  # test-only: stop cleanly there (load measurements)


def peak_rss_mb() -> float:
    if resource is None:
        return 0.0
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r / (1024 * 1024) if sys.platform == "darwin" else r / 1024


def open_store(db_path: str, state: dict) -> Store:
    store = Store(db_path)
    if store.matches_world(state):
        return store
    store.close()
    archived = f"{db_path}.old-{int(time.time())}"
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(db_path + suffix):
            os.replace(db_path + suffix, archived + suffix)
    print(f"the notebook belonged to a different simulator run; moved it to {archived}")
    return Store(db_path)


def run(url: str, db_path: str):
    api = Api(url)
    state = api.get_ok("/sim/state")
    store = open_store(db_path, state)
    first_start = store.meta("scenario") is None
    if first_start:
        store.set_meta("scenario", state["scenario"])
    d0 = (dt.datetime.fromisoformat(state["ts"]) - dt.timedelta(minutes=state["sim_minute"])).date()
    store.set_meta("d0", d0.isoformat())
    workflows = {w["workflow_id"]: w for w in api.get_ok("/workflows")}
    store.save_workflows(list(workflows.values()))
    store.log_decision(state["ts"], None, "controller_started" if first_start else "controller_resumed",
                       f"{'Started' if first_start else 'Resumed'} at {state['ts']}; "
                       f"last chunk already in the notebook: {store.last_chunk()}")

    ingest = Ingestor(api, store, d0)
    gov = Governance(api, store, workflows)
    prompts = PromptManager(api, store, gov, workflows, d0)
    callers = CallerIdManager(store, gov, workflows, d0)
    experiments = ExperimentManager(store, gov, workflows, d0)
    groups = SegmentManager(store, gov, workflows, d0)
    guard = GuardrailWatch(store, workflows)
    crash_at = int(os.environ.get(CRASH_AT_ENV, "-1"))
    stop_at = int(os.environ.get(STOP_AT_ENV, "-1"))

    def decide(now: int, ts: str):
        gov.tick(now, ts)
        for wid in workflows:
            prompts.plan(wid, now, ts)
            gov.submit(wid, now, ts, [callers.plan(wid, now, ts), experiments.plan(wid, now, ts),
                                      groups.plan(wid, now, ts)])
            guard.check(wid, now, ts)

    t0 = time.time()
    read = ingest.catch_up()
    gov.reconcile(state["ts"])
    print(f"[{state['ts'][:16]}] {'started' if first_start else 'resumed'}: caught up on {read:,} events "
          f"in {time.time() - t0:.1f}s")
    if not state["finished"]:
        decide(state["sim_minute"], state["ts"])

    day = state["sim_minute"] // 1440
    day_events, day_t0 = 0, time.time()
    while True:
        t_adv = time.time()
        status, adv = api.post("/sim/advance", {"minutes": STEP_MINUTES})
        t_ing = time.time()
        if status == 409:
            ingest.catch_up()
            break
        if status != 200:
            raise RuntimeError(f"advance failed: {status} {adv}")
        store.set_meta("last_sim_minute", adv["sim_minute"])
        if 0 <= stop_at <= adv["sim_minute"]:
            print(f"stopping at {adv['ts'][:16]} as asked ({STOP_AT_ENV})")
            return
        if 0 <= crash_at <= adv["sim_minute"]:
            print(f"*** test crash at {adv['ts'][:16]} (after advancing, before reading the new events) ***")
            os._exit(137)
        events = ingest.catch_up()
        day_events += events
        t_dec = time.time()
        if not adv["finished"]:
            decide(adv["sim_minute"], adv["ts"])
        t_end = time.time()
        credits = api.get_ok("/sim/state")["credits_remaining"] if adv["sim_minute"] % 60 == 0 else None
        store.save_metrics((adv["sim_minute"], t_end, events, len(adv.get("new_chunks", [])),
                            round(t_ing - t_adv, 4), round(t_dec - t_ing, 4), round(t_end - t_dec, 4),
                            credits, round(peak_rss_mb(), 1)))
        if adv["sim_minute"] // 1440 != day or adv["finished"]:
            secs = time.time() - day_t0
            print(f"D{day} done: {day_events:,} events in {secs:.1f}s ({day_events / max(secs, 1e-9):,.0f} events/s)")
            day, day_events, day_t0 = adv["sim_minute"] // 1440, 0, time.time()
        if adv["finished"]:
            break

    final = api.get_ok("/sim/state")
    store.log_decision(final["ts"], None, "controller_finished", "Scenario finished")
    print("scenario finished")
