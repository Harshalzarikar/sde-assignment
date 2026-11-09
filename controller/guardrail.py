"""Guardrail watch: each workflow promises not to let its guardrail KPI drop by more than `max_drop`.

The simulator does not define the drop, so we read it as relative to the history baseline (stricter than
percentage points at these levels): engagement 0.60 -> breach below 0.588.

Once a day, after dialling ends, the guardrail KPI over the last 2 days is compared with the baseline: the last
3 days of history, i.e. the steady state just before we started. (All of history was a biased baseline for
pickup: caller-ID load builds up during history, so its first days have unusually high pickup. On public_03 that
raised two false guardrail alerts on workflows whose pickup was in fact 7% above the no-controller run.)
  - breach  : more than max_drop below baseline, and the shortfall is not luck (z <= -3) -> alert, with the
              controller changes of the last 48 hours that touched dimensions able to move that KPI.
  - watch   : below baseline by more than max_drop but not yet clearly (z > -3) -> noted, no alert.
Why -3: z treats calls as independent, but whole days differ (each day's campaign mix, caller-ID load), so the
real spread is wider. At -2, a no-effect test world raised two false alerts in 24 daily checks; on hundreds of
workflows that would page someone every day.
Mix effects are possible: raising pickup brings more hard-to-reach people to the phone, which can lower engagement
a little with no change in how the agent behaves. The alert says so; it is a human's call, so nothing is reverted
automatically.
"""
import math

from .clock import label

GUARDRAIL_Z = -3.0
WINDOW_DAYS = 2
BASELINE_DAYS = 3
MOVES = {"pickup_rate": {"caller_id", "schedule"},
         "engagement_rate": {"welcome_message", "voice", "prompt_version", "cohorts", "schedule", "caller_id"}}


class GuardrailWatch:
    def __init__(self, store, workflows: dict):
        self.store = store
        self.wf = workflows

    def check(self, wid: str, now: int, ts: str):
        w = self.wf[wid]
        if now % 1440 != w["dialling_window"][1] * 60:
            return
        g = w["objective"].get("guardrail") or {}
        kpi, max_drop = next(((k.replace("_max_drop", ""), v) for k, v in g.items() if k.endswith("_max_drop")),
                             (None, None))
        if kpi is None:
            return
        day = now // 1440
        base = self._kpi(wid, kpi, -BASELINE_DAYS, 0)
        cur = self._kpi(wid, kpi, max(day - WINDOW_DAYS + 1, 0), day + 1)
        if not base or not cur:
            return
        (bp, bn), (cp, cn) = base, cur
        se = math.sqrt(bp * (1 - bp) / bn + cp * (1 - cp) / cn)
        rel, z = cp / bp - 1, (cp - bp) / se if se else 0.0
        ev = {"kpi": kpi, "baseline": round(bp, 4), "last_days": round(cp, 4), "relative_change": round(rel, 4),
              "z": round(z, 2), "max_drop": max_drop, "calls": cn}
        if rel >= -max_drop:
            return
        if z > GUARDRAIL_Z:
            self.store.log_decision(ts, wid, "guardrail_watch",
                                    f"{kpi} {cp:.1%} vs {bp:.1%} baseline ({rel:+.1%}); below the {max_drop:.0%} limit "
                                    f"but not clearly (z={z:.1f}); watching", ev)
            return
        recent = [p for p in self.store.proposals(wid, ("applied",)) if p["created_minute"] >= now - 2880]
        suspects = [p["body"].get("change_summary", "")[:100] for p in recent
                    if MOVES[kpi] & set(_dims(p))]
        ev["suspects"] = suspects
        self.store.log_decision(ts, wid, "alert",
                                f"GUARDRAIL {kpi} {cp:.1%} vs {bp:.1%} baseline over {WINDOW_DAYS} days ({rel:+.1%}, "
                                f"limit -{max_drop:.0%}, z={z:.1f}) at {label(now)}. Our changes in the last 48h that "
                                f"can move it: {suspects or 'none'}. If none, look for a mix effect or a supervisor edit.",
                                ev)

    def _kpi(self, wid: str, kpi: str, day_from, day_to):
        dials, conn, eng = self.store.db.execute(
            "SELECT SUM(dials), SUM(connected), SUM(engaged) FROM call_counts WHERE workflow_id = ? AND day >= ? "
            "AND day < ?", (wid, day_from, day_to)).fetchone()
        if not dials or not conn:
            return None
        return (conn / dials, dials) if kpi == "pickup_rate" else (eng / conn, conn)


def _dims(p: dict) -> list:
    diff = p["body"].get("diff") or {}
    dims = set()
    for k in diff:
        dims.update(x for x in ("caller_id", "welcome_message", "voice", "prompt_version", "schedule") if x in k)
        if k in ("cohorts", "group_allocations"):
            dims.add("cohorts")
    if not diff:  # auto-applied: summary only
        s = p["body"].get("change_summary", "")
        dims.update(x for x, word in (("caller_id", "Caller ID"), ("welcome_message", "welcome_message"),
                                       ("voice", "voice")) if word in s)
    return sorted(dims)
