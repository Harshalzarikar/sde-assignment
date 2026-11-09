# Outcome-First Agent Loop — Submission

**Name:** Harshal Zarikar | **Date:** 2026-11-09 | **Hours actually spent (be honest):** TODO (Harshal to supply honest estimate) | **Language / storage:** Python 3.13 standard library + SQLite

---

## 0. TL;DR

I built a controller that ingests events, detects a persistently unhealthy caller ID, and repairs it through the governance boundary.
`public_01`: zero changes. `public_02`: one approved, beneficial `ks_cancel` caller-ID change (`D1` 0.875 / `D2` 0.125), `ALL` conclusion +4.4%, pickup +4.1%, engagement -0.1%.
I deliberately did not build welcome/voice experiments, hallucination repair, per-person call time, or a supervisor UI.

## 1. Assumptions

1. The simulator HTTP API is the only source of truth. The controller never reads `runs/`, `sim/`, `sim/_truth`, or `scenarios/*.yaml`.
2. One controller owns one SQLite database and one simulator.
3. Caller-ID health is the highest-value incident to automate first: it has a public degradation, a clear test, a policy-safe repair, and a graded outcome.
4. Pickup and engagement stay separate. Caller-ID repair is judged on pickup first, with the workflow guardrail checked separately.
5. Approver hours, latency, TTL, apply windows and request caps come from the workflow policy, not per-client hard-coding.
6. A proposal outside approver hours is queued if the next opening is more than 30 minutes away.
7. Detection is population-based: 2-hour windows, three consecutive windows, 100 dials in the caller and peer pools, 72-hour baseline, 1% two-sided alpha, 8pp minimum drop.
8. The production target is 4 vCPU / 8 GB Linux. SQLite is an assignment choice; production storage is Postgres.
9. No LLM or validation calls were made, so public runs used zero credits.

## 2. Scope decisions

| # | Capability | Built / Partial / Cut | Why | To finish |
|---|---|---|---|---|
| C1 | Ingest at load scale | Built | Resumable, idempotent, restartable | Stream very large JSON chunks |
| C2 | Shared cohort object | Cut | Not used by the caller-ID loop | One cohort object across detection, experiments, supervisor view |
| C3 | Bootstrap from history | Built | History preloaded before live decisions | Retention and compaction |
| C4 | Call-time optimisation | Cut | Large surface, little public evidence | Per-person timing model plus validation |
| C5 | Caller-ID health | Built | Clear detector, repair and public incident | Multi-ID degradation and alerting |
| C6 | Welcome/voice experiments | Cut | Could not defend sample sizes and interactions in time | Cohort assignment, sample sizing, multiple-comparison control |
| C7 | Winner promotion, decay, rollback | Cut | Depends on C6 | Champion/challenger monitoring and rollback triggers |
| C8 | Hallucination response | Cut | No LLM/validation path built | Detect, suggest, validate, propose, verify |
| C9 | Governance client | Partial | Lifecycle, queueing, TTL, stale re-proposal, auto-apply checks work | Batch multiple changes; client-specific fatigue rules |
| C10 | Impact, affected leads, dry run | Partial | Fields exist, but are not honest enough to rank | Historical impact and real replay |
| C11 | Supervisor view | Cut | No UI/API surface built | One consolidated read model |
| C12 | Queryable audit store | Built | SQLite `audit` + `proposals` answer why/who/what-next | Retention and export |
| C13 | Crash recovery | Built | Kill/restart test passes with identical final state | Automate in CI |
| C14 | Loop observability | Partial | Logs, hourly summaries, load report | Metrics endpoint and paging |
| C15 | Fair credit/approver allocation | Cut | No credits spent; one workflow incident | Tenant budgets and queue prioritisation |

Principle: one incident loop done end-to-end under governance, load and crash tests, instead of many half-built features.

## 3. Stakeholders

### 01 — Nikhil Rao, VP Sales

**Accepted:** one outcome headline per client; fast auto-apply where policy permits; proposals carry summary, diff, affected count, impact and rationale.

**Pushed back:** I refused a blended score; pickup and engagement are separate KPIs. Kestrel's auto-apply is not "everything": `ks_cancel` allows only `schedule`, `caller_id`, `welcome_message` and `voice`, with `max_shift` 0.2; `prompt_version` and `cohorts` are not allowed. Northwind KYC auto-apply is disabled (`[]`, `max_shift` 0), not enabled.

**Wrong against policy:** "Kestrel has turned on auto-apply for everything" and "Northwind are fine with auto-apply on KYC" are both false.

**Reply:** Nikhil — I can give you one headline per client, but not a blended pickup/engagement score; those are separate KPIs and blending them would mislead the room. On Kestrel, auto-apply is limited to schedule, caller ID, welcome message and voice, with a 0.2 max shift; prompt version and cohorts still need approval. Northwind's KYC policy has auto-apply disabled, so I cannot push KYC winners straight through. I also will not roll the persuasive prompt across clients without per-client validation.

### 02 — Meera Iyer, Head of Compliance, Northwind Life

**Accepted:** no customer-facing change without a named Northwind approval; script changes need validation evidence; every change is auditable to variant, approval and timestamp; operational settings stay inside the signed policy. The memo's approver hours match the API: 10:00–19:00, KYC 10:00–18:00.

**Pushed back:** the memo assumes a weekly summary exists; it does not. I did not make every operational setting require a named human approval, because caller-ID rotation and timing are delegated by policy. KYC auto-apply stays disabled. Requests are queued outside approver hours rather than sent at night.

**Wrong against policy:** the weekly summary is not a built product surface.

**Reply:** Meera — accepted: no customer-facing change without a named Northwind approval; script changes carry validation evidence; every change is auditable to variant, approval and timestamp, and I can answer an auditor query from my store. Caller-ID rotation and timing stay inside the signed policy, and KYC auto-apply remains disabled. I have not built the weekly summary yet; that is a gap. Requests are queued outside your approver hours rather than sent at night.

### 03 — Kabir Menon, Platform & SRE lead

**Accepted:** kill/restart safety, idempotency, actionable alerts, no new server to operate, one-day load target, visibility into ingest lag and decisions.

**Pushed back:** I used embedded SQLite, not Postgres. It is a single file with no server to operate, but I would not run it as production HA storage. I do not have a status command or paging integration, so I will not claim one. The load test covered one simulated day, not all five.

**Wrong against policy:** no factual claim was contradicted; the production-infrastructure concern was correct.

**Reply:** Kabir — the kill/restart test is in `reports/crash_recovery.md`: kills mid-ingest, after intent and after the API call, all exit 137, then the run finishes with identical cursors, counts, proposal digest and applied versions, no duplicate IDs, and the pending approval intact. I used SQLite because it is a single embedded file with no server to operate, but I would not run it as production HA storage; the first move is Postgres on a persistent volume. I do not have a status command or paging integration yet.

### 04 — Anita Desai, Operations Supervisor, Kestrel Finance

**Accepted:** I do not undo her edits. Supervisor changes are audited, the caller-ID baseline is reset, and proposals based on an older version are marked stale.

**Pushed back:** "two requests a day and never before 11:00" is stricter than the Kestrel policy the controller receives: `ks_cancel` allows 4 requests per 24 hours and approver hours 10:00–19:00. I did not hard-code her personal cap or start time. I also did not build the one-screen view or a separate pre-change notification beyond the approval request.

**Wrong against policy:** treating 2/day and 11:00 as platform limits would be wrong; the actual policy is 4/24h and 10:00–19:00.

**Reply:** Anita — I will not undo your edits. When you change config I audit it, reset my caller-ID baseline, and mark my pending proposals stale; I do not overwrite you. Two requests a day and nothing before 11:00 is stricter than the Kestrel policy my controller receives (4 requests/24h, approver hours 10:00–19:00), and I did not hard-code your personal cap. I also have not built the one-screen view or a pre-change notification beyond the approval request. Those are real gaps.

### 05 — Tara Kulkarni, Head of Product

**Accepted:** pickup and engagement stay separate; I do not blend them. Detection thresholds are explicit: 2-hour windows, three consecutive windows, 100 dials per caller and peer pool, 72-hour baseline, 1% alpha, 8pp minimum drop. Proposals carry a diff, affected dials, a dry-run payload and an impact score.

**Pushed back:** I did not build cohorts, experiments or a shared cohort object, so there is no shared ID to fragment. The impact score is a constant 0.08 and the dry run is evidence metadata, not a historical replay; I would not rank a queue on it yet. Per-person call time is cut. I verified that expired proposals are marked expired, but not reliably re-queued with fresh data: the deterministic ID can collide with the expired row and return it instead of sending a fresh request.

**Wrong against policy:** no factual contradiction; the gap is that her product requirements are not all implemented.

**Reply:** Tara — pickup and engagement stay separate everywhere; I do not blend them. I did not build cohorts or welcome/voice experiments, so there is no shared cohort object yet; the caller-ID detector uses 2-hour windows, 3 consecutive windows, at least 100 dials per caller and peer pool, and a 72-hour baseline. Proposals carry a diff, affected dials, a dry-run payload and an impact score, but the impact score is currently a constant 0.08 and the dry run is evidence, not a historical replay. Expired proposals are marked expired, but not reliably re-queued with fresh data. Per-person call time is cut.

### 06 — Aryamann, CTO

**Accepted:** outcomes over unexplainable lift; spend compute only where it changes a decision; design for the scale envelope; choose and say no clearly; write for the engineer on call.

**Pushed back:** I did not claim production ownership. I state the gaps plainly. The implemented loop spends zero credits because it makes no LLM or validation calls.

**Wrong against policy:** none; his warning that other memos may be wrong was correct, especially on Kestrel and Northwind auto-apply.

**Reply:** Aryamann — I chose one incident loop and made it survive governance, load and crashes rather than sketch fifteen features. The measurable result is one beneficial caller-ID repair (+11.0% ks_cancel conclusion, +9.5% pickup) and a zero-change run on public_01. I did not build experiments, hallucination repair, per-person call time or the supervisor screen. The audit store answers why/who/what-next from SQLite, and I have written the gaps and production path in the submission.

## 4. Architecture

```
simulator HTTP (/sim/state, /events/chunks, /workflows, /config, /approvals)
        |
        v
controller.main tick (15 simulated minutes)
  advance -> ingest -> supervisor edits -> detect -> governance
      |          |             |              |            |
      v          v             v              v            v
  SQLite events  audit/meta   caller health  POST /approvals
    counts       proposals    state machine  POST /apply, PUT /config
```

State lives in one SQLite file: `cursor`, `events`, `counts`, `proposals`, `audit`, `meta`; WAL on, `synchronous=NORMAL`. On restart the controller loads cursors, preloads missing chunks, processes unseen supervisor edits, reconciles non-terminal proposals, and resumes. Event rows use `event_id` as primary key with `INSERT OR IGNORE`; the cursor advances only in the final 10k-row transaction. A simulator reset is detected when `last_event_id` or chunk count moves backwards; the old DB is archived and a fresh one starts with a `sim_reset` audit row.

```bash
./run.sh <SIM_URL>
python -m controller.main <SIM_URL> --db controller.db
python scripts/run_scenario.py scenarios/public_02_incidents.yaml --name public_02
python scripts/load_test.py scenarios/public_03_load.yaml --work-root <dir> --stop-after-minutes 1440
python scripts/crash_test.py scenarios/public_02_incidents.yaml
python -m pytest -q
```

## 5. The learning loop

- **Cohorts:** not built. The controller does not define cohorts or split traffic. It uses the simulator's `cohort_id` only when writing counts. The one population test uses 100 dials per caller and peer pool per 2-hour window, three windows, enough to avoid a single-call trigger and detect a sustained drop within a day. A real cohort system needs one canonical object and a minimum arm size to avoid fragmenting small workflows.
- **Experiment design:** cut. No welcome × voice allocation, exploration/exploitation split across variants, or interaction handling. The only exploration is a 5% caller-ID share (higher if needed to keep 100 dials per window) after a caller is flagged.
- **Deciding a winner:** not a winner selector. The caller-ID detector uses a two-proportion z-test against healthy peers and the 72-hour baseline, requiring three consecutive 2-hour windows, `p < 0.01`, and an 8pp drop in both. False-winner rate is not measured; 1% is nominal per window, and three windows are a guard, not a family-wise guarantee.
- **Decay and rollback:** no champion/challenger monitor. Recovery exists: after a 24-hour cooldown and three windows within 3pp of baseline and peers, raise the caller by one 10pp step until the original share. It is slow by design; public_02 shows the flagged share near 0.12 through D9, so no completed recovery is evidenced.
- **Call time and caller ID:** history feeds the 72-hour baseline and counts; caller-ID allocation is per cohort, not per person. Per-person call time is cut. History is preloaded: 187,438 events in `public_01`, 5,537,379 in the load run.
- **Guardrail KPI:** before a repair or recovery step, `_guardrail_gate` estimates the expected change against the workflow's `*_max_drop` (0.02 for all three public workflows). If the expected drop is too large, the step is blocked and audited. This is pre-change only; there is no separate post-change guardrail monitor.

## 6. Governance

```
intent -> queued -> pending -> approved -> applied
                    \-> rejected
pending -> expired (TTL elapsed)
approved -> lapsed (apply_by missed)
intent/queued/pending/approved -> stale (base_version superseded)
stale -> replan on current base with the same intended dimensions, or skip
auto-apply: intent -> applied / blocked
```

Proposal IDs are `sha256(workflow | base_version | purpose | canonical proposed_config)`, so a replay with the same material returns the same ID. A `409 duplicate_proposal_id` reconciles to an existing approval if one is listed; otherwise it audits `proposal_duplicate_unmatched` and leaves the row for review. One proposal per change; I did not batch five changes into one request. A proposal is queued when approver hours are closed and the next opening is more than 30 minutes away. Before sending, the controller computes the latest plausible decision time from maximum approver latency and rejects the send if it would miss the TTL. Every tick reconciles the approval list; approvals apply only before `apply_by`; elapsed TTL becomes expired. The 24-hour request cap comes from policy: `ks_cancel` gets 4/24h, not Anita's 2/day.

Auto-apply is used only when policy enables it and every changed dimension is allowed and within `max_shift` under total-variation distance. The detector tries auto-apply first but routes a large reduction through approval when the shift exceeds the cap. In public_02 the `ks_cancel` shift 0.5→0.125 was 0.375, above 0.2, so it correctly used approval.

Impact score, affected leads and dry run are present but not honest enough for ranking. `impact_score` is the constant detector `min_drop` (0.08), not data-derived. `affected_leads` is recent dials, not unique leads. `dry_run` is the detector evidence payload, not a replay against historical cohorts. I would not sort a supervisor queue by this score yet.

Supervisor edits are audited. A caller-ID edit resets that workflow's detector baseline and starts a cooldown. Non-terminal proposals older than the edit are marked stale; if the edit touched the same intended dimension, re-planning is skipped because the evidence no longer holds; if unrelated, the proposal is merged onto the current config and re-sent. The controller never silently overwrites a supervisor edit.

## 7. Incidents

Detection: 2-hour windows, three consecutive windows, at least 100 dials for the flagged caller and 100 for healthy peers in every window, a 72-hour baseline, two-sided alpha 0.01, and an 8pp minimum drop versus both peers and baseline. The nominal false-positive rate is 1% per window per caller; the three-window rule makes a sustained false trigger less likely, but I did not measure the family-wise rate.

Fix: reduce the flagged caller to the exploration share (5%, raised if needed to keep minimum dials), redistribute the removed weight pro rata to other caller IDs, and check the workflow guardrail before applying. Public_02 moved `ks_cancel` from `D1` 0.5 / `D2` 0.5 to `D1` 0.875 / `D2` 0.125. Credit spend was 0.

Post-apply verification: after a 24-hour cooldown, recovery requires three windows within 3pp of baseline and peers, then raises the caller 10pp per step. A completed recovery is not evidenced in public_02.

Timeline from `public_02`:

- D2 10:00: `did_flagged` on `ks_cancel`.
- D2 16:00: controller detects `caller_id_degraded` for `D2`.
- D2 17:00: `apr_00001` approved and applied; `ks_cancel` v2, `caller_id` only.
- D2 flagged share 0.371; D3 0.122; D4–D9 about 0.12–0.13.
- D3 11:30: supervisor edits `ks_cancel` `prompt_version` v2→v3; grade reports `mttr_minutes: null`, `fixed_by: null`; not repaired.
- D5 15:00: supervisor edits `nw_renewal` `voice` v1→v2; not reverted.
- Hallucinated calls: 416 baseline → 475. I cannot attribute the increase solely to the controller because the supervisor edits happened in the same run.

## 8. Scale

Measured on `public_03_load`, one simulated day of five (`--stop-after-minutes 1440`), fresh simulator and DB:

- Simulator startup 231.5 s; history preload 5,537,379 events in 600.2 s (9,226 events/s).
- Controller wall time including preload 732.6 s. Steady-state: 96 ticks in 131.8 s (2.20 min), so the one-day 10-minute target was met.
- Live events 840,012; ingest mean 10,697 events/s, peak 35,619 events/s.
- Mean tick 1,372.86 ms; slowest 7,835.89 ms.
- Ingest lag: max 900 s, final 480 s. This is the normal age of the newest event after a 15-minute advance, not extra backlog.
- Peak controller working set 1,683.7 MB (max RSS in the controller process tree, not the simulator).
- Final DB 3,801.8 MB; WAL/SHM 0.0 MB.

Ingest is dominant at 804.4 ms/tick mean. At 10× the measured live rate, linear extrapolation puts ingest near 8,044 ms/tick; DB and disk growth are next. That is an extrapolation, not a measured 10× run. First fixes: stream JSON chunks, keep 10k-row transactions, and partition ingestion by workflow/tenant. The load run did not finish the five-day scenario.

## 9. Reliability

`scripts/crash_test.py` ran `public_02_incidents` and hard-killed the controller with `os._exit(137)` at `mid_ingest`, `after_intent_before_api`, and `after_api_before_record`. The simulator stayed up. The final restart ran to completion. Results: no duplicate proposal IDs, no duplicate applied versions, one applied proposal, the pending approval was found after the crash, and final cursors, counts, proposal digest and applied-version histories were identical. Overall **IDENTICAL**; no state lost.

Idempotency: `event_id` primary key with `INSERT OR IGNORE`; cursor and final batch commit together; deterministic proposal IDs and upserts; applying is driven by simulator approval status, not a local "sent" flag. Replay re-reads the same chunks and reconciles the same approval.

Connection failures retry three times with backoff. `409 stale_base_version` triggers one re-plan; a second stale stops. `409 duplicate_proposal_id` reconciles to an existing approval if listed, otherwise audits `proposal_duplicate_unmatched` and leaves the row for review. `404/410` marks lapsed. `403` on auto-apply is blocked. Other simulator errors are audited; governance errors can propagate and exit, with restart reconciliation as recovery. On host power loss, `synchronous=NORMAL` can lose the last transaction, but cursor and rows commit together, so replay is safe.

## 10. Audit & observability

Real query against the controller database from a fresh `public_02` run stopped after minute 4000:

```sql
SELECT id, sim_ts, workflow_id, kind, reason
FROM audit
WHERE workflow_id = 'ks_cancel' AND sim_ts >= '2027-01-06T15:45:00+05:30'
ORDER BY id;
```

```text
10 | 2027-01-06T16:00:00+05:30 | ks_cancel | caller_id_degraded | caller D2 pickup stayed below healthy peers and baseline
11 | 2027-01-06T16:00:00+05:30 | ks_cancel | proposal_intent | intent recorded before API call
12 | 2027-01-06T16:00:00+05:30 | ks_cancel | proposal_pending | approval request returned pending
13 | 2027-01-06T16:00:00+05:30 | ks_cancel | caller_id_fix_proposed | caller D2 pickup is persistently below healthy peers and its own baseline
18 | 2027-01-06T17:00:00+05:30 | ks_cancel | proposal_approved | approval apr_00001 is approved
19 | 2027-01-06T17:00:00+05:30 | ks_cancel | proposal_applied | approved config applied
```

The proposal row is `prop_5236d...`, `applied`, base version 1, approval `apr_00001`, created 16:00, updated 17:00. The simulator's `config_changed` event at 17:00 has `actor: controller_approved`, `approval_id: apr_00001`, `changed_dims: ['caller_id']`, version 2. In the same probe, caller counts after 17:00 were `D1` 612 dials / 154 connected and `D2` 88 dials / 9 connected.

**Proposed paging rules (not implemented).** Nothing in the codebase pages anyone. I would page on: simulator unreachable, repeated controller crashes, ingest lag beyond two ticks, pending approval within 15 minutes of `apply_by`, repeated 5xx, and a simulator reset. I would not page on: a single-call pickup blip, a daily "no action" check, a rejected/expired proposal, or an unrelated supervisor edit.

## 11. Results

`public_01` — zero changes:

| workflow | conclusion | Δ | pickup | Δ | engagement | Δ | hallucinated (base) |
|---|---|---|---|---|---|---|---|
| ks_cancel | 0.333 | +0.0% | 0.238 | +0.0% | 0.491 | +0.0% | 47 (47) |
| nw_kyc | 0.364 | +0.0% | 0.242 | +0.0% | 0.591 | +0.0% | 21 (21) |
| nw_renewal | 0.420 | +0.0% | 0.263 | +0.0% | 0.546 | +0.0% | 42 (42) |
| ALL | 0.368 | +0.0% | 0.248 | +0.0% | 0.530 | +0.0% | 110 (110) |

`public_02` — one beneficial change:

| workflow | conclusion | Δ | pickup | Δ | engagement | Δ | hallucinated (base) |
|---|---|---|---|---|---|---|---|
| ks_cancel | 0.275 | +11.0% | 0.201 | +9.5% | 0.477 | +0.2% | 362 (330) |
| nw_kyc | 0.378 | +0.9% | 0.244 | +0.8% | 0.592 | -0.0% | 34 (31) |
| nw_renewal | 0.432 | +1.2% | 0.264 | +0.8% | 0.550 | +0.3% | 79 (55) |
| ALL | 0.349 | +4.4% | 0.231 | +4.1% | 0.529 | -0.1% | 475 (416) |

The caller-ID fix helped `ks_cancel` pickup and conclusion; ALL pickup/conclusion moved with it. It did not improve ALL engagement (-0.1%) and did not repair the supervisor prompt/voice edits; hallucinated calls rose 416 → 475. The `nw_renewal` increase (55 → 79) is in the workflow with the D5 voice edit, consistent with an unrepaired supervisor change. I would add hallucination repair and judge caller-ID repairs against an engagement guardrail, not pickup alone.

## 12. Trade-offs & rejected alternatives

| Option | Why considered | Why rejected / what instead |
|---|---|---|
| Postgres/Redis | SRE already runs them; production durability | SQLite embedded file for the assignment; Postgres is the first production change |
| Auto-apply every allowed change | Sales wants speed | Auto-apply only inside policy; larger shifts go through approval |
| Blend pickup and engagement | Easy summit number | Rejected; separate KPIs everywhere |
| Welcome/voice A/B engine | Large potential lift | Cut; could not defend sample sizes, interactions and multiple comparisons in time |
| LLM prompt repair | Public hallucination incident | Cut; credits finite and validation path not built |
| Per-person call time | Product headline | Cut; cohort-level caller-ID work first |
| One request with five changes | Supervisor fatigue | Not built; one proposal per change, with policy caps |
| Hash-based proposal IDs | Crash/restart idempotency | Kept, but expired re-queue can collide; needs a generation suffix |

## 13. Known weaknesses

- If both caller IDs degrade together, there is no healthy peer pool; no action and no alert.
- `nw_kyc` has a single caller ID; the detector logs "no alternative caller_id to compare" and does nothing.
- Hallucination rates before/after supervisor edits are unverified. Reports/JSON expose only whole-run totals per workflow, not pre/post or flagged/connected breakdowns.
- Supervisor prompt and voice edits are not repaired or reverted.
- Expired proposals are marked expired, but the deterministic ID can return the expired row instead of re-queueing with fresh data.
- `impact_score` is constant 0.08; `affected_leads` is recent dials, not unique leads; `dry_run` is evidence metadata.
- No cohorts, experiments, winner promotion, decay rollback, per-person call time, supervisor view, metrics endpoint or paging.
- False-positive rate is nominal, not measured. The load run covered one day of five; at 10× ingest is the first extrapolated bottleneck.
- SQLite is not production HA storage; 3.8 GB/day and 1.68 GB peak memory need retention, compaction and a Postgres plan.
- ALL engagement is -0.1%, so the one successful change is not a broad engagement win.

## 14. If this went to production at Formi next month

1. Move the store to Postgres on a persistent volume, with migrations, retention and an archive policy.
2. Add metrics and paging for ingest lag, tick duration, crash loops, pending approvals near `apply_by`, and world resets.
3. Harden error handling and add a status/health command that reads the same store.
4. Build hallucination detection with LLM suggestion, validation evidence, approval packaging and post-apply verification.
5. Add the cohort/experiment engine: one cohort object, minimum sample sizes, interaction handling, guardrail monitoring, decay and rollback.
6. Add the consolidated supervisor view and pre-change notification, then per-person call time.
7. Run the full five-day load scenario and a real 10× partition test across workflows and tenants.
8. Write the on-call runbook and add the crash test to CI.
