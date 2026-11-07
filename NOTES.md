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
