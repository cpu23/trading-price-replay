import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app import config, repository
from app.domain import ReplayState
from app.repository import StaleSessionError, delete_session, initialize, load_session, save_session


@pytest.fixture()
def db(tmp_path, monkeypatch):
    for module in (config, repository):
        for name, relative in (("RAW_ROOT", "raw"), ("OHLCV_ROOT", "ohlcv"), ("DB_PATH", "sessions/db.sqlite3")):
            if hasattr(module, name):
                monkeypatch.setattr(module, name, tmp_path / relative)
    initialize()



def test_connections_configure_writer_contention_timeout(db):
    with repository.connect() as connection:
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 30_000


def make_state(**overrides) -> ReplayState:
    return ReplayState.create(
        symbol="TEST",
        start=datetime(2026, 1, 2, tzinfo=timezone.utc),
        end=datetime(2026, 1, 2, 1, tzinfo=timezone.utc),
        profile="utc_aligned",
        **overrides,
    )


def audit_rows(session_id: str) -> dict[str, int]:
    with repository.connect() as db:
        return {
            table: db.execute(
                f"SELECT COUNT(*) FROM {table} WHERE session_id=?", (session_id,)
            ).fetchone()[0]
            for table in ("replay_events", "orders", "trades", "fills")
        }


def test_stale_state_cannot_overwrite_newer_state(db):
    state = make_state()
    save_session(state, "session_started")
    assert state.revision == 1

    first = load_session(state.id)
    second = load_session(state.id)
    assert first.revision == 1 and second.revision == 1

    first.current_index = 5
    save_session(first, "replay_stepped")
    assert first.revision == 2

    # The second copy was loaded at revision 1: its save must fail the CAS.
    second.current_index = 9
    with pytest.raises(StaleSessionError):
        save_session(second, "replay_stepped")
    assert second.revision == 1  # failed save leaves the in-memory revision untouched

    # The database keeps the newer state; nothing from the failed copy leaked.
    reloaded = load_session(state.id)
    assert reloaded.current_index == 5
    assert reloaded.revision == 2
    assert audit_rows(state.id) == {"replay_events": 2, "orders": 0, "trades": 0, "fills": 0}
    with repository.connect() as db:
        events = db.execute(
            "SELECT event_type FROM replay_events WHERE session_id=? ORDER BY id", (state.id,)
        ).fetchall()
    assert [row["event_type"] for row in events] == ["session_started", "replay_stepped"]


def test_deleted_session_cannot_be_resurrected(db):
    state = make_state()
    save_session(state, "session_started")
    loaded = load_session(state.id)
    assert delete_session(state.id) is True

    # A save based on the pre-deletion load must neither resurrect the session
    # nor write any audit rows.
    loaded.current_index = 3
    with pytest.raises(StaleSessionError):
        save_session(loaded, "replay_stepped")

    assert load_session(state.id) is None
    assert audit_rows(state.id) == {"replay_events": 0, "orders": 0, "trades": 0, "fills": 0}
    with repository.connect() as db:
        remaining = db.execute(
            "SELECT COUNT(*) FROM replay_sessions WHERE id=?", (state.id,)
        ).fetchone()[0]
    assert remaining == 0
    assert delete_session(state.id) is False


def test_state_without_revision_cannot_reinsert_existing_session(db):
    state = make_state()
    save_session(state, "session_started")
    # A fabricated copy that never loaded a revision must not silently overwrite
    # the stored row via an upsert-style insert.
    stale = ReplayState(
        id=state.id, symbol="TEST",
        start=datetime(2026, 1, 2, tzinfo=timezone.utc),
        end=datetime(2026, 1, 2, 1, tzinfo=timezone.utc),
        profile="utc_aligned",
    )
    with pytest.raises(StaleSessionError):
        save_session(stale, "replay_stepped")
    reloaded = load_session(state.id)
    assert reloaded.revision == 1
    assert audit_rows(state.id) == {"replay_events": 1, "orders": 0, "trades": 0, "fills": 0}


def test_legacy_database_migrates_revision_column(db, tmp_path, monkeypatch):
    legacy_path = tmp_path / "legacy.sqlite3"
    monkeypatch.setattr(config, "DB_PATH", legacy_path)
    monkeypatch.setattr(config, "RAW_ROOT", tmp_path / "raw")
    monkeypatch.setattr(config, "OHLCV_ROOT", tmp_path / "ohlcv")
    monkeypatch.setattr(repository, "DB_PATH", legacy_path)

    # Pre-migration schema: replay_sessions has no revision column.
    legacy = make_state()
    payload = repository.serializable(legacy)
    payload.pop("revision", None)
    with sqlite3.connect(legacy_path) as db:
        db.executescript(
            """
            CREATE TABLE replay_sessions (
              id TEXT PRIMARY KEY, state_json TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            """
        )
        db.execute(
            "INSERT INTO replay_sessions(id,state_json,updated_at) VALUES(?,?,?)",
            (legacy.id, json.dumps(payload), "2026-01-01T00:00:00+00:00"),
        )

    initialize()  # must add the revision column and backfill it to 1
    loaded = load_session(legacy.id)
    assert loaded.revision == 1

    # A CAS save on the migrated row works and increments the revision.
    loaded.current_index = 2
    save_session(loaded, "replay_stepped")
    assert loaded.revision == 2
    assert load_session(legacy.id).revision == 2

    # New sessions created after migration also start at revision 1.
    fresh = make_state()
    save_session(fresh, "session_started")
    assert fresh.revision == 1


def test_revision_increments_on_every_successful_save(db):
    state = make_state()
    save_session(state, "session_started")
    assert state.revision == 1
    for expected in (2, 3, 4):
        state.current_index = expected
        save_session(state, "replay_stepped")
        assert state.revision == expected
    assert load_session(state.id).revision == 4


def test_stale_save_maps_to_http_409():
    from app.main import stale_session_exception_handler

    response = stale_session_exception_handler(None, StaleSessionError("stale session"))
    assert response.status_code == 409
    assert json.loads(response.body)["detail"] == "stale session"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app import market_data, main

    for module in (config, market_data, repository):
        for name, relative in (("RAW_ROOT", "raw"), ("OHLCV_ROOT", "ohlcv"), ("DB_PATH", "sessions/db.sqlite3")):
            if hasattr(module, name):
                monkeypatch.setattr(module, name, tmp_path / relative)
    market_data.invalidate_bars()
    fixture = Path(__file__).parent / "fixtures" / "dukascopy_1m.csv"
    with TestClient(main.app) as test_client:
        imported = test_client.post("/api/imports", json={
            "path": str(fixture), "symbol": "EURUSD", "asset_class": "forex", "pnl_currency": "USD",
            "price_precision": 5, "contract_multiplier": 1, "default_profile": "utc_aligned",
        })
        assert imported.status_code == 200, imported.text
        yield test_client


def test_stale_save_via_direct_route_save_returns_409(client, monkeypatch):
    from app import service as service_module

    created = client.post("/api/replay/sessions", json={
        "symbol": "EURUSD", "start": "2026-01-02T17:00:00Z", "end": "2026-01-02T17:06:00Z",
        "chart_context_1m_bars": 500, "advance_step_minutes": 1,
    })
    assert created.status_code == 200, created.text
    session_id = created.json()["id"]
    assert client.post(f"/api/replay/sessions/{session_id}/step").status_code == 200
    trade = client.post(f"/api/replay/sessions/{session_id}/orders/market", json={
        "direction": "long", "quantity": 1, "stop_price": 1.0, "target_price": 2.0,
    })
    assert trade.status_code == 200, trade.text
    trade_id = trade.json()["trade_upserts"][0]["id"]

    # `attempt` does not swallow StaleSessionError: a stale save from any
    # mutation route must surface as 409 (not 500) via the app-wide handler.
    def stale_save(*_args, **_kwargs):
        raise StaleSessionError("session was modified or deleted by another client; reload and retry")

    monkeypatch.setattr(service_module, "save_session", stale_save)
    response = client.put(f"/api/trades/{trade_id}/stop", json={"session_id": session_id, "price": 1.05})
    assert response.status_code == 409
    assert response.json()["detail"] == "session was modified or deleted by another client; reload and retry"
