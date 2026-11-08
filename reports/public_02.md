# Grade report — public_02_incidents

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.275 | +11.0% | 0.201 | +9.5% | 0.477 | +0.2% | 0% | 362 (330) |
| nw_kyc | 0.378 | +0.9% | 0.244 | +0.8% | 0.592 | -0.0% | 0% | 34 (31) |
| nw_renewal | 0.432 | +1.2% | 0.264 | +0.8% | 0.550 | +0.3% | 5% | 79 (55) |
| ALL | 0.349 | +4.4% | 0.231 | +4.1% | 0.529 | -0.1% | 3% | 475 (416) |

## Changes applied by the controller

1 applied — beneficial: 1

- ks_cancel v2 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.361 → **beneficial**

## Governance

```
{
  "approval_requests": 1,
  "decisions": {
    "approved": 1,
    "expired": 0
  },
  "rejection_reasons": {},
  "applied_via_approval": 1,
  "auto_applied": 0,
  "auto_apply_rejected": 0,
  "apply_conflicts": 0,
  "lapsed": 0,
  "peak_requests_per_24h": {
    "ks_cancel": 1
  }
}
```

## Incidents

```
[
  {
    "type": "did_flagged",
    "workflow": "ks_cancel",
    "at": "D2 10:00",
    "flagged_did_share_by_day": {
      "2": 0.371,
      "3": 0.122,
      "4": 0.127,
      "5": 0.117,
      "6": 0.12,
      "7": 0.124,
      "8": 0.126,
      "9": 0.13
    }
  },
  {
    "type": "supervisor_edit",
    "workflow": "ks_cancel",
    "at": "D3 11:30",
    "mttr_minutes": null,
    "fixed_by": null
  },
  {
    "type": "supervisor_edit",
    "workflow": "nw_renewal",
    "at": "D5 15:00"
  }
]
```

## Decaying variants — share of in-population calls still on the variant, by day after onset

```
{}
```

Credits used: 0 · validation runs: 0 · LLM fix calls: 0