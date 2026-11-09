# Load report — public_03_load: 1 simulated day of 5

## Exact commands

```
python scripts/load_test.py C:\Users\Asus\Documents\wellfound_assignment\scenarios\public_03_load.yaml --work-root D:\opencode_wellfound_runs --stop-after-minutes 1440
python -m sim serve --scenario C:\Users\Asus\Documents\wellfound_assignment\scenarios\public_03_load.yaml --run-dir D:\opencode_wellfound_runs\load_20261109T052533Z\sim --port <free-port>
python -m controller.main http://127.0.0.1:<port> --db D:\opencode_wellfound_runs\load_20261109T052533Z\controller.db --stop-after-minutes 1440
```

No baseline run. Stopped early after 1440 simulated minutes. One fresh simulator and one fresh controller DB.

## Run shape

| Metric | Value |
|---|---|
| Simulator startup (health) | 231.5 s |
| Controller wall time, including preload | 732.6 s |
| History preload events | 5537379 |
| History preload wall time | 600.2 s |
| History preload rate | 9226 events/s |
| History preload share of controller wall | 81.9% |
| Steady-state ticks | 96 |
| Steady-state wall for ticks | 131.8 s (2.20 min) |
| 10-minute steady-state target | met |
| Live events ingested | 840012 |
| Live-event ticks | 49 |
| Idle ticks | 47 |
| Live-event ingest rate | mean 10697 events/s; peak 35619 events/s |
| Mean tick duration | 1372.86 ms |
| Slowest tick duration | 7835.89 ms |
| Max ingest lag (live-event ticks) | 900.0 s |
| Final ingest lag (live-event ticks) | 480.0 s |
| Peak controller working set | 1683.7 MB |
| Final DB file | 3801.8 MB |
| Final WAL | 0.0 MB |
| Final SHM | 0.0 MB |
| Final DB + WAL + SHM | 3801.8 MB |
| Simulated finish | minute 1440 of 7200 |

## Tick phase breakdown

Profiled ticks: 96 total, 47 idle (no events ingested).

| Phase | All ticks mean | Idle ticks mean |
|---|---|---|
| POST /sim/advance | 210.61 ms | 32.49 ms |
| Ingest | 804.37 ms | 28.10 ms |
| Detect | 327.77 ms | 330.18 ms |
| Governance | 0.06 ms | 0.05 ms |
| Supervisor edits | 0.04 ms | 0.05 ms |
| State reads | 29.44 ms | 29.28 ms |
| Other controller work | 0.57 ms | 0.34 ms |
| Full tick | 1372.86 ms | 420.49 ms |

## Chunk sizes

| Kind | Chunks | Largest chunk |
|---|---|---|
| history | 23 | 250000 events |
| events | 49 | n/a events |

Largest chunk overall: 250000 events, 124381.8 KB (history chunk 21).

## Supervisor scan fix

Before the fix, a 30-minute profile with the same 5,537,379-row history preload measured `supervisor edits` at 9,556.5 ms of a 9,954.5 ms idle tick. `EXPLAIN QUERY PLAN` showed `SCAN events` for `SELECT ... FROM events WHERE type = 'config_changed' ORDER BY event_id`; that query took 10.204 s even though it returned zero `config_changed` rows.

The fix adds `events(type, event_id)` and keeps the existing per-event seen markers. In the one-day run, `supervisor edits` averaged 0.0449 ms across all 96 ticks and 0.0548 ms on idle ticks.

## Measured analysis

Run did not finish the 5-day scenario; it stopped at simulated minute 1440 by --stop-after-minutes 1440.

Measured dominant phase is ingest at 804.4 ms/tick (controller phases excluding advance total 1161.7 ms/tick; POST /sim/advance is 210.6 ms/tick). History preload was 5,537,379 events in 600.2 s (9,226 events/s), separate from steady state. At 10x the measured live-event rate (mean 10,697 events/s and peak 35,619 events/s), linear extrapolation puts the first pressure on ingest: 804.4 ms/tick becomes roughly 8,044 ms/tick, and DB/disk growth becomes the next limit. This is an extrapolation, not a measured 10x run. I would change: keep 10k-row chunk transactions and, if chunk fetch memory is still high, stream the JSON chunk instead of materializing it. Steady-state wall time was 2.2 min for 96 ticks against the 10-minute target (met).

The worst live-event-tick lag was 900 s, exactly one 15-minute advance, and the final was 480 s. That is the normal age of the newest event relative to the post-advance simulated clock, not evidence of an extra hour of controller backlog; idle ticks were excluded from this metric.
