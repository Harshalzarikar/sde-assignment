"""Read-only exploration of the simulator's workflows, policies, and current config.

Usage: python scripts/explore.py [SIM_URL]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from controller.client import SimClient


def _value(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _window(window: Any) -> str:
    if isinstance(window, (list, tuple)) and len(window) == 2:
        return f"{int(window[0]):02d}:00-{int(window[1]):02d}:00 IST"
    return _value(window)


def _objective(objective: dict[str, Any]) -> str:
    primary = objective.get("primary", "unknown")
    guardrail = objective.get("guardrail", {})
    if isinstance(guardrail, dict):
        parts = [f"{k}={_value(v)}" for k, v in guardrail.items()]
        guard = ", ".join(parts) if parts else "none"
    else:
        guard = _value(guardrail)
    return f"primary={primary}; guardrail={guard}"


def _print_governance(governance: dict[str, Any]) -> None:
    print("governance:")
    ordered = [
        "approver_hours",
        "approver_latency_minutes",
        "proposal_ttl_hours",
        "apply_window_hours",
        "max_requests_per_24h",
        "base_reject_rate",
        "min_validation_pass_rate",
        "auto_apply",
    ]
    for key in ordered:
        if key not in governance:
            continue
        value = governance[key]
        if key == "auto_apply" and isinstance(value, dict):
            print(
                "  auto_apply: "
                f"enabled={value.get('enabled')}, "
                f"allowed_dims={_value(value.get('allowed_dims', []))}, "
                f"max_shift={_value(value.get('max_shift'))}"
            )
        elif key == "approver_hours":
            print(f"  {key}: {_window(value)}")
        elif key == "approver_latency_minutes" and isinstance(value, (list, tuple)):
            print(f"  {key}: {value[0]}-{value[-1]} minutes")
        else:
            print(f"  {key}: {_value(value)}")
    for key in sorted(governance):
        if key not in ordered:
            print(f"  {key}: {_value(governance[key])}")


def _print_workflow(client: SimClient, workflow: dict[str, Any]) -> None:
    wid = workflow["workflow_id"]
    print(f"\n--- {wid} ---")
    print(f"tenant: {workflow.get('tenant_id')}")
    if workflow.get("use_case"):
        print(f"use case: {workflow['use_case']}")
    if workflow.get("languages"):
        print(f"languages: {_value(workflow['languages'])}")
    print(f"objective: {_objective(workflow.get('objective', {}))}")
    print(f"mid-flight: {workflow.get('mid_flight')}")
    print(f"dialling window: {_window(workflow.get('dialling_window'))}")
    print(f"cadence: {_value(workflow.get('cadence'))}")
    print(f"capacity: {workflow.get('dials_per_hour')} dials/hour")
    if workflow.get("min_gap_minutes") is not None:
        print(f"minimum gap: {workflow['min_gap_minutes']} minutes")
    if workflow.get("daily_batch"):
        batch = workflow["daily_batch"]
        print(
            "daily batch: "
            f"size={batch.get('size')}, at hour={batch.get('at_hour')}, "
            f"returning share={batch.get('returning_share')}"
        )

    print("options:")
    for dimension, values in workflow.get("options", {}).items():
        print(f"  {dimension}: {_value(values)}")
    if workflow.get("prompt_catalog"):
        print("prompt catalog:")
        for prompt, description in workflow["prompt_catalog"].items():
            print(f"  {prompt}: {description}")

    _print_governance(workflow.get("governance", {}))

    config = client.get_config(wid)
    print(
        f"current config (version {config.get('version')}, "
        f"actor {config.get('actor')}, applied_at {config.get('applied_at')}):"
    )
    print(json.dumps(config.get("config", config), indent=2, ensure_ascii=False, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only simulator exploration")
    parser.add_argument("sim_url", nargs="?", default="http://127.0.0.1:8700")
    args = parser.parse_args()

    client = SimClient(args.sim_url, timeout=30, retries=2)
    state = client.get_state()
    print("=== Simulator state ===")
    print(f"scenario: {state.get('scenario')}")
    print(f"sim time: {state.get('ts')} (minute {state.get('sim_minute')} of {state.get('end_minute')})")
    print(f"finished: {state.get('finished')}")
    print(f"credits remaining: {state.get('credits_remaining')}")
    print(f"last event id: {state.get('last_event_id')}")

    workflows = client.get_workflows()
    print(f"\n=== Workflows ({len(workflows)}) ===")
    for workflow in workflows:
        _print_workflow(client, workflow)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
