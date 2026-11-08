"""Tests for call-count aggregation, including idempotency and rollback."""

import pytest

from controller.store import Store


def _call(
    event_id: int,
    *,
    cohort: str = "default",
    ts: str = "2027-01-04T09:15:00+05:30",
    connected: bool = True,
    engaged: bool = False,
    welcome: str = "W1",
    voice: str = "V1",
    caller: str = "D1",
    prompt: str = "P1",
) -> dict:
    return {
        "event_id": event_id,
        "type": "call",
        "ts": ts,
        "workflow_id": "nw_renewal",
        "cohort_id": cohort,
        "variant": {
            "welcome_message": welcome,
            "voice": voice,
            "caller_id": caller,
            "prompt_version": prompt,
        },
        "connected": connected,
        "engaged": engaged,
    }


def _counts_rows(store: Store) -> int:
    return int(store.conn.execute("SELECT COUNT(*) FROM counts").fetchone()[0])


def test_ingesting_same_chunk_twice_leaves_counts_identical(tmp_path):
    store = Store(tmp_path / "store.db")
    events = [
        _call(1, engaged=True),
        _call(2, caller="D2", connected=False),
    ]

    store.save_chunk("events", 0, events)
    first_rows = _counts_rows(store)
    first_totals = store.hour_totals("nw_renewal", "2027-01-04T09")

    store.save_chunk("events", 0, events)

    assert _counts_rows(store) == first_rows == 5
    assert store.hour_totals("nw_renewal", "2027-01-04T09") == first_totals
    assert first_totals == {"dials": 2, "connected": 1, "engaged": 1}
    store.close()


def test_counts_are_split_by_cohort_dim_and_variant(tmp_path):
    store = Store(tmp_path / "store.db")
    events = [
        _call(1, cohort="senior_ta", voice="V2"),
        _call(2, cohort="young", caller="D2"),
    ]

    store.save_chunk("events", 0, events)

    row = store.conn.execute(
        "SELECT dials, connected, engaged FROM counts "
        "WHERE workflow_id = ? AND cohort_id = ? AND dim = ? AND variant = ? "
        "AND hour_bucket = ?",
        ("nw_renewal", "senior_ta", "voice", "V2", "2027-01-04T09"),
    ).fetchone()
    assert row == (1, 1, 0)
    assert _counts_rows(store) == 8
    store.close()


def test_failure_mid_save_rolls_back_counts_too(tmp_path):
    store = Store(tmp_path / "store.db")

    with pytest.raises(ValueError):
        store.save_chunk("events", 0, [_call(1), {"type": "call"}])

    assert store.count_events() == 0
    assert store.get_cursor("events") is None
    assert _counts_rows(store) == 0
    store.close()
