from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import config, market_data, repository
from app.domain import Fill, ReplayState, Trade
from app.main import app
from app.stats import build_accumulator_from_history
from app.service import MAX_RESPONSE_CLOSED_TRADES, MAX_RESPONSE_FILLS, _snapshot_response


def ts(minute: int = 0) -> datetime:
    return datetime(2026, 1, 2, 12, 0, tzinfo=timezone.utc) + timedelta(minutes=minute)


def build_big_state(closed_count: int = MAX_RESPONSE_CLOSED_TRADES + 50,
                    fills_per_closed: int = 6, open_count: int = 3) -> ReplayState:
    """A session whose history exceeds both response bounds, with open trades that
    must never be capped."""
    state = ReplayState.create(
        symbol="TEST", start=ts(0), end=ts(59), profile="utc_aligned",
        contract_multiplier=1.0, price_precision=5, pnl_currency="USD",
    )
    for index in range(closed_count + open_count):
        trade_id = str(uuid4())
        open_ = index >= closed_count
        trade = Trade(
            id=trade_id, session_id=state.id, direction="long", initial_quantity=1.0,
            remaining_quantity=0.0 if not open_ else 1.0, entry_time=ts(index),
            entry_price=10.0, entry_market_price=10.0, realized_pnl=0.0,
            status="open" if open_ else "closed",
        )
        state.trades.append(trade)
        state.fills.append(Fill(
            id=str(uuid4()), trade_id=trade_id, session_id=state.id, timestamp=ts(index),
            price=10.0, quantity=1.0, reason="entry", pnl=-0.5, market_price=10.0,
            gross_pnl=0.0, commission=0.0, spread_cost=0.0, slippage_cost=0.0,
        ))
        if not open_:
            for fill_index in range(fills_per_closed - 1):
                state.fills.append(Fill(
                    id=str(uuid4()), trade_id=trade_id, session_id=state.id,
                    timestamp=ts(index + fill_index + 1), price=10.0, quantity=0.2,
                    reason="manual", pnl=1.0, market_price=10.0, gross_pnl=1.0,
                    commission=0.0, spread_cost=0.0, slippage_cost=0.0,
                ))
    return state


def test_response_caps_history_but_keeps_every_open_trade():
    state = build_big_state()
    response = _snapshot_response(state, [], [])

    open_ids = [trade.id for trade in state.trades if trade.status == "open"]
    returned_open = [trade for trade in response["trades"] if trade["status"] == "open"]
    returned_closed = [trade for trade in response["trades"] if trade["status"] == "closed"]
    assert [trade["id"] for trade in returned_open] == open_ids
    assert len(returned_closed) == MAX_RESPONSE_CLOSED_TRADES
    # The capped tail keeps the most recent closed trades.
    expected_tail = [trade.id for trade in state.trades if trade.status == "closed"][-MAX_RESPONSE_CLOSED_TRADES:]
    assert [trade["id"] for trade in returned_closed] == expected_tail
    # Honest totals and truncation flags.
    assert response["closed_trades_total"] == 250
    assert response["closed_trades_truncated"] is True
    assert len(response["fills"]) == MAX_RESPONSE_FILLS
    assert response["fills_total"] == len(state.fills) == 1503
    assert response["fills_truncated"] is True
    # The most recent fills are kept.
    assert response["fills"][-1]["id"] == state.fills[-1].id
    # Statistics still cover the full history, not the capped arrays.
    assert response["stats"]["net_pnl"] == sum(fill.pnl for fill in state.fills)
    assert response["stats"]["trades_completed"] == 250


def test_response_within_bounds_reports_no_truncation():
    state = ReplayState.create(
        symbol="TEST", start=ts(0), end=ts(59), profile="utc_aligned", contract_multiplier=1.0,
    )
    trade_id = str(uuid4())
    state.trades.append(Trade(
        id=trade_id, session_id=state.id, direction="long", initial_quantity=1.0,
        remaining_quantity=1.0, entry_time=ts(0), entry_price=10.0, entry_market_price=10.0,
        status="open",
    ))
    state.fills.append(Fill(
        id=str(uuid4()), trade_id=trade_id, session_id=state.id, timestamp=ts(0),
        price=10.0, quantity=1.0, reason="entry", pnl=-0.5, market_price=10.0,
    ))
    response = _snapshot_response(state, [], [])
    assert response["closed_trades_total"] == 0
    assert response["closed_trades_truncated"] is False
    assert response["fills_total"] == 1
    assert response["fills_truncated"] is False
    assert len(response["trades"]) == 1
    assert len(response["fills"]) == 1


@pytest.fixture()
def db_paths(tmp_path, monkeypatch):
    for module in (config, market_data, repository):
        for name, relative in (("RAW_ROOT", "raw"), ("OHLCV_ROOT", "ohlcv"), ("DB_PATH", "sessions/db.sqlite3")):
            if hasattr(module, name):
                monkeypatch.setattr(module, name, tmp_path / relative)
    market_data.invalidate_bars()
    repository.initialize()
    return tmp_path


def _count_row_writes(monkeypatch) -> list[int]:
    """Count INSERT/UPDATE statements against the trades/fills tables during a call."""
    counter = [0]
    original_connect = repository.connect

    def counting_connect():
        connection = original_connect()
        connection.set_trace_callback(lambda sql: _bump_if_row_write(sql, counter))
        return connection

    monkeypatch.setattr(repository, "connect", counting_connect)
    return counter


def _bump_if_row_write(sql: str | None, counter: list[int]) -> None:
    if sql is None:
        return
    statement = sql.upper()
    if statement.startswith(("INSERT", "UPDATE")) and ("INTO TRADES" in statement or "INTO FILLS" in statement):
        counter[0] += 1


def test_persisted_history_stays_complete_and_repeated_saves_skip_immutable_rows(db_paths, monkeypatch):
    state = build_big_state()
    repository.save_session(state, "first_save")
    loaded = repository.load_session(state.id)
    assert loaded is not None
    # Persistence is complete in the tables; a routine load hydrates only the
    # working set (every open trade + bounded recent windows) plus totals.
    assert len(loaded.trades) == MAX_RESPONSE_CLOSED_TRADES + 3
    assert len(loaded.fills) == MAX_RESPONSE_FILLS
    assert loaded.closed_trades_total == 250
    assert loaded.fills_total == 1503

    # A second save with an unchanged state writes no trades/fills rows at all.
    counter = _count_row_writes(monkeypatch)
    repository.save_session(state, "second_save")
    assert counter[0] == 0
    with repository.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM trades WHERE session_id=?", (state.id,)).fetchone()[0] == 253
        assert db.execute("SELECT COUNT(*) FROM fills WHERE session_id=?", (state.id,)).fetchone()[0] == 1503

    # A changed trade is the only trades row rewritten.
    state.trades[0].realized_pnl += 0.25
    counter = _count_row_writes(monkeypatch)
    repository.save_session(state, "third_save")
    assert counter[0] == 1
    reloaded = repository.load_session(state.id)
    # The changed row is a closed trade outside the hydrated window: verify it
    # through the single-row query, not the working set.
    assert repository.get_trade(state.id, state.trades[0].id).realized_pnl == state.trades[0].realized_pnl
    assert len(reloaded.trades) == MAX_RESPONSE_CLOSED_TRADES + 3
    assert len(reloaded.fills) == MAX_RESPONSE_FILLS


def test_rolled_back_save_never_advances_row_tracking(db_paths, monkeypatch):
    from app.repository import StaleSessionError, _PERSISTED_ROWS

    state = build_big_state(closed_count=5, fills_per_closed=2, open_count=1)
    repository.save_session(state, "first_save")
    assert _PERSISTED_ROWS[state.id][0] == 1
    # Simulate a concurrent writer bumping the stored revision behind our back.
    with repository.connect() as db:
        db.execute("UPDATE replay_sessions SET revision=revision+1 WHERE id=?", (state.id,))
    with pytest.raises(StaleSessionError):
        repository.save_session(state, "stale_save")
    # The tracking still claims revision 1, so a save from a freshly loaded
    # revision-2 state does a full rewrite instead of skipping rows that a
    # concurrent writer may have changed.
    assert _PERSISTED_ROWS[state.id][0] == 1
    fresh = repository.load_session(state.id)
    assert fresh.revision == 2
    counter = _count_row_writes(monkeypatch)
    repository.save_session(fresh, "reloaded_save")
    assert counter[0] == 17  # 6 trades + 11 fills rewritten in full
    assert _PERSISTED_ROWS[state.id][0] == 3


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


def test_api_returns_bounded_history_with_totals_and_full_stats(client):
    state = build_big_state()
    state.symbol = "EURUSD"
    # Simulate the schema-v6 backfill: a legacy session's persisted snapshot
    # carries the accumulator and exact history totals rebuilt from its full
    # ledger history, so the stats endpoint reports full-session numbers without
    # scanning history.
    state.accumulator = build_accumulator_from_history(state, state.trades, state.fills)
    state.closed_trades_total = sum(1 for t in state.trades if t.status == "closed")
    state.fills_total = len(state.fills)
    full_history_net_pnl = sum(fill.pnl for fill in state.fills)
    repository.save_session(state, "seeded")
    body = client.get(f"/api/replay/sessions/{state.id}/state").json()
    assert body["closed_trades_total"] == 250
    assert body["closed_trades_truncated"] is True
    assert body["fills_total"] == 1503
    assert body["fills_truncated"] is True
    assert len(body["trades"]) == MAX_RESPONSE_CLOSED_TRADES + 3
    assert len(body["fills"]) == MAX_RESPONSE_FILLS
    assert all(trade["status"] == "open" for trade in body["trades"][:3])
    # The stats endpoint reports full-session numbers, not the capped arrays.
    stats = client.get(f"/api/replay/sessions/{state.id}/stats").json()
    assert stats["trades_completed"] == 250
    assert stats["net_pnl"] == full_history_net_pnl
