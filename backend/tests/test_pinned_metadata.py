from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config, market_data, repository
from app.domain import ReplayState
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    for module in (config, market_data, repository):
        for name, relative in (("RAW_ROOT", "raw"), ("OHLCV_ROOT", "ohlcv"), ("DB_PATH", "sessions/db.sqlite3")):
            if hasattr(module, name):
                monkeypatch.setattr(module, name, tmp_path / relative)
    market_data.invalidate_bars()
    fixture = Path(__file__).parent / "fixtures" / "dukascopy_1m.csv"
    with TestClient(app) as test_client:
        imported = test_client.post("/api/imports", json={
            "path": str(fixture), "symbol": "EURUSD", "asset_class": "forex", "pnl_currency": "USD",
            "price_precision": 5, "contract_multiplier": 100000, "default_profile": "utc_aligned",
        })
        assert imported.status_code == 200, imported.text
        yield test_client


def create_session(client, **overrides):
    body = {
        "symbol": "EURUSD", "start": "2026-01-02T17:00:00Z", "end": "2026-01-02T17:06:00Z",
        "chart_context_1m_bars": 500, "advance_step_minutes": 1,
    }
    body.update(overrides)
    response = client.post("/api/replay/sessions", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_session_snapshots_display_metadata(client):
    created = create_session(client)
    assert created["price_precision"] == 5
    assert created["pnl_currency"] == "USD"
    assert created["contract_multiplier"] == 100000
    assert created["data_version"]
    # The pinned fields survive a reload from the database.
    resumed = client.get(f"/api/replay/sessions/{created['id']}/state").json()
    assert resumed["price_precision"] == 5
    assert resumed["pnl_currency"] == "USD"
    assert resumed["contract_multiplier"] == 100000


def test_resumed_session_keeps_pinned_metadata_after_reimport(client, tmp_path):
    session_id = create_session(client)["id"]
    # Re-import the same symbol with different display metadata and data.
    rows = [
        line for line in (Path(__file__).parent / "fixtures" / "dukascopy_1m.csv").read_text().splitlines()
        if not line.startswith("2026.01.02,17:03:00,")
    ]
    replaced = tmp_path / "removed.csv"
    replaced.write_text("\n".join(rows) + "\n")
    imported = client.post("/api/imports", json={
        "path": str(replaced), "symbol": "EURUSD", "asset_class": "forex", "pnl_currency": "EUR",
        "price_precision": 3, "contract_multiplier": 10, "default_profile": "utc_aligned",
    })
    assert imported.status_code == 200, imported.text
    # The existing session still displays and formats with its pinned snapshot,
    # not the re-imported symbol row.
    resumed = client.get(f"/api/replay/sessions/{session_id}/state").json()
    assert resumed["price_precision"] == 5
    assert resumed["pnl_currency"] == "USD"
    assert resumed["contract_multiplier"] == 100000
    # A fresh session pins the re-imported metadata.
    fresh = create_session(client, start="2026-01-02T17:00:00Z", end="2026-01-02T17:06:00Z")
    assert fresh["price_precision"] == 3
    assert fresh["pnl_currency"] == "EUR"
    assert fresh["contract_multiplier"] == 10


def test_legacy_session_without_pinned_fields_loads_and_responds(client, tmp_path):
    # Simulate a pre-pinning legacy session: its state JSON has no
    # price_precision/pnl_currency/contract_multiplier keys at all.
    fixture = Path(__file__).parent / "fixtures" / "dukascopy_1m.csv"
    legacy = ReplayState.create(
        symbol="EURUSD", start=datetime(2026, 1, 2, 17, 0, tzinfo=timezone.utc),
        end=datetime(2026, 1, 2, 17, 6, tzinfo=timezone.utc), profile="utc_aligned",
        chart_context_1m_bars=500,
    )
    repository.save_session(legacy, "session_started")
    response = client.get(f"/api/replay/sessions/{legacy.id}/state")
    assert response.status_code == 200, response.text
    body = response.json()
    # The fields are present but null, which the frontend treats as a fallback
    # to the current symbol row. contract_multiplier falls back server-side for
    # execution math via _contract_multiplier.
    assert body["price_precision"] is None
    assert body["pnl_currency"] is None
    assert body["contract_multiplier"] is None


def test_replay_state_validates_pinned_display_fields():
    with pytest.raises(ValueError):
        ReplayState.create(symbol="TEST", start=datetime(2026, 1, 2, tzinfo=timezone.utc),
                           end=datetime(2026, 1, 3, tzinfo=timezone.utc), profile="utc_aligned",
                           price_precision=-1)
    with pytest.raises(ValueError):
        ReplayState.create(symbol="TEST", start=datetime(2026, 1, 2, tzinfo=timezone.utc),
                           end=datetime(2026, 1, 3, tzinfo=timezone.utc), profile="utc_aligned",
                           pnl_currency="  ")
