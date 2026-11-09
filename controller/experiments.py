"""Experiments on the welcome message and the voice: find options that keep more people on the call.

Only for workflows whose primary KPI is engagement. Greeting and voice are heard after someone answers, so they
cannot move pickup; on a pickup-first workflow (KYC) a test would only risk its engagement guardrail.

For each dimension there is one champion (the main option) and challengers. Calls are split at random, and
options are only compared inside the same config version (a "stratum"), where the split was random.

Rules, checked every hour, for each challenger against the champion:
  1. Loser    : >= 150 answered calls and z <= -2.5            -> removed.
  2. No gain  : >= 800 answered calls and even the optimistic
                estimate (lift + 2 SE) is below +3%            -> removed.
  2b. Faded  : it earned more calls once (rule 4), now it is behind (z < 0)  -> removed. One chance to climb:
                otherwise a fading option would sit on 8% of calls for the rest of the run.
                Tried and rejected: also removing it when its last 24h dropped clearly below its earlier lead. An
                option climbs because its early estimate is high, so that estimate is biased upward and a 'drop'
                shows up even when nothing changed; on public_01 that removed two genuinely good options (harmful).
  3. Winner   : >= 400 answered calls, z >= 3, lift >= +3%,
                and still ahead over the last 24 hours          -> becomes champion with 80% of calls; the old
                                                                   champion keeps 20% as the control group.
  4. Otherwise its share follows the evidence: 8% to start, 20% once it looks promising (z >= 1, >= 150 calls),
     35% once it looks strong (z >= 2, >= 300 calls), and only if it is also ahead over the last 24 hours
     (a fading option must not get more calls on the strength of its early days). Shares step down again if the
     evidence weakens.
  5. A promoted winner is compared with the control group over the last 24 hours, starting 12 hours after it
     went live. If it is now behind (lift < 0, z <= -2, >= 150 control calls), it has stopped working: it is
     rolled back and removed. The bar is high because the check repeats every hour for days; at z <= -1 and
     -1.5, real winners were rolled back after an unlucky day on the public scenarios.
  6. A winner waiting for approval must still lead the control over the last 24 hours, or the promotion is
     cancelled. An approved change is withdrawn, not applied, if it no longer matches what the evidence says.
Where every change needs a human, only milestones (start, remove, promote, roll back) are requested at any time;
share adjustments wait for at most one request a day, so the approver's queue stays short.
A supervisor's edit to a dimension is never reverted: their choice becomes the champion, that dimension is left
alone for 48 hours, and later changes to it always go to a human for approval.
"""
import math
from collections import defaultdict

from .callerid import normalise, step_toward, total_variation
from .clock import minute_of
from .governance import Change

DIMS = ("welcome_message", "voice")
TIER_SHARE = (0.08, 0.20, 0.35)
CONTROL_SHARE = 0.20
MAX_NON_CHAMPION = 0.60
LOSER_N, LOSER_Z = 150, -2.5
FUTILE_N, MIN_LIFT = 800, 0.03
WIN_N, WIN_Z = 400, 3.0
UP = ((150, 1.0), (300, 2.0))  # (calls, z) needed to reach tier 1, tier 2
RECENT_HOURS = 24
RECENT_MIN_N = 50
ROLLBACK_Z, ROLLBACK_MIN_CONTROL = -2.0, 150
WATCH_GRACE_HOURS = 12
SUPERVISOR_COOLDOWN = 48 * 60
MIN_CHANGE = 0.03
ROUTINE_APPROVAL_GAP = 24 * 60


def compare(rows: list, arm: str, ref: str):
    """Arm vs reference engagement, combined over strata where both ran (each weighted by its sample size)."""
    by = defaultdict(dict)
    for stratum, opt, n, e in rows:
        if opt in (arm, ref):
            by[stratum][opt] = (n, e)
    num = den = var = 0.0
    na_t = nr_t = er_t = ea_t = 0
    for s in by.values():
        (na, ea), (nr, er) = s.get(arm, (0, 0)), s.get(ref, (0, 0))
        if not na or not nr:
            continue
        w = na * nr / (na + nr)
        p = (ea + er) / (na + nr)
        num += w * (ea / na - er / nr)
        den += w
        var += w * p * (1 - p)
        na_t, nr_t, ea_t, er_t = na_t + na, nr_t + nr, ea_t + ea, er_t + er
    if not den or not er_t:
        return None
    diff, se = num / den, math.sqrt(var) / den
    base = er_t / nr_t
    return {"n": na_t, "n_ref": nr_t, "rate": round(ea_t / na_t, 4), "ref_rate": round(base, 4),
            "lift": round(diff / base, 4), "se_lift": round(se / base, 4), "z": round(diff / se, 2) if se else 0.0}


def difference_z(a: dict, b: dict) -> float:
    """z of 'lift a is bigger than lift b', for two independent estimates."""
    se = (a["se_lift"] ** 2 + b["se_lift"] ** 2) ** 0.5
    return (a["lift"] - b["lift"]) / se if se else 0.0


def fmt(c) -> str:
    return f"{c['lift']:+.1%} (z={c['z']}, {c['n']} answered calls vs {c['n_ref']})"


class ExperimentManager:
    def __init__(self, store, gov, workflows: dict, d0):
        self.store = store
        self.gov = gov
        self.wf = workflows
        self.d0 = d0
        gov.checks.append(self._still_wanted)

    def enabled(self, wid: str) -> bool:
        return self.wf[wid]["objective"]["primary"] == "engagement_rate"

    def plan(self, wid: str, now: int, ts: str):
        if not self.enabled(wid):
            return None
        key = f"exp:{wid}"
        _, cfg = self.gov.current(wid)
        st = self.store.meta(key) or {"dims": {}, "last_eval_hour": None}
        h = now // 60
        evaluate = st["last_eval_hour"] != h
        notes, urgent, desired, actual = [], False, {}, {}
        for dim in DIMS:
            if len(self.wf[wid]["options"][dim]) < 2:
                continue
            cur = cfg["allocations"]["default"][dim]
            actual[dim] = cur
            d = st["dims"].setdefault(dim, self._new_dim(cur))
            self._notice_supervisor(wid, dim, d, cur, ts)
            if d["supervisor_minute"] is not None and now - d["supervisor_minute"] < SUPERVISOR_COOLDOWN:
                desired[dim] = cur
                continue
            if evaluate:
                notes += self._evaluate(wid, dim, d, cur, h, ts)
            urgent |= any(n.startswith("roll back") for n in notes)
            desired[dim] = self._target(wid, dim, d)
        if evaluate:
            st["last_eval_hour"] = h
        self.store.set_meta(key, st)

        moved = {dim: total_variation(actual[dim], desired[dim]) for dim in desired}
        if not moved or max(moved.values()) < MIN_CHANGE:
            return None
        change = self._change(wid, now, st, actual, desired, notes, urgent)
        if change.path == "approval" and not self._milestone(st, actual, desired) \
                and now - self._last_request_minute(wid) < ROUTINE_APPROVAL_GAP:
            return None
        return change

    @staticmethod
    def _milestone(st: dict, actual: dict, desired: dict) -> bool:
        """True when the change starts or stops an option, or changes the champion (not just share tweaks)."""
        for dim in desired:
            a = {o for o, w in actual[dim].items() if w > 0}
            b = {o for o, w in desired[dim].items() if w > 0}
            if a != b or max(actual[dim], key=actual[dim].get) != st["dims"][dim]["champion"]:
                return True
        return False

    def _last_request_minute(self, wid: str) -> int:
        rows = [p for p in self.store.proposals(wid) if p["path"] == "approval" and "experiment" in p["body"].get("dry_run", {})]
        return rows[-1]["created_minute"] if rows else -10 ** 9

    def _still_wanted(self, row: dict):
        """Called just before an approved change is applied: a reason to withdraw it, or None."""
        st = self.store.meta(f"exp:{row['workflow_id']}")
        if not st:
            return None
        for dim, d in st["dims"].items():
            change = row["body"]["diff"].get(f"allocations.default.{dim}")
            if not change:
                continue
            to, frm = change["to"], change["from"]
            if max(to, key=to.get) != d["champion"]:
                return f"{dim}: the approved change makes {max(to, key=to.get)} the main option, but the evidence now says {d['champion']}"
            want = self._target(row["workflow_id"], dim, d)
            grown = [o for o in to if o != d["champion"]
                     and to[o] > frm.get(o, 0) + 1e-6 and to[o] > want.get(o, 0) + 1e-6]
            if grown:
                return (f"{dim}: the approved change gives more calls to {grown} than the evidence now supports "
                        f"(now we want {want})")
        return None

    # ---- state
    @staticmethod
    def _new_dim(cur: dict) -> dict:
        return {"champion": max(cur, key=cur.get), "control": None, "tier": {}, "retired": {}, "promoted": None,
                "supervisor_minute": None, "protected": False}

    def _notice_supervisor(self, wid: str, dim: str, d: dict, cur: dict, ts: str):
        for ev in self.store.world_events(wid, "config_changed", limit=10):
            if ev["actor"] != "supervisor" or dim not in ev["changed_dims"]:
                continue
            m = minute_of(ev["ts"], self.d0)
            if d["supervisor_minute"] is not None and m <= d["supervisor_minute"]:
                return
            champ = max(cur, key=cur.get)
            d.update(supervisor_minute=m, protected=True, champion=champ, control=None, promoted=None, climbed=[])
            d["retired"].pop(champ, None)
            self.store.log_decision(ts, wid, "experiment_paused",
                                    f"supervisor changed {dim} to {cur}; {champ} is now the champion. We will not "
                                    f"change {dim} for 48h, and after that only with a human's approval", ev)
            return

    def _rows(self, wid: str, dim: str, since_hour: int = 0) -> list:
        """Calls whose option came from the whole-workflow random split (not from a group rule)."""
        from .segments import rule_cohorts
        excluded = rule_cohorts(self.store, wid, dim)
        return [r for r in self.store.arm_stats(wid, dim, since_hour) if r[0][1] not in excluded]

    def _testing(self, wid: str, dim: str, d: dict) -> list:
        return [o for o in self.wf[wid]["options"][dim]
                if o not in (d["champion"], d["control"]) and o not in d["retired"]]

    # ---- rules
    def _evaluate(self, wid: str, dim: str, d: dict, cur: dict, h: int, ts: str) -> list:
        rows = self._rows(wid, dim)
        recent = self._rows(wid, dim, h - RECENT_HOURS)
        notes = []
        champ = d["champion"]

        live = max(cur, key=cur.get) == champ
        if d["promoted"] and d["promoted"]["hour"] is None and not live:
            rc = compare(recent, champ, d["control"])
            if rc and rc["n"] >= RECENT_MIN_N and rc["lift"] < 0 and rc["z"] <= ROLLBACK_Z:
                msg = (f"cancel promotion of {dim} {champ}: before it went live it fell behind {d['control']} "
                       f"over the last 24h, {fmt(rc)}")
                d["retired"][champ] = {"reason": "stopped leading before going live", "ts": ts, "evidence": rc}
                d.update(champion=d["control"], control=None, promoted=None, climbed=[])
                self.store.log_decision(ts, wid, "winner_cancelled", msg, rc)
                return [msg]
        if d["promoted"] and d["promoted"]["hour"] is None and live:
            d["promoted"]["hour"] = h
            self.store.log_decision(ts, wid, "winner_live", f"{dim} {champ} is live as champion; watching it "
                                    f"against the control group {d['control']}", d["promoted"])
        if d["promoted"] and d["promoted"]["hour"] is not None and h - d["promoted"]["hour"] >= WATCH_GRACE_HOURS:
            since = max(d["promoted"]["hour"], h - RECENT_HOURS)
            r = compare(self._rows(wid, dim, since), champ, d["control"])
            if r and r["n_ref"] >= ROLLBACK_MIN_CONTROL and r["lift"] < 0 and r["z"] <= ROLLBACK_Z:
                msg = (f"roll back {dim} {champ} -> {d['control']}: it has stopped working, now {fmt(r)} against "
                       f"the control group (it was {d['promoted']['evidence']['lift']:+.1%} when promoted)")
                d["retired"][champ] = {"reason": "stopped working", "ts": ts, "evidence": r}
                d.update(champion=d["control"], control=None, promoted=None, climbed=[])
                self.store.log_decision(ts, wid, "winner_rolled_back", msg, r)
                return [msg]

        for arm in self._testing(wid, dim, d):
            c = compare(rows, arm, d["champion"])
            if not c:
                continue
            tier = d["tier"].get(arm, 0)
            climbed = d.setdefault("climbed", [])
            if c["n"] >= LOSER_N and c["z"] <= LOSER_Z:
                self._retire(wid, dim, d, arm, "loser", c, ts, notes, f"remove {dim} {arm}: worse than "
                             f"{d['champion']}, {fmt(c)}")
            elif arm in climbed and c["z"] < 0:
                self._retire(wid, dim, d, arm, "stopped working", c, ts, notes,
                             f"remove {dim} {arm}: it earned more calls earlier but is now behind, {fmt(c)}")
            elif c["n"] >= FUTILE_N and c["lift"] + 2 * c["se_lift"] < MIN_LIFT:
                self._retire(wid, dim, d, arm, "no meaningful gain", c, ts, notes,
                             f"stop testing {dim} {arm}: at best {c['lift'] + 2 * c['se_lift']:+.1%}, {fmt(c)}")
            elif c["n"] >= WIN_N and c["z"] >= WIN_Z and c["lift"] >= MIN_LIFT and not d["promoted"]:
                rc = compare(recent, arm, d["champion"])
                if rc and rc["n"] >= RECENT_MIN_N and rc["lift"] > 0:
                    self._promote(wid, dim, d, arm, c, rc, ts, notes)
                    break
            else:
                new = tier
                if tier < 2 and c["n"] >= UP[tier][0] and c["z"] >= UP[tier][1]:
                    rc = compare(recent, arm, d["champion"])
                    if rc and rc["n"] >= RECENT_MIN_N and rc["lift"] > 0:
                        new = tier + 1
                elif tier > 0 and c["z"] < UP[tier - 1][1] - 1.0:
                    new = tier - 1
                if new != tier:
                    d["tier"][arm] = new
                    if new > tier and arm not in climbed:
                        climbed.append(arm)
                    msg = (f"{dim} {arm} {'looks better' if new > tier else 'evidence weakened'}: {fmt(c)}; "
                           f"share {TIER_SHARE[tier]:.0%} -> {TIER_SHARE[new]:.0%}")
                    self.store.log_decision(ts, wid, "arm_share", msg, c)
                    notes.append(msg)
        return notes

    def _retire(self, wid, dim, d, arm, reason, c, ts, notes, msg):
        d["retired"][arm] = {"reason": reason, "ts": ts, "evidence": c}
        d["tier"].pop(arm, None)
        self.store.log_decision(ts, wid, "arm_removed", msg, c)
        notes.append(msg)

    def _promote(self, wid, dim, d, arm, c, rc, ts, notes):
        old = d["champion"]
        d.update(champion=arm, control=old, promoted={"arm": arm, "hour": None, "evidence": c, "recent": rc}, climbed=[])
        d["tier"] = {}  # the other challengers were measured against the old champion: back to the starting share
        msg = (f"promote {dim} {arm} over {old}: {fmt(c)}, still ahead in the last 24h ({rc['lift']:+.1%}); "
               f"{old} keeps {CONTROL_SHARE:.0%} as the control group to catch a fading winner")
        self.store.log_decision(ts, wid, "winner_promoted", msg, {"overall": c, "last_24h": rc})
        notes.append(msg)

    def _target(self, wid: str, dim: str, d: dict) -> dict:
        shares = {a: TIER_SHARE[d["tier"].get(a, 0)] for a in self._testing(wid, dim, d)}
        room = MAX_NON_CHAMPION - (CONTROL_SHARE if d["control"] else 0)
        total = sum(shares.values())
        if total > room:
            shares = {a: s * room / total for a, s in shares.items()}
        if d["control"]:
            shares[d["control"]] = CONTROL_SHARE
        shares[d["champion"]] = 1 - sum(shares.values())
        return normalise(shares)

    # ---- the proposal
    def _change(self, wid: str, now: int, st: dict, actual: dict, desired: dict, notes: list, urgent: bool):
        pol = self.wf[wid]["governance"]["auto_apply"]
        dims = [dim for dim in desired if total_variation(actual[dim], desired[dim]) >= 1e-6]
        auto = (pol["enabled"] and all(dim in pol["allowed_dims"] for dim in dims)
                and not any(st["dims"][dim]["protected"] for dim in dims))
        target = {dim: (step_toward(actual[dim], desired[dim], pol["max_shift"]) if auto else desired[dim])
                  for dim in dims}
        observed, expected = {}, 0.0
        for dim in dims:
            rows = self._rows(wid, dim)
            champ = st["dims"][dim]["champion"]
            for opt in set(actual[dim]) | set(target[dim]):
                c = compare(rows, opt, champ) if opt != champ else None
                observed[f"{dim}={opt}"] = c or ("champion" if opt == champ else "no data yet")
                lift = c["lift"] if c else 0.0
                expected += (target[dim].get(opt, 0) - actual[dim].get(opt, 0)) * lift
        why = "; ".join(notes) or "start testing: " + ", ".join(
            f"{dim} {[o for o in target[dim] if o not in actual[dim]]}" for dim in dims)
        summary = "Experiment: " + "; ".join(
            f"{dim} " + ", ".join(f"{o} {w:.0%}" for o, w in target[dim].items()) for dim in dims)

        def edit(cfg):
            for dim in dims:
                cfg["allocations"]["default"][dim] = target[dim]

        wf = self.wf[wid]
        active = self.store.active_leads(wid, now, len(wf["cadence"]), self.d0)
        return Change(
            "experiment", "auto" if auto else "approval", edit, summary=summary, rationale=why, urgent=urgent,
            diff={f"allocations.default.{dim}": {"from": actual[dim], "to": target[dim]} for dim in dims},
            evidence={"observed": observed},
            dry_run={"method": "random split of calls; options compared only within the same config version",
                     "observed_vs_champion": observed,
                     "expected_engagement_change": round(expected, 4),
                     "applies_to": ("new campaigns only" if wf["mid_flight"] == "next_campaign_only"
                                    else "all running campaigns")},
            impact=0.8 if urgent else min(0.8, 0.2 + 4 * abs(expected)),
            affected_leads=int(active * max(total_variation(actual[dim], target[dim]) for dim in dims)))
