from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config, market_data, repository, service
from app.main import app
from app.migrations import CURRENT_SCHEMA_VERSION


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
            "price_precision": 5, "contract_multiplier": 1, "default_profile": "utc_aligned",
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


def test_health_reports_ready_database_and_schema(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "database": "ok",
        "schema_version": CURRENT_SCHEMA_VERSION,
    }


def test_session_listing_and_delete_lifecycle(client):
    created = create_session(client)
    session_id = created["id"]

    listed = client.get("/api/replay/sessions").json()
    assert len(listed) == 1
    summary = listed[0]
    assert set(summary) == {"id", "symbol", "start", "end", "status", "current_index", "updated_at"}
    assert summary["id"] == session_id
    assert summary["symbol"] == "EURUSD"
    assert summary["status"] == "active"
    assert summary["current_index"] == -1
    assert summary["updated_at"].endswith("+00:00")

    deleted = client.delete(f"/api/replay/sessions/{session_id}")
    assert deleted.status_code == 200
    assert deleted.json() == {"id": session_id, "deleted": True}
    assert client.get(f"/api/replay/sessions/{session_id}/state").status_code == 404
    assert client.delete(f"/api/replay/sessions/{session_id}").status_code == 404
    assert client.get("/api/replay/sessions").json() == []


def test_session_list_reflects_step_progress_and_completion(client):
    session_id = create_session(client, advance_step_minutes=10)["id"]
    stepped = client.post(f"/api/replay/sessions/{session_id}/step")
    assert stepped.status_code == 200, stepped.text
    summary = next(item for item in client.get("/api/replay/sessions").json() if item["id"] == session_id)
    assert summary["status"] == "completed"
    assert summary["current_index"] == 6


def test_cost_configuration_propagates_to_fills_and_stats(client):
    created = create_session(
        client, initial_balance=5000, spread=0.02, slippage=0.001, commission_per_quantity=5,
    )
    session_id = created["id"]
    assert created["initial_balance"] == 5000
    assert created["spread"] == 0.02
    assert created["slippage"] == 0.001
    assert created["commission_per_quantity"] == 5

    client.post(f"/api/replay/sessions/{session_id}/step")
    trade = client.post(f"/api/replay/sessions/{session_id}/orders/market", json={
        "direction": "long", "quantity": 1, "stop_price": 1.0, "target_price": 2.0,
    })
    assert trade.status_code == 200, trade.text
    entry = trade.json()["fills"][0]
    assert entry["reason"] == "entry"
    assert entry["market_price"] == pytest.approx(1.1013)
    assert entry["price"] == pytest.approx(1.1013 + 0.02 / 2 + 0.001)
    assert entry["commission"] == pytest.approx(5)
    assert entry["spread_cost"] == pytest.approx(0.01)
    assert entry["slippage_cost"] == pytest.approx(0.001)
    assert entry["gross_pnl"] == pytest.approx(0)
    assert entry["pnl"] == pytest.approx(-(5 + 0.01 + 0.001))

    stats = trade.json()["stats"]
    assert stats["net_pnl"] == pytest.approx(-5.011)
    assert stats["gross_pnl"] == pytest.approx(0)
    assert stats["trading_costs"] == pytest.approx(5.011)
    assert stats["commission_paid"] == pytest.approx(5)
    assert stats["spread_cost"] == pytest.approx(0.01)
    assert stats["slippage_cost"] == pytest.approx(0.001)
    assert stats["balance"] == pytest.approx(5000 - 5.011)
    # Unrealized values the open position at the current price minus estimated exit costs.
    assert stats["unrealized_pnl"] == pytest.approx(-5.011)
    assert stats["equity"] == pytest.approx(5000 - 2 * 5.011)

    client.post(f"/api/replay/sessions/{session_id}/step")
    state = client.get(f"/api/replay/sessions/{session_id}/state").json()
    assert state["stats"]["unrealized_pnl"] == pytest.approx((1.1015 - 1.1013) - 5.011)

    closed = client.post(f"/api/trades/{trade.json()['trades'][0]['id']}/close", json={
        "session_id": session_id, "quantity": 1,
    })
    assert closed.status_code == 200, closed.text
    exit_fill = closed.json()["fills"][1]
    assert exit_fill["reason"] == "manual"
    assert exit_fill["market_price"] == pytest.approx(1.1015)
    assert exit_fill["price"] == pytest.approx(1.1015 - 0.02 / 2 - 0.001)
    assert exit_fill["gross_pnl"] == pytest.approx(1.1015 - 1.1013)
    assert closed.json()["stats"]["net_pnl"] == pytest.approx(-5.011 + (1.1015 - 1.1013) - 5.011)
    assert closed.json()["stats"]["balance"] == pytest.approx(5000 - 5.011 + (1.1015 - 1.1013) - 5.011)
    assert closed.json()["stats"]["unrealized_pnl"] == 0


def test_zero_cost_session_keeps_legacy_defaults(client):
    created = create_session(client)
    assert created["initial_balance"] == 10000
    assert created["spread"] == 0
    assert created["slippage"] == 0
    assert created["commission_per_quantity"] == 0
    session_id = created["id"]
    client.post(f"/api/replay/sessions/{session_id}/step")
    trade = client.post(f"/api/replay/sessions/{session_id}/orders/market", json={
        "direction": "long", "quantity": 1, "stop_price": 1.0, "target_price": 2.0,
    }).json()
    entry = trade["fills"][0]
    assert entry["market_price"] == entry["price"]
    assert entry["commission"] == 0
    assert entry["spread_cost"] == 0
    assert entry["slippage_cost"] == 0
    assert entry["pnl"] == 0
    assert trade["stats"]["balance"] == 10000


def test_completed_session_guards_step_and_new_orders(client):
    session_id = create_session(client, advance_step_minutes=10)["id"]
    assert client.post(f"/api/replay/sessions/{session_id}/step").status_code == 200
    assert client.post(f"/api/replay/sessions/{session_id}/step").status_code == 400
    assert client.post(f"/api/replay/sessions/{session_id}/orders/market", json={
        "direction": "long", "quantity": 1,
    }).status_code == 400
    assert client.post(f"/api/replay/sessions/{session_id}/close-all").status_code == 200


def test_active_close_all_is_a_manual_exit(client):
    session_id = create_session(client)["id"]
    client.post(f"/api/replay/sessions/{session_id}/step")
    client.post(f"/api/replay/sessions/{session_id}/orders/market", json={
        "direction": "long", "quantity": 1, "stop_price": 1.0, "target_price": 2.0,
    })
    closed = client.post(f"/api/replay/sessions/{session_id}/close-all")
    assert closed.status_code == 200, closed.text
    state = closed.json()
    assert all(trade["status"] == "closed" for trade in state["trades"])
    assert all(fill["reason"] == "manual" for fill in state["fills"] if fill["reason"] != "entry")
    assert state["stats"]["unrealized_pnl"] == 0


def test_completed_session_close_all_uses_session_end_reason(client):
    session_id = create_session(client)["id"]
    client.post(f"/api/replay/sessions/{session_id}/step")
    trade = client.post(f"/api/replay/sessions/{session_id}/orders/market", json={
        "direction": "long", "quantity": 1, "stop_price": 1.0, "target_price": 2.0,
    }).json()
    trade_id = trade["trades"][0]["id"]
    assert client.patch(f"/api/replay/sessions/{session_id}/settings", json={"advance_step_minutes": 10}).status_code == 200
    assert client.post(f"/api/replay/sessions/{session_id}/step").status_code == 200  # completes
    assert client.post(f"/api/replay/sessions/{session_id}/step").status_code == 400
    closed = client.post(f"/api/replay/sessions/{session_id}/close-all")
    assert closed.status_code == 200, closed.text
    state = closed.json()
    assert state["status"] == "completed"
    assert all(trade["status"] == "closed" for trade in state["trades"])
    assert state["fills"][-1]["reason"] == "session_end"
    assert state["fills"][-1]["trade_id"] == trade_id


def test_manual_close_works_after_completion(client):
    session_id = create_session(client)["id"]
    client.post(f"/api/replay/sessions/{session_id}/step")
    trade = client.post(f"/api/replay/sessions/{session_id}/orders/market", json={
        "direction": "long", "quantity": 1, "stop_price": 1.0, "target_price": 2.0,
    })
    assert trade.status_code == 200, trade.text
    trade_id = trade.json()["trades"][0]["id"]
    assert client.patch(f"/api/replay/sessions/{session_id}/settings", json={"advance_step_minutes": 10}).status_code == 200
    assert client.post(f"/api/replay/sessions/{session_id}/step").status_code == 200  # completes
    assert client.post(f"/api/replay/sessions/{session_id}/step").status_code == 400
    closed = client.post(f"/api/trades/{trade_id}/close", json={
        "session_id": session_id, "quantity": 1,
    })
    assert closed.status_code == 200, closed.text
    assert closed.json()["fills"][-1]["reason"] == "manual"


def test_market_order_requires_a_causal_price(client):
    # Session starts at the very first published bar: no context bar and nothing revealed yet.
    session_id = create_session(client, start="2026-01-02T16:55:00Z")["id"]
    response = client.post(f"/api/replay/sessions/{session_id}/orders/market", json={
        "direction": "long", "quantity": 1,
    })
    assert response.status_code == 400
    assert "causal" in response.json()["detail"]


def test_market_order_rejects_pre_session_context_price(client):
    # Session starts at 17:00 with 16:55-16:59 pre-session context bars available,
    # but nothing revealed yet: a context price alone must not authorize an order
    # (the frontend disables entry until current_index >= 0).
    session_id = create_session(client, start="2026-01-02T17:00:00Z")["id"]
    state = client.get(f"/api/replay/sessions/{session_id}/state").json()
    assert state["current_index"] == -1
    assert state["current_price"] is not None  # pre-session context price exists
    response = client.post(f"/api/replay/sessions/{session_id}/orders/market", json={
        "direction": "long", "quantity": 1,
    })
    assert response.status_code == 400
    assert "causal" in response.json()["detail"]
    # Stepping once reveals a bar; the same order then goes through.
    client.post(f"/api/replay/sessions/{session_id}/step")
    order = client.post(f"/api/replay/sessions/{session_id}/orders/market", json={
        "direction": "long", "quantity": 1,
    })
    assert order.status_code == 200, order.text


def test_malformed_session_requests_are_rejected(client):
    unknown = client.post("/api/replay/sessions", json={
        "symbol": "NOPE", "start": "2026-01-02T17:00:00Z", "end": "2026-01-02T17:06:00Z",
    })
    assert unknown.status_code == 400
    assert unknown.json()["detail"] == "unknown symbol"
    assert client.post("/api/replay/sessions", json={
        "symbol": "EURUSD", "start": "2026-01-02T17:06:00Z", "end": "2026-01-02T17:00:00Z",
    }).status_code == 400
    for field in ("initial_balance", "spread", "slippage", "commission_per_quantity", "conversion_rate", "advance_step_minutes"):
        body = {
            "symbol": "EURUSD", "start": "2026-01-02T17:00:00Z", "end": "2026-01-02T17:06:00Z",
            field: -1 if field != "conversion_rate" else 0,
        }
        assert client.post("/api/replay/sessions", json=body).status_code == 422, field


def test_session_requires_explicit_timezone(client):
    response = client.post("/api/replay/sessions", json={
        "symbol": "EURUSD", "start": "2026-01-02T17:00:00", "end": "2026-01-02T17:06:00",
    })
    assert response.status_code == 400
    assert response.json()["detail"] == "start must include an explicit UTC offset"


def test_session_normalizes_explicit_offset_to_utc(client):
    created = create_session(
        client, start="2026-01-02T12:00:00-05:00", end="2026-01-02T12:06:00-05:00",
    )
    assert created["start"] == "2026-01-02T17:00:00+00:00"
    assert created["end"] == "2026-01-02T17:06:00+00:00"


def test_unknown_session_is_404_everywhere(client):
    cases = (
        ("get", "/api/replay/sessions/nope/state", None),
        ("post", "/api/replay/sessions/nope/step", None),
        ("post", "/api/replay/sessions/nope/close-all", None),
        ("post", "/api/replay/sessions/nope/orders/market", {"direction": "long", "quantity": 1}),
        ("delete", "/api/replay/sessions/nope", None),
    )
    for method, path, body in cases:
        kwargs = {"json": body} if body is not None else {}
        response = getattr(client, method)(path, **kwargs)
        assert response.status_code == 404, (method, path)


def test_stats_are_reported_from_core_engine(client):
    session_id = create_session(client)["id"]
    state = client.get(f"/api/replay/sessions/{session_id}/state").json()
    stats = state["stats"]
    for key in ("trades_opened", "trades_completed", "win_rate", "net_pnl", "gross_pnl", "trading_costs",
                "commission_paid", "spread_cost", "slippage_cost", "unrealized_pnl", "balance", "equity",
                "total_r", "average_r", "average_win", "average_loss", "profit_factor", "max_drawdown",
                "long_pnl", "short_pnl"):
        assert isinstance(stats[key], (int, float)), key
    assert stats["balance"] == stats["equity"]  # nothing open, nothing realized
    assert client.get(f"/api/replay/sessions/{session_id}/stats").json() == stats


def test_displayed_bars_stay_bounded_as_replay_advances(client, tmp_path):
    rows = []
    for i in range(600):
        hour, minute = divmod(i, 60)
        price = 1.1000 + i * 0.0001
        rows.append(
            f"2026.01.02,{hour:02d}:{minute:02d}:00,{price:.4f},{price + 0.0002:.4f},"
            f"{price - 0.0002:.4f},{price:.4f},10,0,0"
        )
    source = tmp_path / "long.csv"
    source.write_text("Date,Time,Open,High,Low,Close,TickVolume,Volume,Spread\n" + "\n".join(rows) + "\n")
    imported = client.post("/api/imports", json={
        "path": str(source), "symbol": "LONGRANGE", "asset_class": "forex", "pnl_currency": "USD",
        "price_precision": 5, "contract_multiplier": 1, "default_profile": "utc_aligned",
    })
    assert imported.status_code == 200, imported.text
    session = create_session(client, symbol="LONGRANGE", start="2026-01-02T00:00:00Z",
                             end="2026-01-02T09:59:00Z", advance_step_minutes=10)
    session_id = session["id"]
    assert session["displayed_bars"] == []  # nothing revealed yet
    state = session
    for _ in range(70):
        if state["status"] == "completed":
            break
        state = client.post(f"/api/replay/sessions/{session_id}/step").json()
        assert len(state["displayed_bars"]) <= 500
    assert state["status"] == "completed"
    assert len(state["displayed_bars"]) <= 500
    assert state["displayed_bars"][-1]["timestamp"] == state["current_market_time"]


def test_session_replays_pinned_version_after_reimport(client, tmp_path):
    session_id = create_session(client)["id"]
    for _ in range(4):
        assert client.post(f"/api/replay/sessions/{session_id}/step").status_code == 200
    state = client.get(f"/api/replay/sessions/{session_id}/state").json()
    assert state["current_market_time"].endswith("17:03:00+00:00")
    pinned_version = state["data_version"]
    assert pinned_version
    # Re-import the same symbol with the 17:03 bar removed.
    rows = [
        line for line in (Path(__file__).parent / "fixtures" / "dukascopy_1m.csv").read_text().splitlines()
        if not line.startswith("2026.01.02,17:03:00,")
    ]
    replaced = tmp_path / "removed.csv"
    replaced.write_text("\n".join(rows) + "\n")
    imported = client.post("/api/imports", json={
        "path": str(replaced), "symbol": "EURUSD", "asset_class": "forex", "pnl_currency": "USD",
        "price_precision": 5, "contract_multiplier": 1, "default_profile": "utc_aligned",
    })
    assert imported.status_code == 200, imported.text
    assert imported.json()["id"] != pinned_version
    # The existing session keeps stepping the pinned version: next bar is 17:04
    # (a re-based session would skip to 17:05) and the tail still holds two bars.
    stepped = client.post(f"/api/replay/sessions/{session_id}/step").json()
    assert stepped["current_market_time"].endswith("17:04:00+00:00")
    assert stepped["remaining_bars"] == 2
    # A fresh session pins the newly published version and skips the removed minute.
    fresh = create_session(client, start="2026-01-02T17:00:00Z", end="2026-01-02T17:06:00Z")
    assert fresh["data_version"] == imported.json()["id"]
    fresh_id = fresh["id"]
    for _ in range(4):
        assert client.post(f"/api/replay/sessions/{fresh_id}/step").status_code == 200
    fresh_state = client.get(f"/api/replay/sessions/{fresh_id}/state").json()
    assert fresh_state["current_market_time"].endswith("17:04:00+00:00")


def test_extreme_numeric_inputs_are_rejected(client):
    def post_raw(path, body: str):
        return client.post(path, content=body, headers={"Content-Type": "application/json"})

    # Non-finite session numbers fail Pydantic validation with 422 before any mutation.
    for field in ("initial_balance", "spread", "slippage", "commission_per_quantity", "conversion_rate"):
        body = (
            '{"symbol": "EURUSD", "start": "2026-01-02T17:00:00Z", "end": "2026-01-02T17:06:00Z", '
            f'"{field}": 1e309}}'
        )
        assert post_raw("/api/replay/sessions", body).status_code == 422, field
    session_id = create_session(client)["id"]
    client.post(f"/api/replay/sessions/{session_id}/step")
    trade = client.post(f"/api/replay/sessions/{session_id}/orders/market", json={
        "direction": "long", "quantity": 1, "stop_price": 1.0, "target_price": 2.0,
    }).json()
    trade_id = trade["trades"][0]["id"]
    for path in (f"/api/trades/{trade_id}/stop", f"/api/trades/{trade_id}/target"):
        response = client.put(path, content=f'{{"session_id": "{session_id}", "price": 1e309}}',
                              headers={"Content-Type": "application/json"})
        assert response.status_code == 422, path
    response = post_raw(f"/api/replay/sessions/{session_id}/orders/market",
                        '{"direction": "long", "quantity": 1e309}')
    assert response.status_code == 422
    # A finite-but-overflowing quantity yields an actionable 400 from the engine and
    # leaves neither an order nor a trade behind (audit and state commit atomically).
    big = create_session(client, commission_per_quantity=1e300)
    big_id = big["id"]
    client.post(f"/api/replay/sessions/{big_id}/step")
    response = client.post(f"/api/replay/sessions/{big_id}/orders/market", json={
        "direction": "long", "quantity": 1e308,
    })
    assert response.status_code == 400, response.text
    assert "finite" in response.json()["detail"]
    state = client.get(f"/api/replay/sessions/{big_id}/state").json()
    assert state["trades"] == []
    with repository.connect() as db:
        count = db.execute("SELECT COUNT(*) FROM orders WHERE session_id=?", (big_id,)).fetchone()[0]
    assert count == 0


def test_order_audit_commits_with_session_state(client):
    session_id = create_session(client)["id"]
    client.post(f"/api/replay/sessions/{session_id}/step")
    trade = client.post(f"/api/replay/sessions/{session_id}/orders/market", json={
        "direction": "long", "quantity": 1, "stop_price": 1.0, "target_price": 2.0,
    }).json()
    trade_id = trade["trades"][0]["id"]
    with repository.connect() as db:
        rows = db.execute(
            "SELECT order_type, payload_json FROM orders WHERE session_id=? ORDER BY created_at", (session_id,)
        ).fetchall()
    assert [row["order_type"] for row in rows] == ["market_entry"]
    assert rows[0]["payload_json"] == '{"direction": "long", "quantity": 1.0}'
    # A second market order produces a second audit row, all in one commit.
    client.post(f"/api/replay/sessions/{session_id}/orders/market", json={
        "direction": "short", "quantity": 2,
    })
    client.post(f"/api/trades/{trade_id}/close", json={"session_id": session_id, "quantity": 1})
    client.post(f"/api/replay/sessions/{session_id}/close-all")
    with repository.connect() as db:
        rows = db.execute(
            "SELECT order_type, payload_json FROM orders WHERE session_id=? ORDER BY created_at", (session_id,)
        ).fetchall()
    types = [row["order_type"] for row in rows]
    assert types == ["market_entry", "market_entry", "market_close", "close_all"]
    assert 'manual' in rows[-1]["payload_json"]


def test_weekend_gap_keeps_full_context_window(client, tmp_path):
    # Friday 2026-01-02: 500 one-minute bars; Monday 2026-01-05: 30 session bars.
    # A 500-minute wall-clock window before Monday 00:00 would reach back into the
    # empty weekend and serve no context; the bar-count window must serve all 500.
    rows = []
    for i in range(500):
        hour, minute = divmod(i, 60)
        price = 1.1000 + i * 0.0001
        rows.append(f"2026.01.02,{hour:02d}:{minute:02d}:00,{price:.4f},{price + 0.0002:.4f},"
                    f"{price - 0.0002:.4f},{price:.4f},10,0,0")
    for minute in range(30):
        rows.append(f"2026.01.05,00:{minute:02d}:00,1.1500,1.1502,1.1498,1.1500,10,0,0")
    source = tmp_path / "weekend.csv"
    source.write_text("Date,Time,Open,High,Low,Close,TickVolume,Volume,Spread\n" + "\n".join(rows) + "\n")
    imported = client.post("/api/imports", json={
        "path": str(source), "symbol": "GAP", "asset_class": "forex", "pnl_currency": "USD",
        "price_precision": 5, "contract_multiplier": 1, "default_profile": "utc_aligned",
    })
    assert imported.status_code == 200, imported.text
    session = create_session(client, symbol="GAP", start="2026-01-05T00:00:00Z",
                             end="2026-01-05T00:29:00Z", chart_context_1m_bars=500)
    session_id = session["id"]
    # The full Friday window is served as context: 500 bars, none from the session.
    assert len(session["displayed_bars"]) == 500
    assert session["displayed_bars"][0]["timestamp"] == "2026-01-02T00:00:00+00:00"
    assert session["displayed_bars"][-1]["timestamp"] == "2026-01-02T08:19:00+00:00"
    assert all(bar["timestamp"] < "2026-01-05T00:00:00+00:00" for bar in session["displayed_bars"])
    # The causal before window is exactly the 500 Friday bars; the replay is only the session.
    state = service.get_state(session_id)
    before, replay = service.session_bars(state)
    assert len(before) == 500
    assert [bar.timestamp.isoformat() for bar in replay] == [
        f"2026-01-05T00:{minute:02d}:00+00:00" for minute in range(30)
    ]
    # Indicator warmup stays causal across the gap: SMA over the Friday context only.
    stepped = client.post(f"/api/replay/sessions/{session_id}/step").json()
    assert stepped["current_market_time"] == "2026-01-05T00:00:00+00:00"
