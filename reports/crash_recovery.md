# Crash recovery report — public_02_incidents

## Commands

```
python scripts/crash_test.py C:\Users\Asus\Documents\wellfound_assignment\scenarios\public_02_incidents.yaml
python -m sim serve --scenario C:\Users\Asus\Documents\wellfound_assignment\scenarios\public_02_incidents.yaml --run-dir <run-dir> --port <free-port>
python -m controller.main http://127.0.0.1:<port> --db <CONTROLLER_DB>
CONTROLLER_CRASH_AT=<point> python -m controller.main ...
```

Artifacts: `C:\Users\Asus\Documents\wellfound_assignment\runs\crash_20261108T071138Z`

## Crash cycles

| # | Crash point | Controller exit |
|---|---|---|
| 1 | `mid_ingest` | 137 |
| 2 | `after_intent_before_api` | 137 |
| 3 | `after_api_before_record` | 137 |

Each controller process was hard-killed with `os._exit(137)`; the simulator process stayed up.
The final restart had no crash hook and ran until the scenario finished.

## Invariants

- Duplicate proposal IDs: `none`
- Duplicate applied versions: `none`
- Applied proposals: `1`
- Applied approval IDs present in simulator: `True`
- Pending approval found immediately after crash (c): `['prop_5236d76e248790dd948866b312a6f24e51c56939ce9e0a5f4cd7e8108812d304']`
- Final simulator approvals for in-flight proposal: `['prop_5236d76e248790dd948866b312a6f24e51c56939ce9e0a5f4cd7e8108812d304']`

## Final state comparison

| Field | Uninterrupted | Crash/restart | Identical |
|---|---|---|---|
| Cursors | `{'events': 490, 'history': 0}` | `{'events': 490, 'history': 0}` | yes |
| Counts rows / dials / connected / engaged | `[3803, 980128, 243996, 130856]` | `[3803, 980128, 243996, 130856]` | yes |
| Proposals (count, digest) | `[1, '02f32ba916c8']` | `[1, '02f32ba916c8']` | yes |
| Applied version histories | `3154f9103f9c` | `3154f9103f9c` | yes |
| Current config digest | `1908032cd159` | `1908032cd159` | yes |

Overall: **IDENTICAL**

No duplicate proposal IDs and no duplicate applied version were observed.
