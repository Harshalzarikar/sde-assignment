# Outcome-First Agent Loop — Submission

**Name:** Harshal Zarikar · **Date:** 2026-11-09 · **Hours actually spent (be honest):** 15 hours ·
**Language / storage:** Python 3.10+ (only dependency: `httpx`, already in `requirements.txt`) / one SQLite file

---

## 0. TL;DR
- An always-on controller (`./run.sh <SIM_URL>`) that steps the simulator 15 minutes at a time. Each step it streams the new events into SQLite, then runs five managers: caller ID, script safety, greeting/voice experiments, group rules and a guardrail watch. All of them send changes through one governance client.
- **public_01: +12.3% leads concluded, public_02: +29.7%, public_03 (load): +20.4%. Zero harmful changes in all three.** The bad-prompt incident was fixed in 90 minutes with 8 credits and no LLM call. The spam-flagged caller ID was detected and removed 60 minutes after onset.
- In four worlds with every effect removed, it promoted **0 winners and 0 group rules** (4 group trials started; all 4 failed fresh confirmation and were removed).
- Kill/restart in the middle of a pending incident approval gives a byte-identical grade report. One simulated day of public_03 takes 13–15 s wall-clock including the simulator. Peak memory is 220 MB.
- Deliberately not built: call-time optimisation (C4), cross-tenant fairness (C15), a web UI (the supervisor view is a CLI) and the weekly compliance summary.

## 1. Assumptions
1. `*_max_drop` in a guardrail is **relative** to the pre-controller baseline (0.02 on 30% pickup means "not below 29.4%"). At these levels that is stricter than percentage points.
2. Greeting and voice are heard only after someone picks up, so they cannot move pickup. Experiments therefore run only on engagement-primary workflows. On pickup-primary (KYC) workflows they could only risk the engagement guardrail, so there the controller manages caller IDs only.
3. The history before D0 ran the business default config. It is the baseline for the hallucination-flag rate, the guardrail (last 3 history days: earlier days have unusually high pickup because caller-ID load builds up over time) and the scripts' "known-good" record.
4. Caller-ID pickup falls with load (slowly) and with a spam flag (suddenly). Health is never observed directly; it is inferred from pickup compared with the other numbers dialled in the same hours under the same config.
5. A validation pass rate measures **safety only**. It is never used as evidence about engagement.
6. Only calls that actually happened count. Options are compared only inside the same `(config_version, cohort_id)` stratum, where assignment was random. An arm that never ran has no rows, so it cannot enter learning.
7. A simulator restart is a new world. If the notebook belongs to a different run (`matches_world`), it is archived and a fresh one is started.
8. A Kestrel ops supervisor's preference (≤ 2 requests a day, none before 11:00) is stricter than her policy (4 a day, from 10:00). I honour it as our own choice. A **safety incident** may still use the policy's full limit and hours.
9. Routine requests always leave one slot of the daily limit free, so an incident never finds the budget exhausted.
10. A request is not sent if the approver's worst-case latency could push the decision past the TTL. It would only expire.
11. The controller manages only cohorts it created (`grp_*`). If anyone else has defined cohorts, the group manager leaves cohorts alone completely rather than risk overwriting a human's segmentation.
12. The 15-minute step is the decision cadence. Experiments are evaluated once per simulated hour; incidents and caller IDs every step.

## 2. Scope decisions
| # | Capability | Built / Partial / Cut | Why | What it would take to finish |
|---|---|---|---|---|
| C1 | Ingestion at load scale | **Built** | Everything else depends on it. Chunks are streamed line by line. Each chunk's counts and the "chunk N done" bookmark commit in one transaction. ~150–175k events/s | Shard by workflow beyond ~10× (§8) |
| C2 | Joins + one shared cohort object | **Built** | Lead features are joined at ingest. Group cohorts have one stable id from their definition (`grp_age_60plus`) in one registry, used by experiments, the supervisor view and the audit trail | Cohort-level *detection* (incidents are workflow-level today) |
| C3 | Bootstrapping from history | **Partial** | Used for the flag-rate baseline, the guardrail baseline, known-good scripts and established caller IDs. Not used to pre-rank greetings or voices: history ran a single option per dimension, so it holds no comparison | — |
| C4 | Call-time optimisation | **Cut** | An offline experiment on public_01 found ~2% pickup from hour weights, far less than caller-ID spreading (+25% on KYC). It also needs its own exposure accounting. I would rather do C5–C9 properly | ~2 days: per-cohort hour weights from history (pickup by hour × feature), shipped via the `schedule` auto-apply dimension in ≤ max_shift steps, measured with a holdout cohort; per-person preferred hour for returning people with ≥ 2 connects |
| C5 | Caller-ID health | **Built** | The biggest, most reliable lever (+25–30% pickup on KYC) | — |
| C6 | Greeting/voice experiments | **Built** | The core of the learning loop | Factorial interaction test (§5) |
| C7 | Winner promotion, decay, rollback | **Built** | A 20% control group stays after every promotion | Faster decay detection for options that were never promoted (§13) |
| C8 | Hallucination incident response | **Built** | Known-good script first; LLM only if none exists; deterministic validation decides; approval with evidence; flags verified after apply | The LLM path is implemented but not exercised by the public scenarios (0 LLM calls) |
| C9 | Governance client | **Built** | Without it nothing else is allowed to happen | — |
| C10 | Impact, affected leads, dry run | **Partial** | Every proposal carries all of them, computed from observed data, not a replay simulator. Impact score is a documented heuristic | Replay dry run against the historical cohort; calibrate impact score against measured lift |
| C11 | Supervisor view | **Partial** | `python -m controller supervisor` gives one screen per workflow: live config, what's being tested, what's winning, waiting on you, changed today. CLI, not web | Thin HTTP/HTML layer over the same function |
| C12 | Decision/audit store | **Built** | `python -m controller why --workflow X --at "D3 13:00"` (§10) | — |
| C13 | Crash recovery demonstrated | **Built** | Script, log and a pytest that kills the real controller | — |
| C14 | Loop observability | **Partial** | `python -m controller health`: per-day events, ingest/decide/simulator seconds, worst tick, decisions, changes, credits, peak RSS, alerts | Export to Prometheus; pager wiring |
| C15 | Cross-tenant fairness | **Cut** | Request limits and a 20-credit incident reserve are per workflow, but there is no cross-tenant policy. With 200–400 credits and 0–8 used, there was nothing to arbitrate | Per-tenant credit buckets; global approver-attention queue ranked by impact score |

**Principle:** build first what can cause a client incident if it is wrong (governance, crash safety, script safety, ingest correctness). Next build the levers with the largest measured effect (caller IDs, then greeting/voice). Cut anything whose effect I measured as small (call time) or that has nothing to arbitrate at this scale (fairness). Every built rule had to survive a no-effect world without false winners.

## 3. Stakeholders

### 01 — Nikhil Rao, VP Sales
- **Accepted:** speed where the policy allows it. Kestrel greeting/voice and Northwind renewal caller-ID changes auto-apply, in ≤ max_shift steps.
- **Declined:** "winning by lunch → live for everyone by evening". A lunchtime lead is usually noise: in the no-effect worlds, several options looked like winners early on and then disappeared. A winner needs ≥ 400 answered calls, z ≥ 3, ≥ +3% lift and still leading over the last 24h. I also declined a single blended score: Product (and the README) forbid blending pickup and engagement.
- **Wrong against the policy:** "Kestrel has auto-apply for everything". Kestrel's `allowed_dims` are schedule, caller_id, welcome_message and voice, with `max_shift 0.2`. Scripts and cohorts still need approval, and no change can move more than 20% of traffic at once. "Northwind is fine with auto-apply on KYC": `nw_kyc` has `auto_apply.enabled: false`, and Northwind's compliance head requires named human approval for anything a customer hears. "Roll out the persuasive prompt": P4's description ("freer objection handling, richer benefit explanations") is exactly what makes scripts hallucinate. The controller changes scripts only to fix a safety incident, never to chase engagement.
- **Reply:** *Nikhil — happy to give you real numbers: on our three test worlds the agent lifted leads concluded by 12–30% overall with zero harmful changes, and it fixed a bad script in 90 minutes. Two corrections so nobody gets surprised on stage: Kestrel's auto-apply covers greeting, voice and caller ID (20% steps), not scripts; Northwind KYC has no auto-apply at all and their compliance head wants a named approver. I won't promote "lunchtime winners": most of them vanish by evening. I also won't blend pickup and engagement into one score. I can give you one lift figure per client for leads concluded, which is the outcome they pay for.*

### 02 — Meera Iyer, Compliance, Northwind Life
- **Accepted, and where it shows:** every greeting, voice, script and cohort change on Northwind goes to the approver. The policy already forbids auto-apply on those dimensions; I don't rely on that, because group rules always go to approval. Script changes carry a `validation_run_id` and the pass rate in `evidence`. Requests are sent only inside 10:00–19:00 (KYC 10:00–18:00), and only if the approver's worst-case latency still fits before expiry. "Slow and right": a group rule needs a screen, a trial and confirmation on fresh data. A global winner that is clearly worse for one group triggers protection for that group.
- **Pushed back / partial:** the weekly summary of operational changes is not built. The data for it is in the store (every auto-applied caller-ID change has its reason), but there is no report command yet.
- **Wrong against the policy:** "call timing and caller-ID rotation are covered by the policy we signed" is true for `nw_renewal`, but **not for `nw_kyc`**, where auto-apply is disabled. On KYC, even caller-ID rotation goes to her approver, and the controller does that.
- **Reply:** *Meera — confirmed: nothing a Northwind customer hears changes without your approver, and script changes always carry a validation run (≥ 97% pass) in the request. We only send requests during your approvers' hours. One correction: the KYC workflow's policy doesn't allow automatic changes at all, so caller-ID rotation there also comes to you for approval; on renewals it's automatic as signed. For IRDAI: every call records the greeting, voice, script and config version it used, and our audit command answers "why was this live, who approved it, what happened next" in seconds. The weekly summary report isn't built yet; I'll send it manually until it is.*

### 03 — Kabir Menon, Platform & SRE
- **Accepted:** the kill/restart test is in `reports/crash_recovery_public_02.log` (killed mid-incident) and in `tests/test_controller.py` (a hard exit of the real controller, then a check of exact counts and that nothing was applied twice). Idempotency comes from deterministic proposal ids and a write-before-send proposal table (§9). Pages are only for things a human must act on (§10). Loop health is visible without reading logs: `python -m controller health`.
- **Pushed back:** "no new stateful infrastructure". I used **SQLite**: one embedded file, no server, so there is nothing to operate at 2 a.m. Backup is a file copy. Recovery is "restart the process" (it replays from the bookmark). The schema is plain SQL, so moving it into the existing Postgres is a driver swap, which I'd do before running more than one controller process.
- **Wrong against the memo:** nothing. One caveat: the < 10 min/day target was measured on a macOS laptop, not on 4 vCPU / 8 GB Linux.
- **Reply:** *Kabir — kill test done: killed mid-run while an incident approval was pending, restarted, identical final report, no double applies. One simulated day of public_03 (840k events) takes ~15 s wall including the simulator; the controller's own share is ~8 s, and peak memory is 220 MB. Measured on a laptop, not your 4 vCPU box, so expect some difference. State is one SQLite file: no server, restart is the recovery procedure, and the schema ports to our Postgres before we scale out. Pages: controller bug (config refused as invalid), out of credits mid-incident, unresolved incident, stuck ingest. Guardrail drifts are business-hours notes, not pages.*

### 04 — Anita Desai, Ops Supervisor, Kestrel Finance
- **Accepted:** her edits are never undone silently. A supervisor edit to greeting or voice makes her choice the champion, pauses that dimension for 48h, and after that changes to it go only through approval. A bad script she ships is replaced only through an approval that carries evidence. At most 2 requests a day, none before 11:00 (`PREFERENCES` in `controller/governance.py`). Each tick's changes are batched into one request. "One screen" is `python -m controller supervisor --workflow ks_cancel`.
- **Pushed back:** a safety incident (the agent saying false things) may use her policy's full limit and start at 10:00. I'd rather send a third request than leave a hallucinating script running until 11:00. "Tell me before anything changes": her company's policy auto-applies greeting, voice and caller-ID changes. She sees them on her screen under "changed today", but there is no channel to warn her *before*. Sending them all as approvals would break her 2-a-day limit.
- **Real risk she named:** the "compliance script word for word" fix. The known-good path picks a script with a good live record on **both** safety and engagement. The LLM fallback, however, picks by validation pass rate, which measures safety only. It could pick P5 ("near-verbatim compliance script"). Listed in §13.
- **Reply:** *Anita — we will never undo your script or voice changes ourselves. If a change you made turns out unsafe, you'll get one request with the evidence and you decide. Two requests a day, none before 11:00, everything batched into one. The exception: if a script starts making false claims, I'll ask from 10:00. Your screen shows what's being tested, what's winning, what we need from you and what changed today. Honest gap: your policy lets greeting/voice changes apply automatically in small steps, and today you see them after the fact, not before. If you want them as requests instead, say so and I'll switch.*

### 05 — Tara Kulkarni, Product
- **Accepted:** KPIs are never blended anywhere. There is one cohort object with one id. Every proposal has impact score, diff, affected leads and dry run. An expired or rejected proposal is never resubmitted: the next tick re-plans from fresh data and sends a new proposal id. Only calls that ran count (Assumption 6). A winner changes only its own workflow.
- **Minimum size and window (her open question):** a group must be ≥ 5% of calls. A screen needs ≥ 100 answered calls per side; confirmation needs ≥ 100 *fresh* calls per side at z ≥ 2.5. Whole-workflow winners need ≥ 400 answered calls and z ≥ 3, plus the last-24h window. **Cost:** a typical winner takes 1.5–3 days. **Benefit:** 0 false winners in 4 no-effect worlds (~30 workflow-dimension experiments, ~60 group screens per hour).
- **Pushed back:** three P0s are not P0. I built caller-ID health and the supervisor view. I cut per-person call time: I measured hour weights at ~2% pickup, against +25% from caller IDs. It is not a summit headline on the evidence I have.
- **Caveat she should know:** the impact score is a heuristic (0.2 + 4 × expected engagement change, capped at 0.8; incidents 0.9–0.95). It ranks sensibly but is not calibrated.
- **Reply:** *Tara — held to all your definitions: separate KPIs, one cohort id everywhere, all four proposal fields, expired = re-plan from fresh data, no learning from arms that never ran. Cohorts: ≥ 5% of calls and confirmation on fresh data. Whole-workflow winners: ≥ 400 answered calls at z ≥ 3. Cost: 1.5–3 days per winner. Benefit: zero false winners in four no-effect test worlds. I delivered caller-ID health and the supervisor view; I cut per-person call time, because I measured ~2% pickup from it versus +25% from caller IDs. The impact score is a heuristic today; calibrating it against measured lift is next.*

### 06 — Aryamann, CTO
- **Accepted:** explainability over size. Every change has a plain-language summary, evidence and an audit answer. Credits went where they change a decision: 8 credits for the whole incident scenario, 0 LLM calls, because a known-good script existed. Choices and cuts are written down (§2). Docs are written for whoever gets paged (§10, §14).
- **Where I disagreed with other memos:** listed above, each checked against `GET /workflows/{id}` policies, not recollection.
- **Reply:** *Aryamann — outcomes: +12%, +30% and +20% leads concluded on the three public worlds, zero harmful changes, a 90-minute incident fix for 8 credits, zero false winners in no-effect worlds. I said no to Sales on blended scores and lunchtime winners, to Product on per-person call time, and to Anita on waiting until 11:00 during a safety incident. Two memo claims contradicted the policies (Kestrel "auto-apply everything", Northwind KYC auto-apply). What breaks first at scale: lead storage and the single-writer store, then serial per-workflow decisions. The fix is sharding by workflow onto Postgres (§8).*

## 4. Architecture

```
                 POST /sim/advance (15 min)                     GET /workflows, /config, /approvals
 simulator ───────────────────────────────┐                ┌───────────────────────────────────────┐
     │  new_chunks                        │                │                                       │
     ▼                                    │                ▼                                       │
 Ingestor (controller/ingest.py)          │          Governance (controller/governance.py)         │
  stream NDJSON line by line              │           - Change objects from every manager           │
  join lead features (last 6 campaigns    │           - one auto-apply PUT + one batched approval   │
   per workflow in memory, all in SQLite) │             per workflow per tick                       │
  aggregate -> call_counts, seg_counts    │           - hours / TTL / limits / one in flight         │
  ONE transaction: counts + bookmark      │           - write proposal BEFORE http; reconcile        │
     │                                    │           - pre-apply checks (withdraw stale evidence)  │
     ▼                                    │                ▲          ▲          ▲          ▲         │
 ┌──────────────── SQLite notebook (state/controller.db) ──┼──────────┼──────────┼──────────┼──────┐ │
 │ meta (bookmark, manager state, cohort registry)  call_counts  seg_counts  leads  world_events   │ │
 │ proposals (status machine)   decisions (audit trail)   loop_metrics                             │ │
 └──────┬──────────────┬───────────────┬────────────────┬────────────────┬────────────────────────┘ │
        ▼              ▼               ▼                ▼                ▼                          │
  PromptManager   CallerIdManager  ExperimentManager  SegmentManager  GuardrailWatch                │
  (safety)        (pickup)         (greeting/voice)   (group rules)   (daily, alerts only)          │
        └──────────────┴───────────────┴────────────────┴───── Change ────────────────────────────┘
                                                                          │
 views.py: supervisor / why       report.py: status / health  ◄── read-only over the notebook
```

**Where state lives:** only in the SQLite file. The process holds caches only (current config within a tick, the lead-feature map), and both are rebuilt from the API and the notebook on start.
**On restart:** read `/sim/state`. Check the notebook belongs to this world. Catch up every chunk after the bookmark. `reconcile` proposals left in `sending`/`applying` by asking `/approvals` and `/config/history`. Run one decision pass. Continue the loop.

## 5. The learning loop
- **Cohorts.** A group is one value set of **one** feature (e.g. `age_band ∈ {46-60, 60+}`), roughly 13 candidates per workflow. One feature at a time keeps groups large enough to measure and the 20-cohort limit far away. There is at most one group rule per dimension. When a greeting rule and a voice rule overlap, a combined cohort goes first, because a lead only matches its first cohort. Same-feature overlaps are split into shared and one-rule-only value sets, so ids never repeat. That bug cost public_03 four rejected requests before it was fixed, and it now has a test. Minimum sizes are in §3 (Product). The whole-workflow experiment reads each group from data it already randomised, so screening costs no extra traffic, and only a confirmed group gets its own cohort.
- **Experiment design.** Each dimension has one champion and challengers. Every challenger starts at 8%, rises to 20% at (≥ 150 calls, z ≥ 1) and 35% at (≥ 300, z ≥ 2), but only if it also leads over the last 24h. Non-champion traffic is capped at 60%. Greeting and voice are sampled independently by the simulator, so the two dimensions form a factorial design: each is analysed marginally, which is valid while their effects don't interact. **Not done:** an explicit interaction test (W×V cells). I'd add it before letting both dimensions promote within the same day.
- **Deciding a winner.** I use a stratified two-proportion z-test. Within each `(config_version, cohort)` stratum, the arm-versus-champion difference is weighted by `n_a·n_c/(n_a+n_c)` (Cochran–Mantel–Haenszel style). This protects against Simpson's paradox when shares change over time, and a unit test covers exactly that case. Rules: loser at z ≤ −2.5 (≥ 150 calls); no meaningful gain when even lift + 2 SE < 3% (≥ 800 calls); winner at z ≥ 3, lift ≥ 3%, ≥ 400 calls and still ahead over the last 24h. **Multiple comparisons:** up to 3 challengers × 2 dimensions, checked hourly, is repeated looks. I control that with a strict z (3.0, one-sided p ≈ 0.0013), minimum sizes, and agreement in the recent window. **Measured false-winner rate:** 0 promotions in 4 no-effect worlds. Group screens look at ~60 comparisons an hour, so they are expected to find lucky groups: a screen only buys a 50/50 trial *inside* the group, never a rule. All 4 lucky screens in the no-effect load world failed fresh confirmation.
- **Decay and rollback.** After promotion, the old champion keeps 20% as a control. From 12h after go-live, the winner is compared with the control over the last 24h; at lift < 0 and z ≤ −2 (≥ 150 control calls) it is rolled back. A winner still awaiting approval is cancelled if it falls clearly behind, and an approved change is **withdrawn** before applying if the evidence no longer supports it. A challenger that climbed and then fell behind on its record is removed. On public_02 the decaying W3 was off by day 5; on public_01 it is not (§13).
- **Call time and caller ID.** History establishes the numbers already in use. New numbers go on trial at 5% of calls (where auto-apply exists) and must reach 80% of the established numbers' pickup within 300 dials. Trial numbers are judged only on calls made under the config version that introduced them, because `next_campaign_only` mixes old and new leads. A number in use is flagged when its last-hour pickup falls below 60% of the others with z ≤ −2.5 (≥ 100 recent dials); load changes slowly, a spam flag is sudden. Otherwise calls are spread evenly, which also avoids load degradation. Caller IDs are handled per workflow, not per cohort or per person. Call time: cut (§2).
- **Guardrail KPI.** Experiments run only where greeting/voice cannot move the guardrail (Assumption 2). Once a day, after dialling ends, each workflow's guardrail KPI over 2 days is compared with the last 3 history days. Below `max_drop` at z ≤ −3 raises an alert listing our changes from the last 48h that can move that KPI. Between −2 and −3 it is a non-paging "watch" note. The −3 bar was set after the −2 bar raised two false alerts in a no-effect world: day-to-day spread is wider than per-call noise. Nothing is reverted automatically, because a mix effect (more hard-to-reach people answering) can lower engagement with no change in the agent. Result: 0 alerts on all public runs.

## 6. Governance
```
            ┌─────────── 4xx (invalid: alert, never resent) ──► failed
 sending ───┤─── 409 stale_base_version ───────────────────► stale ──┐
   │        └─── 409 duplicate id (restart) ─► adopt server's state   │
   ▼                                                                  │
 pending ──► rejected / expired ──────────────────────────────────────┤  managers re-plan next tick from
   │                                                                  │  fresh data; new proposal id
   ▼                                                                  │  (hash of base version + config)
 approved ──► withdrawn (evidence moved on / champion changed) ───────┤
   │     └──► stale (config changed: e.g. supervisor edit) ───────────┤
   ▼     └──► lapsed (410, apply window missed) ──────────────────────┘
 applying ──► applied            (auto path: sending ──► applied | stale | failed)
```
- **Batching and timing:** one approval in flight per workflow. All managers' wishes in a tick are merged into one request. Requests go only within approver hours (Kestrel ≥ 11:00) and only if worst-case latency fits before the TTL. Routine requests leave one daily slot for incidents. Routine share tweaks wait ≥ 24h between requests; milestones (start, remove, promote, roll back) may go any time. Approved changes are applied on the next tick (we poll every 15 min; the apply window is 2h). Peak requests in 24h were 2 (policy 3) on public_01/02. 0 expired, 0 lapsed, 0 apply conflicts on all three public runs.
- **Auto-apply:** used for caller ID everywhere it is allowed, and for Kestrel greeting/voice, always stepping toward the target in moves ≤ `max_shift`. **Not used**, voluntarily: for any dimension a supervisor edited (it goes to approval from then on), for group cohorts (they change what a segment hears), and while an approval is pending, because the auto change would make it stale (unless urgent, e.g. a spam-flagged number).
- **Impact, affected leads, dry run:** expected engagement change = Σ(share moved × measured lift vs champion). Impact = min(0.8, 0.2 + 4 × |expected|); incidents 0.95, flagged caller ID 0.9. Affected leads = active leads (campaigns still inside their cadence) × the largest share moved, or the daily batch when changes apply to new campaigns only. The dry run is the **observed** per-option result from the random split, stratified, with method stated. It is honest about what it is, but it is not a replay simulation, and the impact score is not calibrated.
- **Supervisor edit mid-experiment** (public_02, `nw_renewal`, D5 15:00, voice → V2): the controller logged `experiment_paused` and made V2 the champion. Its pending voice proposal was rejected by the approver. It made no voice change for 48h, then sent a voice change through approval (V2 84%). Nothing was overwritten.

## 7. Incidents
- **Script safety.** Baseline = history flag rate. A changed script with no live record gets a 300-call validation (3 credits). An incident opens on validation < policy minimum, or on live flags ≥ 1.8× baseline with Poisson tail p < 1e-4 (≥ 200 answered calls). For the long-running baseline script, a spike must also be confirmed by validation (p < 1e-5 first), because with no change to explain it, bad luck is the likelier story. Expected false-positive rate per check is ≤ 1e-4 by construction. Observed: 0 false incidents across all 7 runs listed here, including 4 no-effect worlds.
- **Fix choice:** the most recent script with a good live record here (≥ 500 answered calls, flags ≤ 1.3× baseline, and engagement known), validated with 500 calls (5 credits). Only if none exists: ask the LLM (3 credits), validate each suggestion and take the best pass rate. Routine validations stop when 20 credits remain; only an open incident may spend that reserve.
- **Verify:** after the fix is live, flags over ≥ 300 answered calls must return within 1.5× baseline before the incident closes.
- **public_02 timeline (ks_cancel):**
  - D3 11:30: the supervisor ships P2.
  - D3 11:45 (first tick after the event): validation of P2 = 93.7% < 97%, incident opened. P1 (last known-good) validated at 99.0%, and the approval was sent in the same tick with both validation ids.
  - 12:15: controller killed in the crash test (§9).
  - 12:36: approved.
  - 13:00: applied. This is the first tick at which the simulator exposes the decision.
  - 16:00: flags at 1.60% vs baseline 1.52% on 313 answered calls; incident closed.
  - **MTTR 90 min, 8 credits, 0 LLM calls.**
- **public_02 caller ID (ks_cancel):** D2 flagged at 10:00. At 11:00 the controller logged "D2 pickup suddenly dropped to 6.4% vs 27.3% on the other numbers in the last hour (140 dials, z=−3.34)" and moved D2's calls away (urgent auto-apply). The flagged number's share was 5.9% on that day and 0% every day after.

## 8. Scale
Measured on public_03_load (12 workflows, ~840k events/day, all 5 days), on a macOS laptop (**not** the 4-vCPU/8-GB Linux box):

| day | events | wall s (total) | simulator s | ingest s | decide s | ingest events/s | worst tick s | peak RSS |
|---|---|---|---|---|---|---|---|---|
| D0 | 840,092 | 12.8 | 6.1 | 4.9 | 1.8 | 172,979 | 0.93 | 217 MB |
| D1 | 840,038 | 13.3 | 6.2 | 4.8 | 2.2 | 174,018 | 0.93 | 218 MB |
| D2 | 840,035 | 14.7 | 6.5 | 5.5 | 2.6 | 152,426 | 1.10 | 220 MB |
| D3 | 840,018 | 14.9 | 7.0 | 5.2 | 2.7 | 161,390 | 1.03 | 220 MB |
| D4 | 840,012 | 14.4 | 7.1 | 5.1 | 2.3 | 166,099 | 1.04 | 220 MB |

- **Decision lag:** each step's events are ingested and decided on before the next advance. Decisions lag the newest event by at most one 15-min tick plus ~1 s of compute (worst tick 1.1 s). Kabir's target is one simulated day in < 10 min. The controller's own share is ~8 s (ingest + decide), about 75× headroom.
- **Store after 12 days of events:** 321 MB. 2.30M lead rows, 14.8k `call_counts` and 12.6k `seg_counts` rows (~2.6k rows per live day: counts are aggregated, not per call), 104 proposals, ~470 decisions.
- **What breaks first at 10× (~8.4M events/day, ~1,200 workflows):** (1) **lead storage**: every lead is kept for audit, which would be ~3 GB per 5 days in one SQLite file. Then memory for the active-lead feature map (last 6 campaigns per workflow, ~1.1M leads today), heading to ~2 GB. (2) SQLite's single writer, with ingest and decisions in one process. (3) Decisions are serial across workflows, at ~0.2 s per workflow-day today: ~4 min per simulated day at 1,200 workflows. Still inside budget, but the next limit.
- **Partitioning:** hash `workflow_id` to N worker processes. Every manager, count table and proposal is already keyed by workflow, so there is no shared mutable state between workflows. Tenants stay whole on one shard so per-tenant budgets (C15) can be enforced locally. Use one ingest fan-out per chunk and Postgres (already run by SRE) as the store. Leads older than the cadence move to cold storage, keeping only `(lead_id, person_id, features)` for audit.

## 9. Reliability
- **Crash-recovery test** (`devtools/run_local.py --crash-at-minute 5055`, log in `reports/crash_recovery_public_02.log`): the controller hard-exits (`os._exit(137)`) at D3 12:15, after advancing but before reading the new events, while incident approval `apr_00005` is pending. The harness restarts it. It caught up 225 events, found the approval pending, applied it once at 13:00 and finished. **`reports/public_02_crash.md` is identical to `reports/public_02.md`** apart from the title: same KPIs, same 23 changes, same 90-min MTTR, and the count check matches the simulator's truth exactly. `tests/test_controller.py::test_crash_mid_run_then_restart_finishes_with_exact_counts` does the same on public_01 in pytest.
- **What is lost:** nothing persistent. A chunk is either fully counted with its bookmark or not at all (one transaction, unit-tested by forcing a mid-chunk failure). In-memory caches are rebuilt.
- **Idempotency:** proposal id = `workflow-v{base}-{path}-{sha1(config)[:8]}-{n}`, written as `sending` before the HTTP call. After a restart, `reconcile` resends it. The simulator's `duplicate_proposal_id` (409) tells us it already exists, and we adopt its state from `/approvals`. A proposal found in `applying` is resolved via `/config/history`, so nothing is applied twice. A unit test checks that the applied proposal ids are unique after the crash test.
- **4xx/5xx:** network errors and 5xx are retried with exponential backoff (8 tries, up to 10 s). A broken chunk stream restarts from the chunk's start, so a partial read can never be half-counted. A 4xx is an answer, not a failure. 409 stale leads to re-planning. 410 marks the proposal lapsed. 402 (credits) is avoided by budgeting, and is logged as an alert if hit. 422 `invalid_config` is a controller bug: it raises one alert, and the same config is never resent.

## 10. Audit & observability
Real output, from the store only (`python -m controller why --db state/public_02.db --workflow ks_cancel --at "D3 13:00"`):
```
ks_cancel: the config live at D3 13:00 is v10, applied D3 13:00 by controller_approved; it changed ['prompt_version'].
  WHAT: Script: switch P2 -> P1: the current script is saying false things (flags on 4.2% of answered calls)
  WHY:  Incident: validation failed. Fix: last known-good script, validated at 99.0% (policy minimum 97%). The current
        script was set by the supervisor at 2027-01-07T11:30; we are asking rather than changing it back ourselves.
  DIFF: {"prompt_version": {"from": "P2", "to": "P1"}}
  DRY RUN: {"method": "live hallucination flags on the current script + validation runs", ... "fix_validation": {"validation_run_id": "val_00002", "n": 500, ...
  IMPACT SCORE 0.95 | affected leads 4500
  PROPOSED D3 11:45 as ks_cancel-v9-approval-89809b8c-0 via approval
  APPROVED: apr_00005 by the client's approver (apr_00005 approved at 2027-01-07T12:36, apply by 2027-01-07T14:36)
  BEFORE (24h): pickup 25.9% of 4,800 dials, engagement 49.0% of 1,242 answered
  AFTER  (24h): pickup 26.2% of 4,800 dials, engagement 49.7% of 1,256 answered
  WHAT HAPPENED NEXT:
    2027-01-07T13:00 incident_fix_live: script is now P1 (by controller_approved), 1.2h after the incident opened; verifying
    2027-01-07T16:00 incident_resolved: flags back to 1.60% on P1 (baseline 1.52%) after 313 answered calls; incident closed
    next config change: v11 at D4 13:15 by controller_approved
```
Other commands: `supervisor --workflow X` (one screen), `status` (proposals, caller IDs, experiment timeline), `health` (§8), and `devtools/show_decisions.py db workflow [from] [to]` for raw decisions plus manager state.

- **Page on:** config refused as invalid (controller bug); out of credits during an open incident; incident fix not verified (flags still high after the fix); ingest not advancing / controller crash-looping; repeated 5xx from the simulator.
- **Don't page (business-hours queue / supervisor screen):** guardrail breach (z ≤ −3, a business decision, with suspects listed); guardrail "watch" notes; rejected or expired proposals (re-planned automatically); prompt false alarms (validation said no); caller-ID trial failures.

## 11. Results
**public_01** (no incidents)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.381 | +11.5% | 0.259 | +6.0% | 0.513 | +4.3% | 12% | 74 (57) |
| nw_kyc | 0.465 | +26.8% | 0.303 | +25.1% | 0.592 | -0.8% | 0% | 37 (21) |
| nw_renewal | 0.438 | +5.2% | 0.279 | +5.7% | 0.534 | -1.4% | 2% | 49 (54) |
| ALL | 0.418 | +12.3% | 0.274 | +9.4% | 0.537 | +1.5% | 5% | 160 (132) |

29 changes applied: 19 neutral, 10 beneficial, **0 harmful**. 15 approval requests, 0 expired/lapsed.

**public_02** (caller ID flagged, bad prompt, supervisor edit)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.368 | +56.4% | 0.254 | +39.9% | 0.496 | +6.1% | 37% | 74 (348) |
| nw_kyc | 0.488 | +30.6% | 0.308 | +31.3% | 0.596 | -2.0% | 0% | 40 (29) |
| nw_renewal | 0.474 | +9.4% | 0.288 | +10.0% | 0.547 | -1.3% | 12% | 50 (54) |
| ALL | 0.429 | +29.7% | 0.276 | +25.8% | 0.536 | +0.7% | 25% | 164 (431) |

23 changes applied: 16 neutral, 7 beneficial, **0 harmful**. 15 requests, 12 approved, 3 judgement-call rejections, 0 expired/lapsed. 8 credits.

**public_03** (load): conclusion **+20.4%**, pickup +12.1%, engagement +5.2%, 28% of engagement headroom. 96 changes, 57 neutral, 39 beneficial, **0 harmful**. 0 guardrail alerts.

**Interpretation.**
- **Where it helped:** pickup, mostly from spreading calls over more caller IDs (less load degradation), adding unused numbers through trials and dropping flagged ones. That gave +25–31% pickup on KYC. On engagement workflows: greeting/voice winners and group rules (e.g. voice V3 for 60+). On public_02, the 90-min script fix removed most of the hallucinated calls (164 vs 431 baseline).
- **Where it was weak:**
  - Engagement on `nw_renewal` and `nw_kyc` is 0.8–2.0% *below* baseline while pickup is +5–31%. This is the mix effect: more hard-to-reach people now answer, and they engage less. The workflow's guardrail is pickup, which rose, so no guardrail was breached.
  - `nw_renewal` captured only 2–12% of its engagement headroom. It is approval-only with a 3-a-day limit, its best greeting effect is Hindi-only, and W3 decays (§13).
- **Harmful:** none in the final runs. Earlier versions had harmful changes; each one changed the design and is in the commit history:
  - caller-ID trials judged across mixed config versions;
  - a group rule promoted on a lucky screen (led to the trial plus fresh-confirmation design);
  - a "recent drop" fade rule that removed genuinely good options (reverted; §12).
- **What I'd change next:** decay detection for options that were never promoted, and the interaction test.

## 12. Trade-offs & rejected alternatives
| Option | Why considered | Why rejected / what instead |
|---|---|---|
| Thompson sampling / bandit allocation | Faster exploitation | Its allocation drifts with the data, so tests of significance and clean audit answers ("why was V3 at 35%?") get hard. Instead: staged shares (8/20/35%) tied to stated z thresholds, readable by a compliance head |
| Pooled (unstratified) comparisons | Simpler, more data | Shares change between config versions, so Simpson's paradox. Instead: stratified by config version and cohort (unit-tested) |
| Promote group winners straight from the screen | Faster per-segment wins | ~60 comparisons/hour find lucky groups; it produced a fake winner in a no-effect world. Instead: screen → 50/50 in-group trial → confirm on fresh data |
| "Recent drop" fade rule (remove a climbed option when its last 24h fell clearly below its earlier lead) | Faster exit from decaying greetings (public_01 W3) | An option climbs *because* its early estimate is high, so the drop shows up even when nothing changed. It removed two good options (3 harmful changes). Reverted |
| Let the LLM pick the fix | Fewer moving parts | The README forbids it, and credits are expensive. Instead: known-good script first (0 LLM calls on public_02), LLM only to suggest, validation decides |
| Postgres/Redis now | SRE already runs them | No server needed for one process. SQLite with the same plain-SQL schema; migrate when sharding |
| Process every event into per-call rows | Maximum flexibility | 840k rows/day. Aggregated counts keyed by (workflow, day, hour, version, cohort, variant): ~2.6k rows/day, and every analysis reads from them |
| Guardrail alert at z ≤ −2 | Catch drops early | Two false alerts in one no-effect world; at hundreds of workflows it would page daily. z ≤ −3 alerts; −2…−3 is a note |
| Call-time optimisation (C4) | Product's summit headline | Measured ~2% pickup; caller IDs gave 10× that. Cut (§2) |

## 13. Known weaknesses
1. **Decay of an option that was never promoted is slow.** On public_01, `nw_renewal` W3 climbed to 30–35% on three strong early days, then decayed. Because its cumulative record stayed positive, it kept 26–41% of calls on days 3–7 after onset. Rollback is fast only for a promoted winner (control group). The attempted fix made things worse (§12). A sound fix is to compare only data collected *after* the option climbed (unbiased) with the champion over the same hours.
2. **The LLM fallback picks by validation pass rate, which measures safety only.** It could choose a "near-verbatim compliance script" (P5) that kills engagement: exactly Anita's complaint. It should prefer candidates with engagement evidence and watch engagement during verification. Not exercised on public scenarios (a known-good script existed).
3. **Multiple looks are controlled by strict thresholds, not a formal sequential test** (e.g. alpha spending). The no-effect worlds show 0 false winners, but 4 worlds is a small sample.
4. **No interaction test** between greeting and voice.
5. **Impact score is a heuristic, and the dry run is observed data, not a replay.** Product plans to rank the supervisor's queue on the impact score, so it should be calibrated first.
6. **Guardrail is checked daily** (it could take up to 2 days to alert), and it alerts rather than acts.
7. **Only one feature per group.** "Seniors in Tamil Nadu" (two features) cannot become a group.
8. **Kestrel's ≤ 2-requests/≥ 11:00 preference is hard-coded** in `governance.py` rather than in configuration.
9. **Load numbers come from a laptop, not the target box.** The simulator's own time is about half the wall time.
10. **Outcomes vary run to run.** Small code changes alter which approvals are rejected, and public_01 moved between +10.5% and +14.6% across the iterations in the commit history. The published numbers are from the final code, one run each.

## 14. If this went to production at Formi next month
1. **Days 1–2:** shadow mode on 2–3 real workflows. Ingest, analyse and write proposals to the store, but send nothing. Compare its counts with the dialler's own, and its "would-have-changed" list with what supervisors actually did.
2. **Days 3–4:** move the store to Postgres (same schema), add the weekly compliance summary (Meera), and put Kestrel's preferences in per-tenant configuration.
3. **Days 5–6:** wire the §10 page list to the on-call pager and export `loop_metrics` to Prometheus. Run a game day that kills the process during a pending approval in staging.
4. **Days 7–8:** turn on caller-ID management only (the largest, least customer-visible lever; policy-signed latitude). Watch pickup and spam-flag detections against the carrier's own reports.
5. **Days 9–10:** fix weakness 1 (post-climb decay test) and weakness 2 (engagement-aware LLM fallback). Then enable greeting/voice experiments on one consenting tenant, approval-only at first.
6. **Days 11–14:** calibrate the impact score against measured lift. Build the minimal web supervisor screen over `views.py`. Write the on-call runbook from §9–10. Then decide with Product whether call time (C4) earns its place, measured with a holdout rather than asserted.
