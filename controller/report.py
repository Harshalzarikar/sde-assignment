"""Prints what the notebook knows: KPIs per day, caller-ID shares, proposals, and the latest decisions."""
from collections import defaultdict

from .store import Store


def print_health(db_path: str):
    """The loop's own health per simulated day: where wall time goes, decisions, credits, memory, alerts."""
    store = Store(db_path)
    print(f"{'day':<5} {'events':>10} {'wall s':>8} {'sim s':>7} {'ingest s':>9} {'decide s':>9} {'events/s':>9} "
          f"{'worst tick s':>12} {'decisions':>10} {'changes':>8} {'credits':>8} {'peak MB':>8}")
    for day, events, sim_s, ing_s, dec_s, worst, credits, rss in store.db.execute(
            "SELECT sim_minute / 1440, SUM(events), SUM(advance_s), SUM(ingest_s), SUM(decide_s), "
            "MAX(ingest_s + decide_s), MIN(credits), MAX(rss_mb) FROM loop_metrics "
            "WHERE sim_minute > 0 GROUP BY (sim_minute - 1) / 1440 ORDER BY 1").fetchall():
        n_dec, n_chg = store.db.execute(
            "SELECT COUNT(*), SUM(kind = 'applied') FROM decisions WHERE workflow_id IS NOT NULL "
            "AND substr(ts, 1, 10) = ?", (_day_ts(store, day),)).fetchone()
        total = sim_s + ing_s + dec_s
        print(f"D{day:<4} {events:>10,} {total:>8.1f} {sim_s:>7.1f} {ing_s:>9.1f} {dec_s:>9.1f} "
              f"{events / max(ing_s, 1e-9):>9,.0f} {worst:>12.2f} {n_dec:>10} {n_chg or 0:>8} "
              f"{credits if credits is not None else '-':>8} {rss:>8.0f}")
    alerts = store.db.execute("SELECT ts, workflow_id, summary FROM decisions WHERE kind = 'alert' ORDER BY id").fetchall()
    print(f"\nalerts: {len(alerts)}")
    for ts, wid, summary in alerts:
        print(f"  {ts[:16]}  {wid or '-':<12} {summary[:120]}")


def _day_ts(store: Store, day: int) -> str:
    import datetime as dt
    return (dt.date.fromisoformat(store.meta("d0")) + dt.timedelta(days=day)).isoformat()


def print_status(db_path: str, decisions: int = 25):
    store = Store(db_path)
    print(f"scenario: {store.meta('scenario')} | last chunk read: {store.last_chunk()} | "
          f"sim minute: {store.meta('last_sim_minute', 0)}")
    rows = store.db.execute(
        "SELECT workflow_id, CASE WHEN day < 0 THEN 'history' ELSE 'D' || day END, MIN(day), "
        "SUM(dials), SUM(connected), SUM(engaged), SUM(halluc_flags) FROM call_counts "
        "GROUP BY workflow_id, CASE WHEN day < 0 THEN -1 ELSE day END ORDER BY workflow_id, MIN(day)").fetchall()
    print(f"\n{'workflow':<14} {'day':<8} {'dials':>9} {'pickup':>8} {'engagement':>11} {'halluc flags':>13}")
    for wid, label, _, dials, conn, eng, flags in rows:
        print(f"{wid:<14} {label:<8} {dials:>9,} {conn / max(dials, 1):>8.1%} {eng / max(conn, 1):>11.1%} "
              f"{flags / max(conn, 1):>13.2%}")

    print("\ncaller IDs per day (share of dials, pickup):")
    by = defaultdict(dict)
    for wid, day, cid, dials, conn in store.db.execute(
            "SELECT workflow_id, day, caller_id, SUM(dials), SUM(connected) FROM call_counts WHERE day >= 0 "
            "GROUP BY workflow_id, day, caller_id ORDER BY workflow_id, day, caller_id"):
        by[(wid, day)][cid] = (dials, conn)
    for (wid, day), cids in by.items():
        total = sum(d for d, _ in cids.values())
        cells = "  ".join(f"{c} {d / total:>4.0%} ({n / d:.1%})" for c, (d, n) in cids.items())
        print(f"  {wid:<12} D{day:<3} {cells}")

    print("\nscript safety (script changes, validations, LLM calls, incidents):")
    for ts, wid, kind, summary in store.db.execute(
            "SELECT ts, workflow_id, kind, summary FROM decisions WHERE kind LIKE 'incident%' OR kind LIKE 'prompt_%' "
            "OR kind IN ('validation_run', 'llm_suggestions') ORDER BY id").fetchall():
        print(f"  {ts[:16]}  {wid or '-':<12} {kind:<20} {summary[:120]}")

    print("\nexperiments (welcome message / voice decisions):")
    for ts, wid, kind, summary in store.db.execute(
            "SELECT ts, workflow_id, kind, summary FROM decisions WHERE kind IN ('arm_share', 'arm_removed', "
            "'winner_promoted', 'winner_live', 'winner_cancelled', 'winner_rolled_back', 'experiment_paused', 'group_trial', 'group_rule_added', "
            "'group_rule_live', 'group_rule_removed', 'groups_stood_down') ORDER BY id").fetchall():
        print(f"  {ts[:16]}  {wid or '-':<12} {kind:<18} {summary[:130]}")
    for col in ("welcome", "voice"):
        print(f"\n{col} per day (share of answered calls, engagement):")
        by = defaultdict(dict)
        for wid, day, opt, conn, eng in store.db.execute(
                f"SELECT workflow_id, day, {col}, SUM(connected), SUM(engaged) FROM call_counts WHERE day >= 0 "
                f"GROUP BY workflow_id, day, {col} ORDER BY workflow_id, day, {col}"):
            by[(wid, day)][opt] = (conn, eng)
        for (wid, day), opts in by.items():
            total = sum(c for c, _ in opts.values())
            cells = "  ".join(f"{o} {c / total:>4.0%} ({e / max(c, 1):.1%})" for o, (c, e) in opts.items())
            print(f"  {wid:<12} D{day:<3} {cells}")

    print("\nproposals:")
    for p in store.proposals():
        print(f"  {p['workflow_id']:<12} {p['path']:<8} {p['status']:<9} {p['approval_id'] or '':<10} "
              f"v{p['base_version']}->{p['applied_version'] or '?':<3} {p['body'].get('change_summary', '')[:90]}")

    print(f"\nlatest {decisions} decisions:")
    for ts, wid, kind, summary in store.db.execute(
            "SELECT ts, workflow_id, kind, summary FROM decisions ORDER BY id DESC LIMIT ?",
            (decisions,)).fetchall()[::-1]:
        print(f"  {ts[:16]}  {wid or '-':<12} {kind:<20} {summary[:110]}")
