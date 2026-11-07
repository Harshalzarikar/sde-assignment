"""Tests for the SQLite store's idempotency, cursor, and transaction guarantees."""

import pytest

from controller.store import Store


def _event(event_id: int, event_type: str = "lead") -> dict:
    return {
        "event_id": event_id,
        "type": event_type,
        "ts": "2027-01-04T00:00:00+05:30",
        "workflow_id": "nw_renewal",
        "payload_field": event_id,
    }


def _count(store: Store) -> int:
    return int(store.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])


def test_saving_same_chunk_twice_is_idempotent(tmp_path):
    store = Store(tmp_path / "store.db")
    events = [_event(1), _event(2)]

    store.save_chunk("history", 0, events)
    first_count = _count(store)
    store.save_chunk("history", 0, events)

    assert _count(store) == first_count == 2
    assert store.get_cursor("history") == 0
    store.close()


def test_cursor_only_moves_forward(tmp_path):
    store = Store(tmp_path / "store.db")

    store.save_chunk("events", 5, [_event(1)])
    store.save_chunk("events", 2, [_event(2)])

    assert store.get_cursor("events") == 5
    assert _count(store) == 2
    store.close()


def test_failure_mid_save_rolls_back_events_and_cursor(tmp_path):
    store = Store(tmp_path / "store.db")
    store.save_chunk("events", 0, [_event(1)])

    with pytest.raises(ValueError):
        store.save_chunk("events", 1, [_event(2), {"type": "lead"}])

    assert store.get_cursor("events") == 0
    assert _count(store) == 1
    store.close()
