"""Two read-only views over the notebook (never the simulator):

  supervisor - one screen per workflow: what is live, what is being tested and how it is doing, what we are waiting
               on a human for, what changed today, alerts. (stakeholders/04: "one screen, not fifty alerts")
  why        - "why did workflow X change at time T, who approved it, and what happened next?"
"""
import datetime as dt
import json

from .clock import label, minute_of
from .experiments import compare
from .store import Store

KINDS_NEXT = ("winner_live", "winner_rolled_back", "winner_cancelled", "arm_removed", "group_rule_live",
              "group_rule_added", "group_rule_removed", "incident_fix_live", "incident_resolved", "incident_not_fixed",
              "caller_id_flagged", "caller_id_healthy", "experiment_paused", "alert", "guardrail_watch")


def parse_when(text: str, d0: dt.date) -> int:
    """'D3 14:00' or an ISO timestamp -> simulated minute."""
    t = text.strip()
    if t.upper().startswith("D"):
        day, _, hm = t[1:].partition(" ")
        hh, _, mm = (hm or "00:00").partition(":")
        return int(day) * 1440 + int(hh) * 60 + int(mm or 0)
    return minute_of(t if "+" in t else t + "+05:30", d0)


def _kpis(store: Store, wid: str, since_hour: int, until_hour: int):
    dials, conn, eng = store.db.execute(
        "SELECT SUM(dials), SUM(connected), SUM(engaged) FROM call_counts WHERE workflow_id = ? "
        "AND day * 24 + hour >= ? AND day * 24 + hour < ?", (wid, since_hour, until_hour)).fetchone()
    if not dials:
        return "no calls"
    return f"pickup {conn / dials:.1%} of {dials:,} dials, engagement {eng / max(conn, 1):.1%} of {conn:,} answered"


def print_supervisor(db_path: str, only: str | None = None):
    store = Store(db_path)
    d0 = dt.date.fromisoformat(store.meta("d0"))
    now = store.meta("last_sim_minute", 0)
    today = (d0 + dt.timedelta(days=now // 1440)).isoformat()
    wids = [r[0] for r in store.db.execute("SELECT workflow_id FROM workflows ORDER BY workflow_id")]
    for wid in wids:
        if only and wid != only:
            continue
        info = json.loads(store.db.execute("SELECT info FROM workflows WHERE workflow_id = ?", (wid,)).fetchone()[0])
        cfg = store.meta(f"config:{wid}")
        print("=" * 100)
        print(f"{wid}  ({info['use_case']})  at {label(now)}  primary: {info['objective']['primary']}, guardrail: "
              f"{info['objective'].get('guardrail')}")
        print(f"  last 24h: {_kpis(store, wid, now // 60 - 24, now // 60)}")
        if cfg:
            c = cfg["config"]
            dflt = c["allocations"]["default"]
            print(f"  LIVE (config v{cfg['version']}): script {c['prompt_version']} | caller ID {_fmt(dflt['caller_id'])}")
            print(f"       greeting {_fmt(dflt['welcome_message'])} | voice {_fmt(dflt['voice'])}")
            for coh in c["cohorts"]:
                print(f"       group {coh['id']} {coh['where']}: {c['allocations'].get(coh['id'], {})}")
        exp = store.meta(f"exp:{wid}")
        if exp:
            print("  TESTING (each option vs the main one, answered calls, all days):")
            from .segments import rule_cohorts
            for dim, d in exp["dims"].items():
                excluded = rule_cohorts(store, wid, dim)
                rows = [r for r in store.arm_stats(wid, dim) if r[0][1] not in excluded]
                cells = []
                for opt in info["options"][dim]:
                    if opt == d["champion"]:
                        cells.append(f"{opt}=main")
                        continue
                    k = compare(rows, opt, d["champion"])
                    tag = ("control" if opt == d["control"] else
                           f"removed ({d['retired'][opt]['reason']})" if opt in d["retired"] else "testing")
                    cells.append(f"{opt} {tag}" + (f" {k['lift']:+.1%} z={k['z']}" if k else ""))
                extra = f" | promoted from {d['control']}" if d.get("promoted") else ""
                print(f"     {dim:<16} " + "; ".join(cells) + extra)
        seg = store.meta(f"seg:{wid}")
        if seg and seg["rules"]:
            for dim, r in seg["rules"].items():
                print(f"     group {r['stage']}: {dim} {r['winner']} for {r['feature']} {r['values']} "
                      f"(vs {r['control']}, found {r['evidence']['lift']:+.1%})")
        waiting = store.proposals(wid, ("sending", "pending", "approved"))
        print(f"  WAITING ON YOU: {len(waiting) or 'nothing'}")
        for p in waiting:
            print(f"     {p['approval_id'] or p['proposal_id']} ({p['status']}, impact {p['body'].get('impact_score')}): "
                  f"{p['body'].get('change_summary', '')[:110]}")
        print("  CHANGED TODAY:")
        changed = store.db.execute("SELECT body FROM world_events WHERE workflow_id = ? AND type = 'config_changed' "
                                   "AND substr(ts, 1, 10) = ? ORDER BY event_id", (wid, today)).fetchall()
        for (body,) in changed:
            ev = json.loads(body)
            p = store.proposals(wid) if ev["proposal_id"] else []
            summary = next((x["body"].get("change_summary", "") for x in p if x["proposal_id"] == ev["proposal_id"]), "")
            print(f"     {ev['ts'][11:16]} v{ev['version']} by {ev['actor']} {ev['changed_dims']} {summary[:80]}")
        if not changed:
            print("     nothing")
        alerts = store.db.execute("SELECT ts, summary FROM decisions WHERE workflow_id = ? AND kind = 'alert' "
                                  "ORDER BY id DESC LIMIT 3", (wid,)).fetchall()
        if alerts:
            print("  ALERTS:")
            for ts, s in alerts:
                print(f"     {ts[:16]} {s[:140]}")


def print_why(db_path: str, wid: str, when: str):
    store = Store(db_path)
    d0 = dt.date.fromisoformat(store.meta("d0"))
    t = parse_when(when, d0)
    events = [json.loads(b) for (b,) in store.db.execute(
        "SELECT body FROM world_events WHERE workflow_id = ? AND type = 'config_changed' ORDER BY event_id", (wid,))]
    before = [e for e in events if minute_of(e["ts"], d0) <= t + 14]
    if not before:
        print(f"{wid} had no config change at or before {label(t)}; it ran the business default.")
        return
    ev = before[-1]
    m = minute_of(ev["ts"], d0)
    print(f"{wid}: the config live at {label(t)} is v{ev['version']}, applied {label(m)} by {ev['actor']}; "
          f"it changed {ev['changed_dims']}.")
    if ev["actor"] == "supervisor":
        print("  A client supervisor made this edit directly (it bypasses governance). Our reaction:")
        for ts, kind, s in store.db.execute(
                "SELECT ts, kind, summary FROM decisions WHERE workflow_id = ? AND ts >= ? ORDER BY id LIMIT 6",
                (wid, ev["ts"][:16])):
            print(f"    {ts[:16]} {kind}: {s[:130]}")
    else:
        p = next((x for x in store.proposals(wid) if x["proposal_id"] == ev["proposal_id"]), None)
        if p:
            b = p["body"]
            print(f"  WHAT: {b.get('change_summary')}")
            print(f"  WHY:  {b.get('rationale')}")
            if b.get("diff"):
                print(f"  DIFF: {json.dumps(b['diff'])[:400]}")
            if b.get("dry_run"):
                print(f"  DRY RUN: {json.dumps(b['dry_run'])[:400]}")
            if b.get("impact_score") is not None:
                print(f"  IMPACT SCORE {b['impact_score']} | affected leads {b.get('affected_leads')}")
            print(f"  PROPOSED {label(p['created_minute'])} as {p['proposal_id']} via {p['path']}")
            if p["path"] == "approval":
                decided = store.db.execute(
                    "SELECT summary FROM decisions WHERE kind = 'approval_approved' AND details LIKE ?",
                    (f"%{p['proposal_id']}%",)).fetchone()
                print(f"  APPROVED: {p['approval_id']} by the client's approver ({decided[0] if decided else 'see log'})")
            else:
                print("  APPROVED: auto-apply, permitted by the workflow's policy for these dimensions")
    h = m // 60
    print(f"  BEFORE (24h): {_kpis(store, wid, h - 24, h)}")
    print(f"  AFTER  (24h): {_kpis(store, wid, h, h + 24)}")
    print("  WHAT HAPPENED NEXT:")
    later = store.db.execute(
        f"SELECT ts, kind, summary FROM decisions WHERE workflow_id = ? AND ts > ? AND ts <= ? AND kind IN "
        f"({','.join('?' * len(KINDS_NEXT))}) ORDER BY id LIMIT 10",
        (wid, ev["ts"][:16], _iso(d0, m + 2880), *KINDS_NEXT)).fetchall()
    for ts, kind, s in later:
        print(f"    {ts[:16]} {kind}: {s[:130]}")
    nxt = [e for e in events if minute_of(e["ts"], d0) > m]
    if nxt:
        print(f"    next config change: v{nxt[0]['version']} at {label(minute_of(nxt[0]['ts'], d0))} by {nxt[0]['actor']}")
    if not later and not nxt:
        print("    nothing notable")


def _iso(d0: dt.date, minute: int) -> str:
    return (dt.datetime.combine(d0, dt.time()) + dt.timedelta(minutes=minute)).isoformat()[:16]


def _fmt(alloc: dict) -> str:
    return ", ".join(f"{k} {v:.0%}" for k, v in alloc.items())
