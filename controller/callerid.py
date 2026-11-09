"""Caller-ID manager: spread calls over healthy phone numbers, and stop using numbers that are spam-flagged.

Caller IDs are picked at random for each dial, so numbers dialled in the same hours under the same config reach
the same kind of people, and their pickup can be compared fairly. Two traps it avoids:
  * A busy or recently busy number has lower pickup just from load. Load changes slowly; a spam flag is sudden.
    So a number already in use is only flagged when its pickup suddenly falls relative to the other numbers.
  * When running campaigns keep their old config (next_campaign_only), old and new leads are mixed. So a new
    number is judged only on calls made under the config version that introduced it.

Rules, in order:
  1. A number in use is flagged -> move its calls to the healthy numbers (urgent).
  2. A number on trial -> once it has 300 dials it must reach 80% of the established numbers' pickup to become
     established; otherwise it is flagged.
  3. Numbers never used -> with auto-apply: put each on trial with 5% of calls.
                           without auto-apply (approvals are scarce): spread evenly at once; they go on trial.
  4. Otherwise -> keep calls spread evenly over the established numbers.
"""
import math

from .clock import minute_of
from .governance import Change

PROBE_SHARE = 0.05
MIN_TRIAL_DIALS = 300
TRIAL_OK_RATIO = 0.80
DROP_RATIO = 0.60
FLAG_Z = -2.5
RECENT_HOURS = 1
BEFORE_HOURS = 24
MIN_RECENT_DIALS = 100
MIN_BEFORE_DIALS = 300
MIN_EARLY_DIALS = 100
MIN_CHANGE = 0.02
SUPERVISOR_COOLDOWN = 48 * 60


def total_variation(a: dict, b: dict) -> float:
    return 0.5 * sum(abs(a.get(k, 0) - b.get(k, 0)) for k in set(a) | set(b))


def normalise(weights: dict) -> dict:
    s = sum(weights.values())
    return {k: round(v / s, 6) for k, v in sorted(weights.items()) if v / s > 1e-6}


def equal_split(numbers) -> dict:
    return normalise({d: 1.0 for d in numbers})


def step_toward(cur: dict, target: dict, max_shift: float) -> dict:
    """Moves from cur toward target by at most max_shift (total-variation distance), as auto-apply requires."""
    tv = total_variation(cur, target)
    if tv <= max_shift + 1e-9:
        return target
    f = max_shift * 0.995 / tv
    return normalise({k: cur.get(k, 0) + f * (target.get(k, 0) - cur.get(k, 0)) for k in set(cur) | set(target)})


def pooled(stats: dict, numbers) -> tuple:
    return sum(stats.get(d, (0, 0))[0] for d in numbers), sum(stats.get(d, (0, 0))[1] for d in numbers)


def z_below(n: int, c: int, m: int, k: int, ratio: float) -> float:
    """z-score of 'pickup c/n is below ratio x pickup k/m'. Very negative = clearly below."""
    p, q = c / n, k / m
    se = math.sqrt(p * (1 - p) / n + ratio ** 2 * q * (1 - q) / m) or 1e-9
    return (p - ratio * q) / se


def sudden_drops(recent: dict, before: dict, established: list) -> dict:
    """Established numbers whose pickup, relative to the other established numbers, fell below 60% of before."""
    out = {}
    for d in established:
        others = [k for k in established if k != d]
        (nr, cr), (nb, cb) = recent.get(d, (0, 0)), before.get(d, (0, 0))
        (mr, kr), (mb, kb) = pooled(recent, others), pooled(before, others)
        if min(nr, mr) < MIN_RECENT_DIALS or min(nb, mb) < MIN_BEFORE_DIALS or not (kb and kr):
            continue
        usual = (cb / nb) / (kb / mb)
        z = z_below(nr, cr, mr, kr, DROP_RATIO * usual)
        if z < FLAG_Z:
            out[d] = {"reason": "sudden drop", "pickup_last_hour": round(cr / nr, 4), "dials_last_hour": nr,
                      "others_last_hour": round(kr / mr, 4), "usual_ratio": round(usual, 3), "z": round(z, 2)}
    return out


def trial_verdict(stats: dict, d: str, established: list):
    """('healthy'|'flagged'|None, evidence) for a number on trial, compared with the established numbers."""
    n, c = stats.get(d, (0, 0))
    m, k = pooled(stats, established)
    if n < MIN_EARLY_DIALS or not m or not k:
        return None, {}
    ratio = (c / n) / (k / m)
    ev = {"reason": "trial", "pickup": round(c / n, 4), "dials": n, "established_pickup": round(k / m, 4),
          "ratio": round(ratio, 3)}
    if z_below(n, c, m, k, TRIAL_OK_RATIO) < FLAG_Z:
        return "flagged", ev
    if n >= MIN_TRIAL_DIALS:
        return ("healthy" if ratio >= TRIAL_OK_RATIO else "flagged"), ev
    return None, ev


def describe(stats: dict) -> dict:
    return {d: {"dials": n, "pickup": round(c / n, 4) if n else None} for d, (n, c) in sorted(stats.items())}


class CallerIdManager:
    def __init__(self, store, gov, workflows: dict, d0):
        self.store = store
        self.gov = gov
        self.wf = workflows
        self.d0 = d0

    def plan(self, wid: str, now: int, ts: str):
        """Updates what we know about each number and returns the caller-ID change we want, if any."""
        key = f"callerid:{wid}"
        version, cfg = self.gov.current(wid)
        cur = cfg["allocations"]["default"]["caller_id"]
        st = self.store.meta(key) or {"known": sorted(cur), "trial": {}, "flagged": {}}
        h = now // 60

        for d in cur:
            if d not in st["known"] and d not in st["trial"] and d not in st["flagged"]:
                st["trial"][d] = {"version": version, "hour": h}
                self.store.log_decision(ts, wid, "caller_id_trial", f"{d} is on trial from config v{version}")

        established = [d for d in st["known"] if d not in st["flagged"]]
        recent = self.store.caller_stats(wid, h - RECENT_HOURS)
        before = self.store.caller_stats(wid, h - RECENT_HOURS - BEFORE_HOURS, until_hour=h - RECENT_HOURS)
        for d, ev in sudden_drops(recent, before, established).items():
            self._flag(st, wid, ts, d, ev)

        for d, t in list(st["trial"].items()):
            stats = self.store.caller_stats(wid, t["hour"], min_version=t["version"])
            verdict, ev = trial_verdict(stats, d, [k for k in established if k not in st["trial"]])
            if verdict == "flagged":
                del st["trial"][d]
                self._flag(st, wid, ts, d, ev)
            elif verdict == "healthy":
                del st["trial"][d]
                st["known"] = sorted(set(st["known"]) | {d})
                self.store.log_decision(ts, wid, "caller_id_healthy",
                                        f"{d} passed its trial: pickup {ev['pickup']:.1%} vs {ev['established_pickup']:.1%} "
                                        f"on established numbers ({ev['dials']} dials)", ev)
        self.store.set_meta(key, st)

        pol = self.wf[wid]["governance"]["auto_apply"]
        auto_ok = pol["enabled"] and "caller_id" in pol["allowed_dims"]
        flagged = set(st["flagged"])
        urgent = any(d in flagged for d in cur)
        if not urgent and self._supervisor_touched_recently(wid, now):
            return

        healthy = [d for d in cur if d not in flagged]
        untried = [d for d in self.wf[wid]["options"]["caller_id"]
                   if d not in cur and d not in flagged and d not in st["known"] and d not in st["trial"]]
        if urgent:
            if not healthy:
                return
            target = normalise({d: cur[d] for d in healthy}) if st["trial"] else equal_split(healthy)
            why = f"stop using {sorted(flagged & set(cur))} (spam-flagged: pickup far below the other numbers)"
        elif st["trial"]:
            return
        elif untried and auto_ok:
            target = normalise({**{d: w * (1 - PROBE_SHARE * len(untried)) for d, w in cur.items()},
                                **{d: PROBE_SHARE for d in untried}})
            why = f"put {untried} on trial with {PROBE_SHARE:.0%} of calls each before spreading load onto them"
        elif untried:
            target = equal_split(healthy + untried)
            why = (f"spread calls evenly over {sorted(target)}: a number's pickup falls as its call volume rises, so "
                   f"sharing the load raises pickup. New numbers go on trial and are removed if they underperform")
        else:
            target = equal_split(healthy)
            why = f"keep calls spread evenly over the healthy numbers {sorted(target)}"
        if total_variation(cur, target) < MIN_CHANGE:
            return

        evidence = {"last_hour": describe(recent), "flagged": st["flagged"], "from": cur, "to": target}

        def set_to(alloc):
            def edit(c):
                c["allocations"]["default"]["caller_id"] = alloc
            return edit

        if auto_ok:
            step = step_toward(cur, target, pol["max_shift"])
            return Change("caller_id", "auto", set_to(step), urgent=urgent, rationale=why, evidence=evidence,
                          summary=f"Caller ID {cur} -> {step}" + (" (step toward target)" if step != target else ""))
        wf = self.wf[wid]
        next_only = wf["mid_flight"] == "next_campaign_only"
        return Change(
            "caller_id", "approval", set_to(target),
            summary=f"Caller ID: {'stop using a spam-flagged number' if urgent else 'spread calls evenly'} "
                    f"({', '.join(f'{d} {w:.0%}' for d, w in target.items())})",
            diff={"allocations.default.caller_id": {"from": cur, "to": target}},
            affected_leads=(wf["daily_batch"]["size"] if next_only
                            else self.store.active_leads(wid, now, len(wf["cadence"]), self.d0)),
            dry_run={"method": "observed pickup per caller ID over the last day of dialling",
                     "observed": describe(before),
                     "expected": "the busiest number's pickup recovers as its load falls; new numbers are measured "
                                 "on trial from the first day they carry calls",
                     "applies_to": "new campaigns only" if next_only else "all running campaigns"},
            impact=0.9 if urgent else 0.5, rationale=why, evidence=evidence, urgent=urgent)

    def _flag(self, st: dict, wid: str, ts: str, d: str, ev: dict):
        if d in st["flagged"]:
            return
        st["flagged"][d] = ev
        if ev["reason"] == "trial":
            msg = (f"{d} failed its trial: pickup {ev['pickup']:.1%} is {ev['ratio']:.0%} of the established "
                   f"numbers' ({ev['dials']} dials); needs {TRIAL_OK_RATIO:.0%}")
        else:
            msg = (f"{d} pickup suddenly dropped to {ev['pickup_last_hour']:.1%} vs {ev['others_last_hour']:.1%} on "
                   f"the other numbers in the last hour ({ev['dials_last_hour']} dials, z={ev['z']})")
        self.store.log_decision(ts, wid, "caller_id_flagged", msg, ev)

    def _supervisor_touched_recently(self, wid: str, now: int) -> bool:
        for ev in self.store.world_events(wid, "config_changed"):
            if ev["actor"] == "supervisor" and "caller_id" in ev["changed_dims"]:
                return now - minute_of(ev["ts"], self.d0) < SUPERVISOR_COOLDOWN
        return False
