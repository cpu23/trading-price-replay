"""Phase-1 replay-time semantics.

A candle stamped 17:00 is only causally revealed at 17:01. The market clock
exposes the reveal time; market/manual executions inherit it as an exact
timestamp; intrabar stop/target fills carry the candle interval; gap fills at
the open are exact at the open.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config, market_data, repository
from app.domain import Bar, Fill, ReplayState, Trade, bar_reveal_time
from app.execution import close_trade, open_trade, process_bar, update_close_excursions
from app.main import app
from app.service import _state_response


def ts(minute: int) -> datetime:
    return datetime(2026, 1, 2, 17, minute, tzinfo=timezone.utc)


def bar(minute: int, open_: float = 10.0, high: float = 11.0, low: float = 9.0, close: float = 10.0) -> Bar:
    return Bar(timestamp=ts(minute), open=open_, high=high, low=low, close=close, volume=0.0)


def make_state() -> ReplayState:
    return ReplayState.create(
        symbol="TEST",
        start=datetime(2026, 1, 2, 17, 0, tzinfo=timezone.utc),
        end=datetime(2026, 1, 2, 18, 0, tzinfo=timezone.utc),
        profile="utc_aligned",
        chart_context_1m_bars=0,
        advance_step_minutes=1,
        initial_balance=10000.0,
        contract_multiplier=1.0,
        price_precision=5,
        pnl_currency="USD",
    )


def reveal(bar_: Bar) -> datetime:
    return bar_.timestamp + timedelta(minutes=1)


# --- reveal time ---------------------------------------------------------


def test_bar_reveal_time_is_open_plus_one_minute():
    b = bar(0)
    assert bar_reveal_time(b) == b.timestamp + timedelta(minutes=1)
    assert bar_reveal_time(b).tzinfo is not None


def test_sequential_candle_reveal_times_advance_one_minute():
    # Each revealed M1 candle's causal time is its open + 1 minute, so
    # sequential reveals advance exactly one minute apart. (The service-layer
    # `step` / multi-minute stepping is covered by the API tests.)
    reveals = [bar_reveal_time(bar(m)) for m in range(3)]
    assert reveals[0] == ts(1)
    assert reveals[1] == ts(2)
    assert reveals[2] == ts(3)
    for earlier, later in zip(reveals, reveals[1:]):
        assert later - earlier == timedelta(minutes=1)


# --- exact executions at reveal time --------------------------------------


def test_market_entry_fill_is_exact_at_reveal_time():
    state = make_state()
    b = bar(0)
    trade = open_trade(state, reveal(b), float(b.close), "long", 1.0, None, None, 1.0)
    fill = state.fills[-1]
    assert fill.reason == "entry"
    assert fill.timestamp == reveal(b)
    assert fill.time_precision == "exact"
    assert fill.execution_window_start == fill.execution_window_end == fill.timestamp
    assert trade.entry_time == reveal(b)
    assert trade.entry_price == b.close
    assert trade.entry_market_price == b.close


def test_manual_exit_fill_is_exact_at_reveal_time():
    state = make_state()
    b1, b2 = bar(0), bar(1)
    trade = open_trade(state, reveal(b1), 10.0, "long", 1.0, 9.0, 12.0, 1.0)
    close_trade(state, trade, reveal(b2), 10.5, 1.0, "manual", 1.0)
    fill = state.fills[-1]
    assert fill.reason == "manual"
    assert fill.timestamp == reveal(b2)
    assert fill.time_precision == "exact"
    assert trade.status == "closed"
    assert trade.exit_time == reveal(b2)
    assert trade.exit_time_precision == "exact"
    assert trade.exit_window_start == trade.exit_window_end == reveal(b2)
    assert trade.exit_price == 10.5
    assert trade.exit_market_price == 10.5
    assert trade.final_exit_reason == "manual"


def test_final_session_liquidation_is_exact_at_final_reveal():
    state = make_state()
    b1, b5 = bar(0), bar(4)
    trade = open_trade(state, reveal(b1), 10.0, "long", 1.0, 9.0, 12.0, 1.0)
    close_trade(state, trade, reveal(b5), 10.2, 1.0, "session_end", 1.0)
    fill = state.fills[-1]
    assert fill.reason == "session_end"
    assert fill.timestamp == reveal(b5)
    assert fill.time_precision == "exact"
    assert trade.exit_time == reveal(b5)
    assert trade.final_exit_reason == "session_end"


# --- stop/target execution windows -----------------------------------------


def test_opening_gap_stop_executes_exactly_at_candle_open():
    state = make_state()
    trade = open_trade(state, ts(1), 10.0, "long", 1.0, stop_price=9.5, target_price=12.0, contract_multiplier=1.0)
    b = bar(2, open_=9.0, high=12.0, low=8.5, close=9.2)  # gaps through the stop
    process_bar(state, b, 1.0)
    fill = state.fills[-1]
    assert fill.reason == "stop"
    assert fill.price == 9.0  # contracted to the gap open
    assert fill.timestamp == b.timestamp
    assert fill.time_precision == "exact"
    assert fill.execution_window_start == fill.execution_window_end == b.timestamp
    assert trade.exit_time == b.timestamp
    assert trade.exit_time_precision == "exact"
    assert trade.exit_window_start == trade.exit_window_end == b.timestamp
    assert trade.final_exit_reason == "stop"


def test_opening_gap_target_executes_exactly_at_candle_open():
    state = make_state()
    trade = open_trade(state, ts(1), 10.0, "short", 1.0, stop_price=10.5, target_price=9.5, contract_multiplier=1.0)
    b = bar(2, open_=9.0, high=12.0, low=8.5, close=9.2)  # gaps through the short target
    process_bar(state, b, 1.0)
    fill = state.fills[-1]
    assert fill.reason == "target"
    assert fill.price == 9.0
    assert fill.time_precision == "exact"
    assert trade.exit_time == b.timestamp
    assert trade.exit_time_precision == "exact"
    assert trade.final_exit_reason == "target"


def test_ordinary_intrabar_stop_is_bar_interval():
    state = make_state()
    trade = open_trade(state, ts(1), 10.0, "long", 1.0, stop_price=9.5, target_price=12.0, contract_multiplier=1.0)
    b = bar(2, open_=10.2, high=10.8, low=9.4, close=9.8)  # open above the stop
    process_bar(state, b, 1.0)
    fill = state.fills[-1]
    assert fill.reason == "stop"
    assert fill.time_precision == "bar_interval"
    assert fill.timestamp == b.timestamp  # effective ordering timestamp
    assert fill.execution_window_start == b.timestamp
    assert fill.execution_window_end == b.timestamp + timedelta(minutes=1)
    assert trade.exit_time == b.timestamp
    assert trade.exit_time_precision == "bar_interval"
    assert trade.exit_window_start == b.timestamp
    assert trade.exit_window_end == b.timestamp + timedelta(minutes=1)


def test_ordinary_intrabar_target_is_bar_interval():
    state = make_state()
    trade = open_trade(state, ts(1), 10.0, "long", 1.0, stop_price=9.5, target_price=10.8, contract_multiplier=1.0)
    b = bar(2, open_=10.2, high=10.9, low=9.7, close=10.7)  # open below the target
    process_bar(state, b, 1.0)
    fill = state.fills[-1]
    assert fill.reason == "target"
    assert fill.time_precision == "bar_interval"
    assert fill.execution_window_start == b.timestamp
    assert fill.execution_window_end == b.timestamp + timedelta(minutes=1)
    assert trade.exit_time_precision == "bar_interval"
    assert trade.final_exit_reason == "target"


def test_ambiguous_candle_stops_first_with_bar_interval():
    state = make_state()
    trade = open_trade(state, ts(1), 10.0, "long", 1.0, stop_price=9.5, target_price=10.5, contract_multiplier=1.0)
    b = bar(2, open_=10.0, high=10.6, low=9.4, close=10.0)  # touches both intrabar
    process_bar(state, b, 1.0)
    fill = state.fills[-1]
    assert fill.reason == "stop"  # conservative stop-first handling preserved
    assert fill.time_precision == "bar_interval"
    assert trade.status == "closed"


# --- close-based excursion semantics ----------------------------------------


def test_close_excursion_tracks_only_revealed_closes():
    state = make_state()
    b0 = bar(0, open_=10.0, high=10.4, low=9.6, close=10.0)
    trade = open_trade(state, reveal(b0), 10.0, "long", 1.0, 9.0, 12.0, 1.0)
    update_close_excursions(state, b0, 1.0)  # reference close equals entry
    b1 = bar(1, open_=10.0, high=11.0, low=9.6, close=11.0)
    process_bar(state, b1, 1.0)
    update_close_excursions(state, b1, 1.0)
    assert trade.mfe_gross_pnl == pytest.approx(1.0)
    assert trade.mae_gross_pnl == pytest.approx(0.0)
    b2 = bar(2, open_=10.0, high=10.4, low=9.6, close=9.6)
    process_bar(state, b2, 1.0)
    update_close_excursions(state, b2, 1.0)
    assert trade.mae_gross_pnl == pytest.approx(-0.4)
    assert trade.mfe_gross_pnl == pytest.approx(1.0)


def test_short_close_excursion_is_mirrored():
    state = make_state()
    b0 = bar(0, open_=10.0, high=10.4, low=9.6, close=10.0)
    trade = open_trade(state, reveal(b0), 10.0, "short", 1.0, 11.0, 8.0, 1.0)
    update_close_excursions(state, b0, 1.0)
    b1 = bar(1, open_=10.0, high=10.4, low=9.0, close=9.0)
    process_bar(state, b1, 1.0)
    update_close_excursions(state, b1, 1.0)
    assert trade.mfe_gross_pnl == pytest.approx(1.0)  # price fell below entry
    assert trade.mae_gross_pnl == pytest.approx(0.0)


def test_close_that_closed_the_trade_via_stop_is_not_counted():
    state = make_state()
    b0 = bar(0, open_=10.0, high=10.4, low=9.6, close=10.0)
    trade = open_trade(state, reveal(b0), 10.0, "long", 1.0, 9.0, 12.0, 1.0)
    update_close_excursions(state, b0, 1.0)
    b1 = bar(1, open_=10.5, high=10.9, low=8.8, close=8.9)  # stop touched intrabar
    process_bar(state, b1, 1.0)
    assert trade.status == "closed"
    update_close_excursions(state, b1, 1.0)  # must skip a closed trade
    assert trade.mfe_gross_pnl == pytest.approx(0.0)
    assert trade.mae_gross_pnl == pytest.approx(0.0)


# --- legacy fills ------------------------------------------------------------


def test_legacy_fills_load_without_precision_metadata():
    payload = {
        "id": "f1", "trade_id": "t1", "session_id": "s1",
        "timestamp": "2026-01-02T17:00:00+00:00", "price": 10.0, "quantity": 1.0,
        "reason": "entry", "pnl": -0.5, "market_price": 10.0, "gross_pnl": 0.0,
        "commission": 0.5, "spread_cost": 0.0, "slippage_cost": 0.0,
    }
    fill = repository._parse_fill(payload)
    assert fill.time_precision is None
    assert fill.execution_window_start is None
    assert fill.execution_window_end is None


def test_legacy_trade_loads_without_exit_metadata():
    payload = {
        "id": "t1", "session_id": "s1", "direction": "long",
        "initial_quantity": 1.0, "remaining_quantity": 0.0,
        "entry_time": "2026-01-02T17:01:00+00:00", "entry_price": 10.0,
        "stop_price": None, "target_price": None, "initial_risk": None,
        "realized_pnl": 1.0, "status": "closed", "entry_market_price": 10.0,
    }
    trade = repository._parse_trade(payload)
    assert trade.exit_time is None
    assert trade.exit_time_precision is None
    assert trade.exit_window_start is None
    assert trade.exit_window_end is None
    assert trade.final_exit_reason is None
    assert trade.exit_price is None
    assert trade.exit_market_price is None


def test_state_response_normalizes_legacy_precision():
    state = make_state()
    trade = Trade(
        id="t1", session_id=state.id, direction="long", initial_quantity=1.0,
        remaining_quantity=0.0, entry_time=ts(1), entry_price=10.0,
        realized_pnl=1.0, status="closed", entry_market_price=10.0,
    )
    state.trades.append(trade)
    fill = Fill(
        id="f1", trade_id="t1", session_id=state.id, timestamp=ts(1),
        price=10.0, quantity=1.0, reason="entry", pnl=-0.5,
        market_price=10.0, gross_pnl=0.0,
    )
    state.fills.append(fill)
    open_t = open_trade(state, ts(2), 10.0, "short", 1.0, 11.0, 8.0, 1.0)
    response = _state_response(state, [], [])
    by_id = {item["id"]: item for item in response["trades"]}
    assert by_id["t1"]["exit_time_precision"] == "legacy"
    assert by_id[open_t.id]["exit_time_precision"] is None  # still open
    assert response["fills"][0]["time_precision"] == "legacy"
    assert response["fills"][-1]["time_precision"] == "exact"


# --- API-level reveal semantics (fixture market data) -------------------------


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


def create_session(client, advance_step_minutes=1):
    session = client.post("/api/replay/sessions", json={
        "symbol": "EURUSD", "start": "2026-01-02T17:00:00Z", "end": "2026-01-02T17:06:00Z",
        "chart_context_1m_bars": 500, "advance_step_minutes": advance_step_minutes,
    })
    assert session.status_code == 200, session.text
    return session.json()


def test_api_market_clock_shows_reveal_time(client):
    session = create_session(client)
    # Before the first step the clock shows the last context candle (16:59),
    # revealed at 17:00 -- never the unrevealed session-start open.
    assert session["current_market_time"] == "2026-01-02T17:00:00+00:00"
    assert session["current_candle_time"] == "2026-01-02T16:59:00+00:00"
    assert session["current_price"] == 1.1010
    stepped = client.post(f"/api/replay/sessions/{session['id']}/step").json()
    # The 17:00 candle (close 1.1013) is revealed at 17:01.
    assert stepped["current_market_time"] == "2026-01-02T17:01:00+00:00"
    assert stepped["current_candle_time"] == "2026-01-02T17:00:00+00:00"
    assert stepped["current_price"] == 1.1013


def test_api_sequential_and_multi_minute_stepping(client):
    session = create_session(client)
    sid = session["id"]
    assert client.post(f"/api/replay/sessions/{sid}/step").json()["current_market_time"] == "2026-01-02T17:01:00+00:00"
    assert client.post(f"/api/replay/sessions/{sid}/step").json()["current_market_time"] == "2026-01-02T17:02:00+00:00"

    wide = create_session(client, advance_step_minutes=2)
    stepped = client.post(f"/api/replay/sessions/{wide['id']}/step").json()
    # A two-minute step reveals the 17:00 and 17:01 candles at once.
    assert stepped["current_market_time"] == "2026-01-02T17:02:00+00:00"
    assert stepped["current_candle_time"] == "2026-01-02T17:01:00+00:00"


def test_api_market_entry_uses_reveal_timestamp(client):
    session = create_session(client)
    sid = session["id"]
    client.post(f"/api/replay/sessions/{sid}/step")
    trade = client.post(f"/api/replay/sessions/{sid}/orders/market", json={
        "direction": "long", "quantity": 1,
    })
    assert trade.status_code == 200, trade.text
    state = client.get(f"/api/replay/sessions/{sid}/state").json()
    item = state["trades"][0]
    entry = state["fills"][-1]
    assert item["entry_time"] == "2026-01-02T17:01:00+00:00"
    assert item["entry_price"] == 1.1013
    assert item["entry_market_price"] == 1.1013
    assert entry["timestamp"] == "2026-01-02T17:01:00+00:00"
    assert entry["time_precision"] == "exact"
    assert entry["execution_window_start"] == entry["execution_window_end"] == entry["timestamp"]


def test_api_manual_exit_uses_reveal_timestamp(client):
    session = create_session(client)
    sid = session["id"]
    client.post(f"/api/replay/sessions/{sid}/step")
    opened = client.post(f"/api/replay/sessions/{sid}/orders/market", json={
        "direction": "long", "quantity": 1,
    })
    trade_id = opened.json()["trades"][-1]["id"]
    client.post(f"/api/replay/sessions/{sid}/step")
    closed = client.post(f"/api/trades/{trade_id}/close", json={"session_id": sid, "quantity": 1})
    assert closed.status_code == 200, closed.text
    state = client.get(f"/api/replay/sessions/{sid}/state").json()
    item = state["trades"][0]
    assert item["status"] == "closed"
    assert item["exit_time"] == "2026-01-02T17:02:00+00:00"
    assert item["exit_time_precision"] == "exact"
    assert item["exit_window_start"] == item["exit_window_end"] == "2026-01-02T17:02:00+00:00"
    assert item["final_exit_reason"] == "manual"
    exit_fill = state["fills"][-1]
    assert exit_fill["reason"] == "manual"
    assert exit_fill["timestamp"] == "2026-01-02T17:02:00+00:00"
    assert exit_fill["time_precision"] == "exact"
    assert exit_fill["execution_window_start"] == exit_fill["execution_window_end"] == exit_fill["timestamp"]


def test_api_session_end_liquidation_uses_final_reveal_time(client):
    session = create_session(client)
    sid = session["id"]
    client.post(f"/api/replay/sessions/{sid}/step")
    opened = client.post(f"/api/replay/sessions/{sid}/orders/market", json={
        "direction": "long", "quantity": 1,
    })
    assert opened.status_code == 200, opened.text
    trade_id = opened.json()["trades"][-1]["id"]
    state = None
    while True:
        state = client.post(f"/api/replay/sessions/{sid}/step").json()
        if state["status"] == "completed":
            break
    closed = client.post(f"/api/replay/sessions/{sid}/close-all")
    assert closed.status_code == 200, closed.text
    state = client.get(f"/api/replay/sessions/{sid}/state").json()
    item = next(t for t in state["trades"] if t["id"] == trade_id)
    assert item["status"] == "closed"
    assert item["exit_time"] == "2026-01-02T17:07:00+00:00"
    assert item["exit_time_precision"] == "exact"
    assert item["final_exit_reason"] == "session_end"
    exit_fill = state["fills"][-1]
    assert exit_fill["reason"] == "session_end"
    assert exit_fill["timestamp"] == "2026-01-02T17:07:00+00:00"
    assert exit_fill["time_precision"] == "exact"
