"""Trade review mutations: validation, normalization, persistence, bounded states."""
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config, market_data, repository
from app.main import app
from app.service import MAX_REVIEW_NOTE_LENGTH, MAX_REVIEW_TAG_LENGTH, MAX_REVIEW_TAGS


@pytest.fixture()
def client(tmp_path, monkeypatch):
    for module in (config, market_data, repository):
        for name, relative in (("RAW_ROOT", "raw"), ("OHLCV_ROOT", "ohlcv"), ("DB_PATH", "sessions/db.sqlite3")):
            if hasattr(module, name):
                monkeypatch.setattr(module, name, tmp_path / relative)
    fixture = Path(__file__).parent / "fixtures" / "dukascopy_1m.csv"
    with TestClient(app) as test_client:
        imported = test_client.post("/api/imports", json={
            "path": str(fixture), "symbol": "EURUSD", "asset_class": "forex",
            "pnl_currency": "USD", "price_precision": 5, "contract_multiplier": 1,
            "default_profile": "utc_aligned",
        })
        assert imported.status_code == 200, imported.text
        yield test_client


def open_trade(client, sid) -> str:
    session = client.post("/api/replay/sessions", json={
        "symbol": "EURUSD", "start": "2026-01-02T17:00:00Z", "end": "2026-01-02T17:06:00Z",
        "chart_context_1m_bars": 500, "advance_step_minutes": 1,
    })
    assert session.status_code == 200, session.text
    client.post(f"/api/replay/sessions/{sid}/step")
    opened = client.post(f"/api/replay/sessions/{sid}/orders/market", json={
        "direction": "long", "quantity": 1,
    })
    assert opened.status_code == 200, opened.text
    return opened.json()["trade_upserts"][-1]["id"]


def test_review_roundtrip_persists_and_survives_reload(client):
    sid = client.post("/api/replay/sessions", json={
        "symbol": "EURUSD", "start": "2026-01-02T17:00:00Z", "end": "2026-01-02T17:06:00Z",
        "chart_context_1m_bars": 500, "advance_step_minutes": 1,
    }).json()["id"]
    trade_id = open_trade(client, sid)

    patched = client.patch(f"/api/trades/{trade_id}/review", json={
        "session_id": sid, "review_note": "  watch size  ", "review_tags": ["a", "a", "b", ""],
    })
    assert patched.status_code == 200, patched.text
    record = patched.json()
    assert record["trade_id"] == trade_id
    assert record["note"] == "watch size"  # trimmed
    assert record["tags"] == ["a", "b"]  # de-duplicated, empty dropped, order kept

    # A second session for the same client would evict nothing; force a real
    # database reload by dropping the in-memory cache.
    repository._cache_pop(sid)
    reloaded = client.get(f"/api/replay/sessions/{sid}/state")
    assert reloaded.status_code == 200, reloaded.text
    item = next(t for t in reloaded.json()["trades"] if t["id"] == trade_id)
    assert item["review_note"] == "watch size"
    assert item["review_tags"] == ["a", "b"]


def test_review_validation_bounds(client):
    sid = client.post("/api/replay/sessions", json={
        "symbol": "EURUSD", "start": "2026-01-02T17:00:00Z", "end": "2026-01-02T17:06:00Z",
        "chart_context_1m_bars": 500, "advance_step_minutes": 1,
    }).json()["id"]
    trade_id = open_trade(client, sid)

    # Exactly-at-bound values are accepted.
    ok = client.patch(f"/api/trades/{trade_id}/review", json={
        "session_id": sid, "review_note": "x" * MAX_REVIEW_NOTE_LENGTH,
        "review_tags": ["t" * MAX_REVIEW_TAG_LENGTH] + [f"tag{i}" for i in range(MAX_REVIEW_TAGS - 1)],
    })
    assert ok.status_code == 200, ok.text

    # One past each bound is rejected: note length by the request model,
    # tag count by the service.
    too_long_note = client.patch(f"/api/trades/{trade_id}/review", json={
        "session_id": sid, "review_note": "x" * (MAX_REVIEW_NOTE_LENGTH + 1),
    })
    assert too_long_note.status_code == 422
    too_long_tag = client.patch(f"/api/trades/{trade_id}/review", json={
        "session_id": sid, "review_tags": ["t" * (MAX_REVIEW_TAG_LENGTH + 1)],
    })
    assert too_long_tag.status_code == 422
    too_many_tags = client.patch(f"/api/trades/{trade_id}/review", json={
        "session_id": sid, "review_tags": [f"tag{i}" for i in range(MAX_REVIEW_TAGS + 1)],
    })
    assert too_many_tags.status_code == 400
    # Duplicates collapse before the count bound applies.
    deduped = client.patch(f"/api/trades/{trade_id}/review", json={
        "session_id": sid, "review_tags": ["same"] * (MAX_REVIEW_TAGS + 5),
    })
    assert deduped.status_code == 200, deduped.text
    assert deduped.json()["tags"] == ["same"]


def test_review_unknown_trade_is_404(client):
    sid = client.post("/api/replay/sessions", json={
        "symbol": "EURUSD", "start": "2026-01-02T17:00:00Z", "end": "2026-01-02T17:06:00Z",
        "chart_context_1m_bars": 500, "advance_step_minutes": 1,
    }).json()["id"]
    missing = client.patch(f"/api/trades/{uuid.uuid4()}/review", json={
        "session_id": sid, "review_note": "ghost",
    })
    assert missing.status_code == 404


def test_review_does_not_bump_session_revision(client):
    sid = client.post("/api/replay/sessions", json={
        "symbol": "EURUSD", "start": "2026-01-02T17:00:00Z", "end": "2026-01-02T17:06:00Z",
        "chart_context_1m_bars": 500, "advance_step_minutes": 1,
    }).json()["id"]
    trade_id = open_trade(client, sid)
    before = client.get(f"/api/replay/sessions/{sid}/state").json()["revision"]
    patched = client.patch(f"/api/trades/{trade_id}/review", json={
        "session_id": sid, "review_note": "note", "review_tags": ["x"],
    })
    assert patched.status_code == 200, patched.text
    after = client.get(f"/api/replay/sessions/{sid}/state").json()["revision"]
    assert after == before


def test_review_works_for_trades_outside_hydrated_window(client):
    # A session whose closed history exceeds the hydrated window: the review
    # mutation must resolve the trade from the table, not the in-memory set.
    from datetime import datetime, timedelta, timezone
    from app.domain import Fill, ReplayState, Trade

    def ts(minute):
        return datetime(2026, 1, 2, 17, minute % 60, minute // 60, tzinfo=timezone.utc)

    state = ReplayState.create(
        symbol="EURUSD", start=datetime(2026, 1, 2, 17, 0, tzinfo=timezone.utc),
        end=datetime(2026, 1, 3, 0, 0, tzinfo=timezone.utc), profile="utc_aligned",
        chart_context_1m_bars=500, advance_step_minutes=1, initial_balance=10000.0,
        contract_multiplier=1.0, price_precision=5, pnl_currency="USD",
    )
    old_trade = Trade(
        id="old-trade", session_id=state.id, direction="long", initial_quantity=1.0,
        remaining_quantity=0.0, entry_time=ts(1), entry_price=1.1013, realized_pnl=1.0,
        status="closed", entry_market_price=1.1013,
    )
    old_fill = Fill(
        id="old-fill", trade_id="old-trade", session_id=state.id, timestamp=ts(2),
        price=1.1015, quantity=1.0, reason="entry", pnl=-0.5,
        market_price=1.1013, gross_pnl=0.0,
    )
    for index in range(300):
        trade = Trade(
            id=f"trade-{index}", session_id=state.id, direction="short",
            initial_quantity=1.0, remaining_quantity=0.0, entry_time=ts(10 + index),
            entry_price=1.1, realized_pnl=-0.2, status="closed", entry_market_price=1.1,
        )
        fill = Fill(
            id=f"fill-{index}", trade_id=trade.id, session_id=state.id,
            timestamp=ts(11 + index), price=1.1002, quantity=1.0, reason="target",
            pnl=-0.2, market_price=1.1002, gross_pnl=-0.2,
        )
        state.trades.extend([trade])
        state.fills.extend([fill])
    state.trades.insert(0, old_trade)
    state.fills.insert(0, old_fill)
    repository.save_session(state, "test_seed")

    patched = client.patch(f"/api/trades/old-trade/review", json={
        "session_id": state.id, "review_note": "old but reviewable",
    })
    assert patched.status_code == 200, patched.text
    # The old trade is outside the hydrated snapshot window...
    snapshot = client.get(f"/api/replay/sessions/{state.id}/state").json()
    assert all(t["id"] != "old-trade" for t in snapshot["trades"])
    # ...but its review persisted and hydrates whenever the trade is served.
    reviews = repository.get_trade_reviews(["old-trade"])
    assert reviews["old-trade"] == ("old but reviewable", [])
