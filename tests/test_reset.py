"""Tests for simulator-reset detection and fresh-DB archiving."""

from controller.store import Store, open_store


def _event(event_id: int) -> dict:
    return {
        "event_id": event_id,
        "type": "lead",
        "ts": "2027-01-04T00:00:00+05:30",
        "workflow_id": "nw_renewal",
    }


def _state(last_event_id: int, chunks: int) -> dict:
    return {
        "scenario": "public_01_dev",
        "end_minute": 11520,
        "tick_minutes": 15,
        "last_event_id": last_event_id,
        "chunks": chunks,
        "ts": "2027-01-04T00:00:00+05:30",
    }


def test_env_var_configures_db_path(tmp_path, monkeypatch):
    target = tmp_path / "from_env.db"
    monkeypatch.setenv("CONTROLLER_DB", str(target))
    store = Store()
    try:
        assert store.path == target
    finally:
        store.close()


def test_higher_simulator_state_resumes_existing_db(tmp_path):
    db_path = tmp_path / "controller.db"
    store = Store(db_path)
    store.save_chunk("events", 5, [_event(100)])
    store.close()

    messages = []
    store = open_store(_state(last_event_id=150, chunks=6), db_path, log=messages.append)
    try:
        assert store.count_events() == 1
        assert store.get_cursor("events") == 5
        assert store.get_meta("world") is not None
        assert not list(tmp_path.glob("*.bak"))
        assert any("resuming" in message for message in messages)
    finally:
        store.close()


def test_lower_simulator_state_archives_and_starts_fresh(tmp_path):
    db_path = tmp_path / "controller.db"
    store = Store(db_path)
    store.save_chunk("events", 5, [_event(100)])
    store.close()

    messages = []
    store = open_store(_state(last_event_id=50, chunks=2), db_path, log=messages.append)
    try:
        assert store.count_events() == 0
        assert store.get_cursor("events") is None
        assert any("reset detected" in message for message in messages)
        assert any("starting fresh database" in message for message in messages)
        archives = list(tmp_path.glob("*.bak"))
        assert len(archives) == 1
        assert archives[0].exists()
        audit = store.conn.execute(
            "SELECT kind, reason FROM audit ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert audit[0] == "sim_reset"
    finally:
        store.close()
