# Notes: public_01_dev (observed via HTTP only)

Snapshot taken at `2027-01-04T00:00:00+05:30` (sim minute 0 of 11520), with 200.0 credits remaining.
The scenario is `public_01_dev`; `finished` is false. One history chunk exists (chunk 0, 187,438 events
through minute 0); the events chunk list is empty.

## (a) Governance and objectives

| Workflow | Primary KPI | Guardrail | Approver hours | Approval limits | Auto-apply rules |
|---|---|---|---|---|---|
| `nw_renewal` | `engagement_rate` | `pickup_rate_max_drop` 0.02 | 10:00–19:00 | max 3 requests/24h; TTL 24h; apply within 2h; latency 30–240 min; reject rate 0.10; min validation pass 0.97 | enabled; `schedule`, `caller_id`; `max_shift` 0.5 |
| `nw_kyc` | `pickup_rate` | `engagement_rate_max_drop` 0.02 | 10:00–18:00 | max 2 requests/24h; TTL 24h; apply within 2h; latency 60–480 min; reject rate 0.15; min validation pass 0.97 | disabled; allowed dims `[]`; `max_shift` 0 |
| `ks_cancel` | `engagement_rate` | `pickup_rate_max_drop` 0.02 | 10:00–19:00 | max 4 requests/24h; TTL 12h; apply within 2h; latency 20–120 min; reject rate 0.05; min validation pass 0.97 | enabled; `schedule`, `caller_id`, `welcome_message`, `voice`; `max_shift` 0.2 |

## (b) Where the real API differs from docs/SIMULATOR.md

- `objective.guardrail` is an object keyed by metric, e.g. `{"pickup_rate_max_drop": 0.02}` or
  `{"engagement_rate_max_drop": 0.02}`. The doc only says each workflow declares a `guardrail`; it does not
  name these keys.
- `GET /workflows/{id}/config` returns a wrapper (`workflow_id`, `version`, `actor`, `applied_at`, `config`),
  not a bare config object. The config itself is under `.config`.
- Config-history entries include `applied_at`; the `config_changed` event schema in the doc omits it.
- `governance.approver_hours` is a two-element list `[start, end]` (hours), and
  `governance.approver_latency_minutes` is a two-element `[min, max]` list. The doc describes them in prose
  without specifying these shapes.
- Workflow payloads include fields not shown in the endpoint summary/config example: `use_case`, `languages`,
  `min_gap_minutes`, `daily_batch.returning_share`, `prompt_catalog`, and `current_version`.
- `options` includes `prompt_version` alongside `welcome_message`, `voice`, and `caller_id`; the config
  example only shows the latter three being allocated.
- `GET /sim/state` returns more than `credits_remaining`: `scenario`, `sim_minute`, `ts`, `end_minute`,
  `finished`, `tick_minutes`, `chunks`, and `last_event_id`.

## (c) Surprises

- History is substantial before D0: 187,438 events in history chunk 0, even though there are no events chunks
  yet. This is enough to bootstrap from the default config.
- All three workflows start at version 1 with `actor: business_default`, empty `cohorts`, empty
  `schedule.hour_weights`, and empty `schedule.lead_preferred_hour`.
- Initial allocations are narrow: `W1` and `V1` at weight 1.0 for every workflow. `nw_renewal` and
  `ks_cancel` split caller IDs 50/50 between `D1` and `D2`; `nw_kyc` uses `D1` at 1.0.
- `nw_kyc` is the only workflow with auto-apply disabled, and it also has the widest approver latency
  (60–480 min), the lowest request cap (2/24h), and the highest reject rate (0.15).
- `ks_cancel` has the broadest auto-apply dimensions (including welcome message and voice) but the tightest
  `max_shift` (0.2), shortest TTL (12h), lowest reject rate (0.05), and highest request cap (4/24h).
- `nw_renewal` allows auto-apply only for `schedule` and `caller_id`, but with the loosest `max_shift` (0.5).

## (d) Live approval round trip (fresh simulator, HTTP only)

Pending response after `POST /workflows/nw_renewal/approvals` (created_at is the actual send time):

```json
{
  "approval_id": "apr_00001",
  "base_version": 1,
  "changed_dims": ["caller_id"],
  "created_at": "2027-01-04T10:00:00+05:30",
  "expires_at": "2027-01-05T10:00:00+05:30",
  "proposal_id": "prop_live_probe",
  "status": "pending",
  "workflow_id": "nw_renewal"
}
```

Approved response after `GET /approvals/apr_00001`:

```json
{
  "approval_id": "apr_00001",
  "apply_by": "2027-01-04T14:27:00+05:30",
  "base_version": 1,
  "changed_dims": ["caller_id"],
  "created_at": "2027-01-04T10:00:00+05:30",
  "decided_at": "2027-01-04T12:27:00+05:30",
  "expires_at": "2027-01-05T10:00:00+05:30",
  "proposal_id": "prop_live_probe",
  "reasons": [],
  "status": "approved",
  "workflow_id": "nw_renewal"
}
```

Applied response after `POST /workflows/nw_renewal/approvals/apr_00001/apply`:

```json
{
  "changed_dims": ["caller_id"],
  "version": 2,
  "workflow_id": "nw_renewal"
}
```

The approval JSON omits `apply_by` once the approval is applied. Config version moved from 1 to 2,
and `changed_dims` is present on the pending response too.

## (e) Public scenario runs

`scripts/run_scenario.py <scenario> --name <name>` runs a fresh no-op baseline first, then a fresh simulator
subprocess for the controller, then grades both runs. The controller never reads the scenario or run
directories; it only receives the simulator URL. The script sets `PYTHONUTF8=1` so grade reports can write
non-ASCII characters on Windows.

| Scenario | Grade report | Controller changes | ALL conclusion | ALL pickup | ALL engagement | Hallucinated calls (base) |
|---|---|---|---|---|---|---|
| `public_01_dev` | `reports/public_01.md` | 0 | +0.0% | +0.0% | +0.0% | 110 (110) |
| `public_02_incidents` | `reports/public_02.md` | 1, labeled `beneficial` | +4.4% | +4.1% | -0.1% | 475 (416) |

`public_01_dev` had no incidents and the controller made no changes, so its deltas are exactly zero rather
than negative. `public_02_incidents` produced one approved change:

- `ks_cancel` v2 `caller_id`: move weight from degraded `D2` to healthy `D1` (`D1` 0.875 / `D2` 0.125).
  Grade label: **beneficial** (`did_health_delta` +0.361). Workflow outcome: conclusion +11.0%, pickup
  +9.5%, engagement +0.2%.

Incident timing in `public_02_incidents`:

- `did_flagged` on `ks_cancel` at D2 10:00. The grade report exposes the flagged DID share by day, not an
  MTTR: 0.371 on D2, 0.122 on D3, then about 0.12-0.13 through D9. Our audit log detected it at D2 16:00
  and the approved fix applied at D2 17:00, i.e. 7 hours after the D2 10:00 onset by our own clock.
- `supervisor_edit` on `ks_cancel` at D3 11:30 (`prompt_version` v2 -> v3): grade reports
  `mttr_minutes: null` and `fixed_by: null`. We did not repair prompt hallucination, so the supervisor edit
  remains unfixed.
- `supervisor_edit` on `nw_renewal` at D5 15:00 (`voice` v1 -> v2): no MTTR field, and we did not revert it.

The rise in hallucinated calls (416 baseline -> 475) is consistent with the supervisor prompt/voice edits
and is not something the current caller-health controller repairs. Overall engagement moved -0.1%, so the
caller-ID fix improved pickup/conclusion but did not improve the ALL engagement KPI.

Governance for `public_02_incidents`: 1 approval request, 1 approved, 1 applied via approval, 0 expired,
0 lapsed, 0 conflicts, and peak 1 request/24h for `ks_cancel`. `public_01_dev` made 0 requests.

Controller logs had no errors or tracebacks in either run. The only detector line was the expected
`[detect] nw_kyc: no alternative caller_id to compare` (the workflow has a single caller ID). SQLite audit
rows: `public_01_dev` 0, `public_02_incidents` 12.
