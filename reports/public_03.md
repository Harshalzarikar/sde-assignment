# Grade report — public_03_load

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| arav_cancel_2 | 0.392 | +16.3% | 0.260 | +6.9% | 0.534 | +7.6% | 47% | 404 (409) |
| arav_kyc_1 | 0.480 | +33.7% | 0.277 | +28.6% | 0.595 | -0.8% | 0% | 442 (352) |
| arav_renewal_0 | 0.465 | +14.7% | 0.281 | +6.4% | 0.585 | +6.9% | 37% | 471 (408) |
| kest_cancel_2 | 0.390 | +15.7% | 0.260 | +7.3% | 0.532 | +6.7% | 45% | 444 (418) |
| kest_kyc_1 | 0.482 | +35.4% | 0.279 | +30.4% | 0.595 | -0.3% | 0% | 488 (370) |
| kest_renewal_0 | 0.477 | +18.5% | 0.282 | +6.6% | 0.595 | +9.2% | 44% | 451 (403) |
| nort_cancel_2 | 0.394 | +16.1% | 0.262 | +7.2% | 0.536 | +7.5% | 49% | 427 (368) |
| nort_kyc_1 | 0.402 | +13.0% | 0.237 | +10.1% | 0.596 | +0.1% | 0% | 354 (318) |
| nort_renewal_0 | 0.474 | +17.0% | 0.283 | +7.0% | 0.594 | +8.4% | 47% | 486 (419) |
| tide_cancel_2 | 0.395 | +15.0% | 0.262 | +7.2% | 0.534 | +6.6% | 47% | 427 (401) |
| tide_kyc_1 | 0.479 | +34.5% | 0.278 | +29.3% | 0.594 | -0.7% | 0% | 443 (376) |
| tide_renewal_0 | 0.469 | +15.8% | 0.280 | +6.1% | 0.591 | +8.3% | 44% | 467 (425) |
| ALL | 0.441 | +20.4% | 0.270 | +12.1% | 0.574 | +5.2% | 28% | 5304 (4667) |

## Changes applied by the controller

96 applied — neutral: 57, beneficial: 39

- nort_renewal_0 v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=-0.010 → **neutral**
- nort_cancel_2 v2 (controller_auto) dims=['welcome_message', 'voice', 'caller_id'] engageΔ=+0.000 didΔ=+0.008 → **neutral**
- kest_renewal_0 v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=-0.010 → **neutral**
- kest_cancel_2 v2 (controller_auto) dims=['welcome_message', 'voice', 'caller_id'] engageΔ=+0.000 didΔ=+0.008 → **neutral**
- arav_renewal_0 v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=-0.010 → **neutral**
- arav_cancel_2 v2 (controller_auto) dims=['welcome_message', 'voice', 'caller_id'] engageΔ=+0.000 didΔ=+0.008 → **neutral**
- tide_renewal_0 v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=-0.010 → **neutral**
- tide_cancel_2 v2 (controller_auto) dims=['welcome_message', 'voice', 'caller_id'] engageΔ=+0.000 didΔ=+0.008 → **neutral**
- nort_cancel_2 v3 (controller_auto) dims=['voice'] engageΔ=+0.001 didΔ=+0.000 → **neutral**
- kest_cancel_2 v3 (controller_auto) dims=['voice'] engageΔ=+0.001 didΔ=+0.000 → **neutral**
- arav_cancel_2 v3 (controller_auto) dims=['voice'] engageΔ=+0.001 didΔ=+0.000 → **neutral**
- tide_cancel_2 v3 (controller_auto) dims=['voice'] engageΔ=+0.001 didΔ=+0.000 → **neutral**
- arav_renewal_0 v3 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.020 → **neutral**
- nort_renewal_0 v3 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.020 → **neutral**
- kest_renewal_0 v3 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.020 → **neutral**
- nort_cancel_2 v4 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.024 → **beneficial**
- kest_renewal_0 v4 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.034 → **beneficial**
- kest_cancel_2 v4 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.024 → **beneficial**
- arav_cancel_2 v4 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.024 → **beneficial**
- tide_renewal_0 v3 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.054 → **beneficial**
- tide_cancel_2 v4 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.024 → **beneficial**
- nort_cancel_2 v5 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.010 → **neutral**
- kest_cancel_2 v5 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.010 → **neutral**
- arav_cancel_2 v5 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.010 → **neutral**
- tide_cancel_2 v5 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.010 → **neutral**
- nort_renewal_0 v4 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.017 didΔ=+0.000 → **beneficial**
- nort_renewal_0 v5 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.035 → **beneficial**
- nort_cancel_2 v6 (controller_auto) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- tide_cancel_2 v6 (controller_auto) dims=['welcome_message'] engageΔ=+0.012 didΔ=+0.000 → **beneficial**
- nort_cancel_2 v7 (controller_auto) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- kest_cancel_2 v6 (controller_auto) dims=['welcome_message', 'voice'] engageΔ=+0.017 didΔ=+0.000 → **beneficial**
- kest_cancel_2 v7 (controller_auto) dims=['voice'] engageΔ=+0.002 didΔ=+0.000 → **neutral**
- kest_cancel_2 v8 (controller_auto) dims=['voice'] engageΔ=+0.011 didΔ=+0.000 → **beneficial**
- tide_cancel_2 v7 (controller_approved) dims=['cohorts', 'welcome_message'] engageΔ=+0.009 didΔ=+0.000 → **neutral**
- arav_renewal_0 v4 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.017 didΔ=+0.000 → **beneficial**
- arav_cancel_2 v6 (controller_approved) dims=['cohorts', 'welcome_message'] engageΔ=+0.007 didΔ=+0.000 → **neutral**
- nort_cancel_2 v8 (controller_auto) dims=['welcome_message'] engageΔ=+0.008 didΔ=+0.000 → **neutral**
- arav_renewal_0 v5 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.040 → **beneficial**
- arav_cancel_2 v7 (controller_auto) dims=['voice'] engageΔ=+0.010 didΔ=+0.000 → **beneficial**
- tide_kyc_1 v2 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.397 → **beneficial**
- kest_kyc_1 v2 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.398 → **beneficial**
- arav_cancel_2 v8 (controller_approved) dims=['voice'] engageΔ=+0.009 didΔ=+0.000 → **neutral**
- kest_renewal_0 v5 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.017 didΔ=+0.000 → **beneficial**
- arav_cancel_2 v9 (controller_approved) dims=['welcome_message'] engageΔ=-0.007 didΔ=+0.000 → **neutral**
- nort_cancel_2 v9 (controller_auto) dims=['voice'] engageΔ=+0.010 didΔ=+0.000 → **beneficial**
- kest_cancel_2 v9 (controller_auto) dims=['voice'] engageΔ=+0.001 didΔ=+0.000 → **neutral**
- arav_kyc_1 v2 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.428 → **beneficial**
- tide_renewal_0 v4 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.016 didΔ=+0.000 → **beneficial**
- nort_cancel_2 v10 (controller_auto) dims=['welcome_message'] engageΔ=+0.004 didΔ=+0.000 → **neutral**
- arav_cancel_2 v10 (controller_auto) dims=['voice'] engageΔ=+0.008 didΔ=+0.000 → **neutral**
- tide_cancel_2 v8 (controller_auto) dims=['voice'] engageΔ=+0.010 didΔ=+0.000 → **beneficial**
- nort_renewal_0 v6 (controller_approved) dims=['welcome_message'] engageΔ=+0.093 didΔ=+0.000 → **beneficial**
- kest_cancel_2 v10 (controller_auto) dims=['voice'] engageΔ=+0.017 didΔ=+0.000 → **beneficial**
- arav_cancel_2 v11 (controller_auto) dims=['welcome_message'] engageΔ=+0.004 didΔ=+0.000 → **neutral**
- kest_cancel_2 v11 (controller_auto) dims=['voice'] engageΔ=+0.014 didΔ=+0.000 → **beneficial**
- nort_cancel_2 v11 (controller_auto) dims=['voice'] engageΔ=+0.013 didΔ=+0.000 → **beneficial**
- kest_cancel_2 v12 (controller_auto) dims=['voice'] engageΔ=+0.006 didΔ=+0.000 → **neutral**
- arav_cancel_2 v12 (controller_auto) dims=['welcome_message', 'voice'] engageΔ=+0.019 didΔ=+0.000 → **beneficial**
- tide_cancel_2 v9 (controller_auto) dims=['welcome_message'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- arav_cancel_2 v13 (controller_auto) dims=['voice'] engageΔ=+0.005 didΔ=+0.000 → **neutral**
- nort_cancel_2 v12 (controller_auto) dims=['voice'] engageΔ=+0.017 didΔ=+0.000 → **beneficial**
- arav_cancel_2 v14 (controller_auto) dims=['voice'] engageΔ=+0.004 didΔ=+0.000 → **neutral**
- tide_cancel_2 v10 (controller_auto) dims=['voice'] engageΔ=+0.013 didΔ=+0.000 → **beneficial**
- nort_cancel_2 v13 (controller_auto) dims=['voice'] engageΔ=+0.014 didΔ=+0.000 → **beneficial**
- nort_cancel_2 v14 (controller_auto) dims=['voice'] engageΔ=+0.006 didΔ=+0.000 → **neutral**
- arav_cancel_2 v15 (controller_auto) dims=['voice'] engageΔ=+0.004 didΔ=+0.000 → **neutral**
- nort_cancel_2 v15 (controller_approved) dims=['cohorts', 'welcome_message'] engageΔ=+0.009 didΔ=+0.000 → **neutral**
- tide_renewal_0 v5 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.093 didΔ=+0.000 → **beneficial**
- arav_renewal_0 v6 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.100 didΔ=+0.000 → **beneficial**
- nort_renewal_0 v7 (controller_approved) dims=['welcome_message'] engageΔ=+0.007 didΔ=+0.000 → **neutral**
- tide_cancel_2 v11 (controller_approved) dims=['cohorts', 'welcome_message', 'voice'] engageΔ=+0.013 didΔ=+0.000 → **beneficial**
- kest_renewal_0 v6 (controller_approved) dims=['cohorts', 'welcome_message', 'voice'] engageΔ=+0.115 didΔ=+0.000 → **beneficial**
- arav_cancel_2 v16 (controller_approved) dims=['cohorts', 'voice'] engageΔ=+0.009 didΔ=+0.000 → **neutral**
- arav_renewal_0 v7 (controller_approved) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- tide_renewal_0 v6 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.007 didΔ=+0.000 → **neutral**
- tide_cancel_2 v12 (controller_approved) dims=['voice'] engageΔ=+0.025 didΔ=+0.000 → **beneficial**
- tide_cancel_2 v13 (controller_auto) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- tide_cancel_2 v14 (controller_auto) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- tide_cancel_2 v15 (controller_auto) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- arav_cancel_2 v17 (controller_auto) dims=['welcome_message'] engageΔ=+0.004 didΔ=+0.000 → **neutral**
- tide_cancel_2 v16 (controller_approved) dims=['cohorts', 'welcome_message', 'voice'] engageΔ=+0.002 didΔ=+0.000 → **neutral**
- nort_cancel_2 v16 (controller_approved) dims=['welcome_message'] engageΔ=+0.009 didΔ=+0.000 → **neutral**
- tide_renewal_0 v7 (controller_approved) dims=['cohorts', 'voice'] engageΔ=+0.015 didΔ=+0.000 → **beneficial**
- tide_cancel_2 v17 (controller_auto) dims=['voice'] engageΔ=+0.006 didΔ=+0.000 → **neutral**
- nort_renewal_0 v8 (controller_approved) dims=['cohorts', 'voice'] engageΔ=+0.013 didΔ=+0.000 → **beneficial**
- nort_kyc_1 v2 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.411 → **beneficial**
- kest_renewal_0 v7 (controller_approved) dims=['voice'] engageΔ=+0.011 didΔ=+0.000 → **beneficial**
- arav_cancel_2 v18 (controller_approved) dims=['cohorts', 'welcome_message'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- tide_renewal_0 v8 (controller_approved) dims=['voice'] engageΔ=+0.008 didΔ=+0.000 → **neutral**
- arav_cancel_2 v19 (controller_auto) dims=['welcome_message'] engageΔ=+0.006 didΔ=+0.000 → **neutral**
- arav_cancel_2 v20 (controller_auto) dims=['welcome_message'] engageΔ=+0.006 didΔ=+0.000 → **neutral**
- arav_cancel_2 v21 (controller_auto) dims=['welcome_message'] engageΔ=+0.002 didΔ=+0.000 → **neutral**
- nort_renewal_0 v9 (controller_approved) dims=['voice'] engageΔ=+0.008 didΔ=+0.000 → **neutral**
- arav_cancel_2 v22 (controller_approved) dims=['cohorts', 'welcome_message'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- kest_cancel_2 v13 (controller_approved) dims=['cohorts', 'welcome_message'] engageΔ=+0.013 didΔ=+0.000 → **beneficial**
- nort_renewal_0 v10 (controller_approved) dims=['voice'] engageΔ=+0.002 didΔ=+0.000 → **neutral**

## Governance

```
{
  "approval_requests": 39,
  "decisions": {
    "approved": 35,
    "rejected": 4,
    "expired": 0
  },
  "rejection_reasons": {
    "approver declined (judgement call)": 4
  },
  "applied_via_approval": 34,
  "auto_applied": 62,
  "auto_apply_rejected": 0,
  "apply_conflicts": 0,
  "lapsed": 0,
  "peak_requests_per_24h": {
    "nort_renewal_0": 2,
    "nort_kyc_1": 1,
    "arav_renewal_0": 2,
    "arav_kyc_1": 1,
    "tide_renewal_0": 2,
    "tide_kyc_1": 1,
    "kest_renewal_0": 1,
    "kest_kyc_1": 1,
    "arav_cancel_2": 3,
    "tide_cancel_2": 3,
    "nort_cancel_2": 1,
    "kest_cancel_2": 1
  }
}
```

## Incidents

```
[]
```

## Decaying variants — share of in-population calls still on the variant, by day after onset

```
{
  "nort_renewal_0:welcome_message=W3": {
    "3": 0.8,
    "4": 0.796
  },
  "kest_renewal_0:welcome_message=W3": {
    "3": 0.795,
    "4": 0.798
  },
  "arav_renewal_0:welcome_message=W3": {
    "3": 0.798,
    "4": 0.8
  },
  "tide_renewal_0:welcome_message=W3": {
    "3": 0.799,
    "4": 0.804
  }
}
```

Credits used: 0 · validation runs: 0 · LLM fix calls: 0
