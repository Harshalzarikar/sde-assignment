"""Script (prompt) safety: notice when the robot starts saying false things, find a safe script, ask to switch.

The rules, in order:
  1. Baseline = the hallucination-flag rate of the script that ran during history (the business default).
  2. When the script changes to one with no good live record here, run a small validation (300 test calls).
  3. An incident opens when
       - that validation is below the policy's minimum pass rate, or
       - live flags under a changed script are clearly above baseline (>= 1.8x, and a chance below 1 in 10,000
         of seeing that many flags if the script were as safe as baseline), or
       - live flags spike under the long-running baseline script AND a validation confirms it
         (with no change to explain it, a spike alone may be bad luck, so it is checked first).
  4. Fix = the most recent script with a good live record here (proven on safety and engagement), validated with
     500 test calls and proposed with that evidence. Only when no such script exists do we ask the LLM for
     alternatives, validate each, and pick the best pass rate. The LLM suggests; validation and rules decide.
  5. Supervisor edits are never undone silently: the fix goes to the approver with the evidence.
  6. After the fix is live, flags must come back to normal before the incident is closed.
"""
import copy
import math

from .clock import label, minute_of
from .governance import OPEN

VALIDATE_NEW_N = 300
VALIDATE_FIX_N = 500
SPIKE_RATIO = 1.8
SPIKE_P_CHANGED = 1e-4
SPIKE_P_UNCHANGED = 1e-5
MIN_LIVE_CONNECTED = 200
LIVE_WINDOW_HOURS = 12
GOOD_RECORD_CONNECTED = 500
GOOD_RATIO = 1.3
VERIFY_CONNECTED = 300
VERIFY_RATIO = 1.5
CREDIT_RESERVE = 20
MUTE_HOURS = 12


def validation_cost(n: int) -> int:
    return math.ceil(n / 100)


def poisson_tail(k: int, lam: float) -> float:
    """P(X >= k) for X ~ Poisson(lam): the chance of seeing k or more flags by bad luck alone."""
    if k <= 0:
        return 1.0
    term = total = math.exp(-lam)
    for i in range(1, k):
        term *= lam / i
        total += term
    return max(0.0, 1.0 - total)


def spike_p(connected: int, flags: int, base_rate: float):
    """Chance of this many flags if the script were as safe as baseline, or None if it isn't a clear spike."""
    if connected < MIN_LIVE_CONNECTED or base_rate <= 0 or flags / connected < SPIKE_RATIO * base_rate:
        return None
    return poisson_tail(flags, connected * base_rate)


class PromptManager:
    def __init__(self, api, store, gov, workflows: dict, d0):
        self.api = api
        self.store = store
        self.gov = gov
        self.wf = workflows
        self.d0 = d0

    def plan(self, wid: str, now: int, ts: str):
        key = f"prompt:{wid}"
        version, cfg = self.gov.current(wid)
        prompt = cfg["prompt_version"]
        h = now // 60
        st = self.store.meta(key) or self._initial_state(wid, prompt, h, now, ts)

        if prompt != st["live"]["prompt"]:
            who = self._last_prompt_change(wid)
            st["live"] = {"prompt": prompt, "since_hour": h, "since_minute": now, "validation": None}
            self.store.log_decision(ts, wid, "prompt_changed",
                                    f"script changed to {prompt} by {who.get('actor', 'unknown')}", who)
            if not self._good_record(wid, prompt, st) and self._can_spend(validation_cost(VALIDATE_NEW_N)):
                st["live"]["validation"] = self._validate(wid, prompt, VALIDATE_NEW_N, ts,
                                                          "new script with no live safety record here")
            self.store.set_meta(key, st)

        if st["incident"] is None:
            st["incident"] = self._detect(wid, prompt, st, h, now, ts)
        inc = st["incident"]
        if inc is None:
            pass
        elif prompt == inc["bad_prompt"]:
            self._drive_fix(wid, now, ts, cfg, version, st)
        else:
            self._verify(wid, prompt, st, h, now, ts)
        self.store.set_meta(key, st)

    # ---- 1-3: baseline and detection
    def _initial_state(self, wid: str, prompt: str, h: int, now: int, ts: str) -> dict:
        hist = self.store.prompt_stats(wid, history=True)
        base_prompt = max(hist, key=lambda p: hist[p]["connected"]) if hist else prompt
        b = hist.get(base_prompt, {"connected": 0, "flags": 0})
        rate = b["flags"] / b["connected"] if b["connected"] else 0.0
        self.store.log_decision(ts, wid, "prompt_baseline",
                                f"baseline script {base_prompt}: hallucination flags on {rate:.2%} of "
                                f"{b['connected']:,} answered history calls", {"prompt": base_prompt, "rate": rate})
        return {"baseline_prompt": base_prompt, "baseline_rate": rate, "muted_until_hour": -10 ** 9,
                "live": {"prompt": prompt, "since_hour": h, "since_minute": now, "validation": None},
                "incident": None}

    def _detect(self, wid: str, prompt: str, st: dict, h: int, now: int, ts: str):
        live = st["live"]
        min_pass = self._min_pass(wid)
        val = live.get("validation")
        if val and val["pass_rate"] < min_pass:
            return self._open(st, wid, prompt, now, ts, "validation failed",
                              {"validation": val, "min_pass_rate": min_pass})
        since = max(live["since_hour"], h - LIVE_WINDOW_HOURS)
        s = self.store.prompt_stats(wid, since_hour=since).get(prompt)
        if not s:
            return None
        p = spike_p(s["connected"], s["flags"], st["baseline_rate"])
        changed = prompt != st["baseline_prompt"]
        if p is None or p >= (SPIKE_P_CHANGED if changed else SPIKE_P_UNCHANGED):
            return None
        ev = {"flag_rate": round(s["flags"] / s["connected"], 4), "connected": s["connected"],
              "flags": s["flags"], "baseline_rate": round(st["baseline_rate"], 4), "chance_by_luck": p,
              "since": label(since * 60)}
        if changed:
            return self._open(st, wid, prompt, now, ts, "live flags spiked after a script change", ev)
        if h < st["muted_until_hour"] or not self._can_spend(validation_cost(VALIDATE_FIX_N), reserve=0):
            return None
        val = self._validate(wid, prompt, VALIDATE_FIX_N, ts, "confirm a flag spike on the long-running script")
        if val and val["pass_rate"] < min_pass:
            return self._open(st, wid, prompt, now, ts, "live spike confirmed by validation", {**ev, "validation": val})
        st["muted_until_hour"] = h + MUTE_HOURS
        self.store.log_decision(ts, wid, "prompt_false_alarm",
                                f"flag spike on {prompt} ({ev['flag_rate']:.2%}) not confirmed by validation; "
                                f"ignoring spikes for {MUTE_HOURS}h", {**ev, "validation": val})
        return None

    def _open(self, st, wid, prompt, now, ts, reason, evidence) -> dict:
        self.store.log_decision(ts, wid, "incident_opened",
                                f"script {prompt} is unsafe ({reason}); looking for a fix", evidence)
        return {"bad_prompt": prompt, "opened_minute": now, "reason": reason, "evidence": evidence,
                "fix": None, "proposal_id": None, "fixed_minute": None}

    # ---- 4-5: fix and propose
    def _drive_fix(self, wid: str, now: int, ts: str, cfg: dict, version: int, st: dict):
        inc = st["incident"]
        if inc["proposal_id"]:
            status = self.store.proposal(inc["proposal_id"])["status"]
            if status in OPEN or status == "applied":
                return
            inc["proposal_id"] = None
            if status == "failed":
                inc["fix"] = None
        if inc["fix"] is None:
            inc["fix"] = self._choose_fix(wid, inc, st, ts)
            if inc["fix"] is None:
                return
        if not self.gov.can_request(wid, now, urgent=True)[0]:
            return
        inc["proposal_id"] = self._propose(wid, now, ts, cfg, version, st)

    def _choose_fix(self, wid: str, inc: dict, st: dict, ts: str):
        bad, min_pass = inc["bad_prompt"], self._min_pass(wid)
        known = [p for p in self._known_good(wid, st) if p != bad]
        if known and self._can_spend(validation_cost(VALIDATE_FIX_N), reserve=0):
            val = self._validate(wid, known[0], VALIDATE_FIX_N, ts,
                                 f"candidate fix for {bad}: the last script with a good live record")
            if val and val["pass_rate"] >= min_pass:
                self.store.log_decision(ts, wid, "incident_fix_chosen",
                                        f"fix: switch back to {known[0]} (good live record, validation "
                                        f"{val['pass_rate']:.1%} >= {min_pass:.0%}); no LLM call needed",
                                        {"prompt": known[0], "validation": val})
                return {"prompt": known[0], "validation": val, "source": "last known-good script"}
        if not self._can_spend(3, reserve=0):
            self.store.log_decision(ts, wid, "alert", "no credits left to look for a script fix", {})
            return None
        problem = (f"Script {bad} produces hallucinations: flagged on "
                   f"{inc['evidence'].get('flag_rate', 0):.1%} of answered calls vs {st['baseline_rate']:.1%} usual.")
        status, resp = self.api.post("/llm/suggest-fixes", {"workflow_id": wid, "problem": problem})
        if status != 200:
            self.store.log_decision(ts, wid, "alert", f"LLM suggest-fixes failed: {status}", {"response": resp})
            return None
        self.store.log_decision(ts, wid, "llm_suggestions",
                                f"LLM suggested {[s['prompt_version'] for s in resp['suggestions']]} "
                                f"(credits left {resp['credits_remaining']})", resp)
        best = None
        for s in resp["suggestions"]:
            p = s["prompt_version"]
            if p == bad or not self._can_spend(validation_cost(VALIDATE_FIX_N), reserve=0):
                continue
            val = self._validate(wid, p, VALIDATE_FIX_N, ts, "LLM-suggested candidate fix")
            if val and val["pass_rate"] >= min_pass and (best is None or val["pass_rate"] > best["validation"]["pass_rate"]):
                best = {"prompt": p, "validation": val, "source": "LLM suggestion, best validation pass rate"}
        if best:
            self.store.log_decision(ts, wid, "incident_fix_chosen",
                                    f"fix: {best['prompt']} (validation {best['validation']['pass_rate']:.1%}, chosen "
                                    f"by validation among LLM suggestions)", best)
        else:
            self.store.log_decision(ts, wid, "alert", "no candidate script passed validation", {})
        return best

    def _propose(self, wid: str, now: int, ts: str, cfg: dict, version: int, st: dict) -> str:
        inc, fix = st["incident"], st["incident"]["fix"]
        bad = inc["bad_prompt"]
        live = self.store.prompt_stats(wid, since_hour=st["live"]["since_hour"]).get(bad, {"connected": 0, "flags": 0})
        fix_record = self.store.prompt_stats(wid).get(fix["prompt"])
        who = self._last_prompt_change(wid)
        new = copy.deepcopy(cfg)
        new["prompt_version"] = fix["prompt"]
        bad_rate = live["flags"] / live["connected"] if live["connected"] else None
        changed_by = (f" The current script was set by the {who['actor']} at {who['ts'][:16]}; we are asking rather "
                      f"than changing it back ourselves." if who.get("actor") == "supervisor" else "")
        return self.gov.request(
            wid, now, ts, version, new, urgent=True,
            summary=f"Script: switch {bad} -> {fix['prompt']}: the current script is saying false things "
                    f"({'flags on ' + format(bad_rate, '.1%') + ' of answered calls' if bad_rate is not None else inc['reason']})",
            diff={"prompt_version": {"from": bad, "to": fix["prompt"]}},
            affected_leads=self.store.active_leads(wid, now, len(self.wf[wid]["cadence"]), self.d0),
            dry_run={"method": "live hallucination flags on the current script + validation runs",
                     "current_script": {"prompt": bad, "answered_calls": live["connected"],
                                        "flag_rate": bad_rate, "since": label(st["live"]["since_minute"])},
                     "baseline_flag_rate": round(st["baseline_rate"], 4),
                     "fix_validation": {k: fix["validation"][k] for k in ("validation_run_id", "n", "pass_rate")},
                     "fix_live_record": fix_record and {
                         "answered_calls": fix_record["connected"],
                         "flag_rate": round(fix_record["flags"] / max(fix_record["connected"], 1), 4),
                         "engagement": round(fix_record["engaged"] / max(fix_record["connected"], 1), 4)}},
            impact=0.95,
            rationale=(f"Incident: {inc['reason']}. Fix: {fix['source']}, validated at "
                       f"{fix['validation']['pass_rate']:.1%} (policy minimum {self._min_pass(wid):.0%})." + changed_by),
            evidence={"validation_run_id": fix["validation"]["validation_run_id"]})

    # ---- 6: verify
    def _verify(self, wid: str, prompt: str, st: dict, h: int, now: int, ts: str):
        inc = st["incident"]
        if inc["fixed_minute"] is None:
            who = self._last_prompt_change(wid)
            inc["fixed_minute"], inc["fixed_by"] = now, who.get("actor", "unknown")
            self.store.log_decision(ts, wid, "incident_fix_live",
                                    f"script is now {prompt} (by {inc['fixed_by']}), "
                                    f"{(now - inc['opened_minute']) / 60:.1f}h after the incident opened; verifying", who)
        s = self.store.prompt_stats(wid, since_hour=inc["fixed_minute"] // 60).get(prompt)
        if not s or s["connected"] < VERIFY_CONNECTED:
            return
        rate = s["flags"] / s["connected"]
        if rate <= VERIFY_RATIO * st["baseline_rate"]:
            self.store.log_decision(ts, wid, "incident_resolved",
                                    f"flags back to {rate:.2%} on {prompt} (baseline {st['baseline_rate']:.2%}) after "
                                    f"{s['connected']} answered calls; incident closed",
                                    {**inc, "verified_rate": rate})
        else:
            self.store.log_decision(ts, wid, "incident_not_fixed",
                                    f"flags still {rate:.2%} on {prompt}; treating {prompt} as a new suspect", inc)
            st["live"]["since_hour"] = inc["fixed_minute"] // 60
        st["incident"] = None

    # ---- helpers
    def _known_good(self, wid: str, st: dict) -> list:
        stats = self.store.prompt_stats(wid)
        good = [p for p, s in stats.items() if s["connected"] >= GOOD_RECORD_CONNECTED
                and s["flags"] / s["connected"] <= GOOD_RATIO * st["baseline_rate"]]
        return sorted(good, key=lambda p: -stats[p]["last_hour"])

    def _good_record(self, wid: str, prompt: str, st: dict) -> bool:
        return prompt in self._known_good(wid, st)

    def _min_pass(self, wid: str) -> float:
        return self.wf[wid]["governance"].get("min_validation_pass_rate", 0.97)

    def _credits(self) -> float:
        return self.api.get_ok("/sim/state")["credits_remaining"]

    def _can_spend(self, cost: int, reserve: int = CREDIT_RESERVE) -> bool:
        """Optional checks keep a reserve of credits for fixing incidents; fixes themselves use reserve=0."""
        return self._credits() - cost >= reserve

    def _validate(self, wid: str, prompt: str, n: int, ts: str, why: str):
        status, run = self.api.post("/validation/runs", {"workflow_id": wid, "prompt_version": prompt, "n": n})
        if status != 200:
            self.store.log_decision(ts, wid, "alert", f"validation of {prompt} failed: {status}", {"response": run})
            return None
        self.store.log_decision(ts, wid, "validation_run",
                                f"{prompt}: {run['pass_rate']:.1%} of {n} test calls passed ({why}); "
                                f"credits left {run['credits_remaining']}", run)
        return run

    def _last_prompt_change(self, wid: str) -> dict:
        """Who last changed the script, from the simulator's config history (events can lag by a tick)."""
        for v in reversed(self.api.get_ok(f"/workflows/{wid}/config/history")):
            if "prompt_version" in v["changed_dims"]:
                return {"actor": v["actor"], "ts": v["applied_at"], "version": v["version"],
                        "minute": minute_of(v["applied_at"], self.d0), "approval_id": v["approval_id"]}
        return {}
