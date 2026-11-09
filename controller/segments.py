"""Groups of people (cohorts): give a group its own greeting or voice when the evidence says it differs.

The whole-workflow experiment splits calls at random for everyone, so each group can be read separately afterwards
with no extra test traffic. A group is one value set of one lead feature (e.g. age_band in [46-60, 60+]); one
feature at a time keeps groups big enough to measure and the cohort list short.

One shared cohort object: every cohort this module creates gets a stable id from its definition ("grp_age_60plus")
and is recorded in the notebook (meta 'seg:<workflow>'). The experiment manager, the supervisor view and the
audit trail all use that same id and definition.

At most one group rule per dimension (greeting, voice). Checked every hour:
  1. Protect a group: the whole-workflow champion is a promoted winner, but in one group it is clearly worse than
     the option it replaced (lift <= -5%, z <= -2.5, >= 150 calls on the old option there)
     -> that group goes back to the old option at 80% (the newer one keeps 20%, so we keep measuring).
  2. Screen: in the random-split data, an option beats the champion in one group (z >= 2.75, >= 100 calls,
     lift >= +5%), the group is >= 5% of calls, and its lead inside the group is clearly bigger than outside it
     (difference z >= 2). An option that is better for everyone looks better in any group; that is the
     whole-workflow experiment's job, not a group rule.
     -> a trial inside that group only: option 50%, champion 50%. Nobody outside the group hears it.
     Screening looks at up to ~13 groups x 3 options x 2 dimensions every hour, so it will sometimes pick a lucky
     group. That is why a screen only buys a trial, never a rule.
  3. Confirm: the trial's own calls (fresh data, collected after the screen) must agree: lift > 0, z >= 2.5,
     >= 100 calls on each side -> rule: option 80%, champion 20% as the control group.
     Behind (lift < 0, z <= -1.5) or not confirmed within 72 hours -> trial dropped, not retried.
  A trial or rule is dropped when the whole-workflow champion changes under it (its option became the winner for
  everyone, or the option it was compared with is no longer the main one): otherwise it could take calls away
  from a new winner.
  4. A rule whose winner falls behind its control over the last 24 hours (lift < 0, z <= -2, >= 100 control
     calls, after 12 hours) is removed: the group goes back to the whole-workflow setting.
Group changes always go to a human: they change what a segment of customers hears.
If anyone else has defined cohorts in the config, this module leaves cohorts alone entirely.
"""
from .experiments import compare, difference_z, fmt
from .governance import Change

PREFIX = "grp_"
SHORT = {"age_band": "age", "lang": "lang", "city_tier": "tier", "value_band": "value"}
DIMS = ("welcome_message", "voice")
SCREEN_Z, SCREEN_N, MIN_LIFT, SLICE_Z = 2.75, 100, 0.05, 2.0
DIFFERENCE_Z = 2.0
HARM_Z, HARM_LIFT, HARM_MIN_N = -2.5, -0.05, 150
MIN_GROUP_SHARE = 0.05
SHARES = {"trial": 0.5, "rule": 0.8}
CONFIRM_Z, CONFIRM_N, TRIAL_FAIL_Z, TRIAL_MAX_HOURS = 2.5, 100, -1.5, 72
RULE_GRACE_HOURS, RULE_ROLLBACK_Z, RULE_MIN_CONTROL = 12, -2.0, 100


def cohort_id(feature: str, values: list) -> str:
    return PREFIX + SHORT[feature] + "_" + "_".join(v.replace("+", "plus") for v in sorted(values))


def alloc(rule: dict) -> dict:
    s = SHARES[rule["stage"]]
    return {rule["winner"]: s, rule["control"]: round(1 - s, 6)}


def compile_cohorts(rules: dict) -> list:
    """[(id, where, {dim: allocation})] in evaluation order, ids unique. With a greeting rule and a voice rule,
    people in both groups get a combined cohort first, because a lead only belongs to the first cohort it matches."""
    items = sorted(rules.items())
    single = [(cohort_id(r["feature"], r["values"]), {r["feature"]: list(r["values"])}, {dim: alloc(r)})
              for dim, r in items]
    if len(items) < 2:
        return single
    (da, a), (db, b) = items
    if a["feature"] != b["feature"]:
        both = cohort_id(a["feature"], a["values"]) + "__" + cohort_id(b["feature"], b["values"])[len(PREFIX):]
        return [(both, {a["feature"]: list(a["values"]), b["feature"]: list(b["values"])},
                 {da: alloc(a), db: alloc(b)})] + single
    # Same feature (both rules on value_band=mid, say): split its values so no id or value set repeats.
    f = a["feature"]
    shared = sorted(set(a["values"]) & set(b["values"]))
    out = [(cohort_id(f, shared), {f: shared}, {da: alloc(a), db: alloc(b)})] if shared else []
    for dim, r in items:
        rest = sorted(set(r["values"]) - set(shared))
        if rest:
            out.append((cohort_id(f, rest), {f: rest}, {dim: alloc(r)}))
    return out


def rule_cohorts(store, wid: str, dim: str) -> set:
    """Every cohort id that has ever carried a group rule for this dimension. The whole-workflow experiment
    leaves those calls out: their option was chosen by the group rule, not by the random split."""
    st = store.meta(f"seg:{wid}") or {}
    return set(st.get("ever", {}).get(dim, []))


def pool(rows_by_value: dict, values) -> list:
    return [r for v in values for r in rows_by_value.get(v, [])]


def who(r: dict) -> str:
    return f"{SHORT[r['feature']]} {'/'.join(r['values'])}"


class SegmentManager:
    def __init__(self, store, gov, workflows: dict, d0):
        self.store = store
        self.gov = gov
        self.wf = workflows
        self.d0 = d0
        gov.checks.append(self._still_wanted)

    def plan(self, wid: str, now: int, ts: str):
        if self.wf[wid]["objective"]["primary"] != "engagement_rate":
            return None
        key = f"seg:{wid}"
        _, cfg = self.gov.current(wid)
        st = self.store.meta(key) or {"rules": {}, "retired": [], "ever": {}, "last_eval_hour": None,
                                      "stood_down": False}
        foreign = [c["id"] for c in cfg["cohorts"] if not c["id"].startswith(PREFIX)]
        if foreign:
            if not st["stood_down"]:
                st["stood_down"] = True
                self.store.log_decision(ts, wid, "groups_stood_down",
                                        f"someone else defined cohorts {foreign}; we will not change cohorts", {})
                self.store.set_meta(key, st)
            return None
        h = now // 60
        notes = []
        if st["last_eval_hour"] != h:
            st["last_eval_hour"] = h
            exp = self.store.meta(f"exp:{wid}") or {"dims": {}}
            for dim in DIMS:
                d = exp["dims"].get(dim)
                if not d or d.get("supervisor_minute") is not None and now - d["supervisor_minute"] < 48 * 60:
                    continue
                r = st["rules"].get(dim)
                if r and r["kind"] == "winner" and (r["winner"] == d["champion"] or r["control"] != d["champion"]):
                    notes += self._drop(wid, dim, st, ts, None, f"the main {dim} is now {d['champion']}")
                elif r and r["kind"] == "protect" and r["control"] != d["champion"]:
                    notes += self._drop(wid, dim, st, ts, None, f"the main {dim} is now {d['champion']}")
                elif r:
                    notes += self._watch(wid, dim, st, cfg, h, ts)
                else:
                    notes += self._find(wid, dim, d, st, ts)
        self.store.set_meta(key, st)

        want = compile_cohorts(st["rules"])
        have = [(c["id"], c["where"], dict(cfg["allocations"].get(c["id"], {}))) for c in cfg["cohorts"]]
        if _norm(want) == _norm(have):
            return None
        return self._change(wid, now, st, cfg, want, have, notes)

    # ---- 1-2: protect or screen
    def _find(self, wid: str, dim: str, d: dict, st: dict, ts: str) -> list:
        champ, control = d["champion"], d["control"]
        by = self._by_feature(wid, dim, set(st["ever"].get(dim, [])))
        sizes = self.store.segment_sizes(wid)
        if control:
            best = None
            for feat, rows in by.items():
                bad = [v for v, r in rows.items() if (c := compare(r, champ, control))
                       and c["n_ref"] >= HARM_MIN_N and c["lift"] <= HARM_LIFT and c["z"] <= HARM_Z]
                if bad and len(bad) < len(rows):
                    c = compare(pool(rows, bad), champ, control)
                    out = compare(pool(rows, [v for v in rows if v not in bad]), champ, control)
                    if out and difference_z(c, out) <= -DIFFERENCE_Z and (best is None or c["z"] < best[2]["z"]):
                        best = (feat, sorted(bad), c)
            if best:
                feat, values, c = best
                return self._add(wid, dim, st, "protect", "rule", feat, values, control, champ, c, sizes, ts,
                                 f"protect group {SHORT[feat]} {'/'.join(values)}: {dim} {champ} is worse there than "
                                 f"{control}, {fmt(c)}; that group goes back to {control}")
        best = None
        for opt in self.wf[wid]["options"][dim]:
            if opt == champ:
                continue
            for feat, rows in by.items():
                if [dim, opt, feat] in st["retired"]:
                    continue
                good = [v for v, r in rows.items() if (c := compare(r, opt, champ)) and c["lift"] > 0
                        and c["z"] >= SLICE_Z]
                if not good or len(good) == len(rows) or sum(sizes.get((feat, v), 0) for v in good) < MIN_GROUP_SHARE:
                    continue
                c = compare(pool(rows, good), opt, champ)
                if not (c and c["n"] >= SCREEN_N and c["z"] >= SCREEN_Z and c["lift"] >= MIN_LIFT):
                    continue
                out = compare(pool(rows, [v for v in rows if v not in good]), opt, champ)
                if out and difference_z(c, out) >= DIFFERENCE_Z and (best is None or c["z"] > best[3]["z"]):
                    best = (opt, feat, sorted(good), {**c, "outside_group": out,
                                                      "difference_z": round(difference_z(c, out), 2)})
        if not best:
            return []
        opt, feat, values, c = best
        return self._add(wid, dim, st, "winner", "trial", feat, values, opt, champ, c, sizes, ts,
                         f"group trial: {dim} {opt} looks better for {SHORT[feat]} {'/'.join(values)}, {fmt(c)} "
                         f"against {champ}; trying it on half of that group's calls to confirm on fresh data")

    def _add(self, wid, dim, st, kind, stage, feat, values, winner, control, c, sizes, ts, msg) -> list:
        share = sum(sizes.get((feat, v), 0) for v in values)
        st["rules"][dim] = {"kind": kind, "stage": stage, "feature": feat, "values": values, "winner": winner,
                            "control": control, "created": ts, "live_hour": None, "evidence": c,
                            "group_share": round(share, 3)}
        ids = [i for i, _, a in compile_cohorts(st["rules"]) if dim in a]
        st["ever"][dim] = sorted(set(st["ever"].get(dim, [])) | set(ids))
        self.store.log_decision(ts, wid, "group_trial" if stage == "trial" else "group_rule_added",
                                f"{msg} ({share:.0%} of calls)", st["rules"][dim])
        return [msg]

    # ---- 3-4: confirm a trial, watch a rule
    def _watch(self, wid: str, dim: str, st: dict, cfg: dict, h: int, ts: str) -> list:
        r = st["rules"][dim]
        ids = {i for i, _, a in compile_cohorts(st["rules"]) if dim in a}
        live = [c["id"] for c in cfg["cohorts"] if c["id"] in ids]
        if live and cfg["allocations"].get(live[0], {}).get(dim) != alloc(r):
            live = []
        if r["live_hour"] is None:
            if live:
                r["live_hour"] = h
                self.store.log_decision(ts, wid, "group_rule_live", f"{dim} {r['stage']} for {who(r)} is live", r)
            return []
        rows = [x for x in self.store.arm_stats(wid, dim, since_hour=r["live_hour"]) if x[0][1] in ids]
        if r["stage"] == "trial":
            c = compare(rows, r["winner"], r["control"])
            enough = c and min(c["n"], c["n_ref"]) >= CONFIRM_N
            if enough and c["lift"] > 0 and c["z"] >= CONFIRM_Z:
                r.update(stage="rule", live_hour=None, confirmed=c)
                msg = f"group rule: {dim} {r['winner']} for {who(r)} confirmed on fresh data, {fmt(c)}; 80% from now"
                self.store.log_decision(ts, wid, "group_rule_added", msg, r)
                return [msg]
            if enough and c["lift"] < 0 and c["z"] <= TRIAL_FAIL_Z or h - r["live_hour"] >= TRIAL_MAX_HOURS:
                return self._drop(wid, dim, st, ts, c, "the trial did not confirm it")
            return []
        if h - r["live_hour"] < RULE_GRACE_HOURS:
            return []
        recent = [x for x in self.store.arm_stats(wid, dim, since_hour=max(r["live_hour"], h - 24)) if x[0][1] in ids]
        c = compare(recent, r["winner"], r["control"])
        if c and c["n_ref"] >= RULE_MIN_CONTROL and c["lift"] < 0 and c["z"] <= RULE_ROLLBACK_Z:
            return self._drop(wid, dim, st, ts, c, f"it fell behind {r['control']} in that group")
        return []

    def _drop(self, wid: str, dim: str, st: dict, ts: str, c, why: str) -> list:
        r = st["rules"].pop(dim)
        st["retired"].append([dim, r["winner"], r["feature"]])
        msg = f"remove group {r['stage']} {dim} {r['winner']} for {who(r)}: {why}" + (f", {fmt(c)}" if c else "")
        self.store.log_decision(ts, wid, "group_rule_removed", msg, c or {})
        return [msg]

    def _by_feature(self, wid: str, dim: str, excluded: set) -> dict:
        """{feature: {value: rows}} using only calls whose option came from the whole-workflow random split."""
        out = {}
        for (feat, val), rows in self.store.segment_stats(wid, dim).items():
            out.setdefault(feat, {})[val] = [r for r in rows if r[0][1] not in excluded]
        return out

    # ---- the proposal
    def _change(self, wid: str, now: int, st: dict, cfg: dict, want: list, have: list, notes: list):
        def edit(c):
            for cid, _, _ in have:
                c["allocations"].pop(cid, None)
            c["cohorts"] = [{"id": cid, "where": where} for cid, where, _ in want]
            for cid, _, a in want:
                c["allocations"][cid] = a

        expected = 0.0
        for dim, r in st["rules"].items():
            cur = cfg["allocations"]["default"].get(dim, {})
            expected += r["group_share"] * (SHARES[r["stage"]] - cur.get(r["winner"], 0)) * abs(r["evidence"]["lift"])
        wf = self.wf[wid]
        active = self.store.active_leads(wid, now, len(wf["cadence"]), self.d0)
        share = max([r["group_share"] for r in st["rules"].values()] or [0.1])
        groups = "; ".join(f"{cid} {a}" for cid, _, a in want) or "none (everyone follows the default)"
        return Change(
            "groups", "approval", edit,
            summary=f"Groups: {groups}",
            rationale="; ".join(notes) or "group rules changed",
            diff={"cohorts": {"from": [[i, w] for i, w, _ in have], "to": [[i, w] for i, w, _ in want]},
                  "group_allocations": {"from": {i: a for i, _, a in have}, "to": {i: a for i, _, a in want}}},
            evidence={"rules": st["rules"]},
            dry_run={"method": "the group's own calls from the random split, compared within the same config version",
                     "rules": {dim: {k: r.get(k) for k in ("kind", "stage", "feature", "values", "winner", "control",
                                                           "group_share", "evidence", "confirmed")}
                               for dim, r in st["rules"].items()},
                     "expected_engagement_change": round(expected, 4)},
            impact=min(0.8, 0.2 + 4 * expected),
            affected_leads=int(active * share))

    def _still_wanted(self, row: dict):
        change = row["body"]["diff"].get("group_allocations")
        if not change:
            return None
        st = self.store.meta(f"seg:{row['workflow_id']}") or {"rules": {}}
        want = {i: a for i, _, a in compile_cohorts(st["rules"])}
        if _norm_alloc(want) != _norm_alloc(change["to"]):
            return "groups: the approved group settings no longer match what the evidence says"
        return None


def _norm_alloc(m: dict) -> dict:
    return {i: {d: {o: round(x, 4) for o, x in a.items()} for d, a in al.items()} for i, al in m.items()}


def _norm(items: list) -> list:
    return [(i, {k: sorted(v) for k, v in w.items()}, _norm_alloc({i: al})[i]) for i, w, al in items]
