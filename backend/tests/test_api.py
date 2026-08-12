from pathlib import Path

from fastapi.testclient import TestClient

from app import config, market_data, repository
from app.main import app


def test_api_import_replay_trade_and_resume(tmp_path, monkeypatch):
    for module in (config, market_data, repository):
        for name, relative in (("RAW_ROOT", "raw"), ("OHLCV_ROOT", "ohlcv"), ("DB_PATH", "sessions/db.sqlite3")):
            if hasattr(module, name):
                monkeypatch.setattr(module, name, tmp_path / relative)
    fixture = Path(__file__).parent / "fixtures" / "dukascopy_1m.csv"
    with TestClient(app) as client:
        imported = client.post("/api/imports", json={
            "path": str(fixture), "symbol": "EURUSD", "asset_class": "forex", "pnl_currency": "USD",
            "price_precision": 5, "contract_multiplier": 100000, "default_profile": "utc_aligned",
        })
        assert imported.status_code == 200, imported.text
        session = client.post("/api/replay/sessions", json={
            "symbol": "EURUSD", "start": "2026-01-02T17:00:00Z", "end": "2026-01-02T17:06:00Z",
            "chart_context_1m_bars": 500, "advance_step_minutes": 2,
        }).json()
        session_id = session["id"]
        stepped = client.post(f"/api/replay/sessions/{session_id}/step").json()
        assert stepped["current_market_time"].endswith("17:01:00+00:00")
        trade = client.post(f"/api/replay/sessions/{session_id}/orders/market", json={
            "direction": "long", "quantity": 1, "stop_price": 1.0, "target_price": 2.0,
        })
        assert trade.status_code == 200, trade.text
        resumed = client.get(f"/api/replay/sessions/{session_id}/state").json()
        assert len(resumed["trades"]) == 1
