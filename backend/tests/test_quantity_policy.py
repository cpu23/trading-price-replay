"""Centralized partial-close quantity policy.

Verifies that every close path resolves the executed fill quantity and the
stored remainder through the single policy in ``app.quantity``: decimal-grid
subtraction removes ordinary float drift, an ULP-scaled window absorbs only
representational overshoot/residual (never an absolute or relative epsilon),
scale-sensitive tiny remainders stay open, a tolerated final close books the
actual pre-close remainder and stores exactly 0.0, true oversize is rejected,
and fresh fill quantities plus the stored remainder reconcile to the initial
quantity.
"""
import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app import config, repository
from app.domain import Fill, ReplayState, Trade
from app.execution import close_trade, open_trade
from app.quantity import resolve_close
from app.repository import initialize, load_session, save_session
from app.stats import build_accumulator_from_history, calculate_stats, calculate_stats_from_history


def ts(minute: int = 0) -> datetime:
    return datetime(2026, 1, 2, 12, minute, tzinfo=timezone.utc)


def make_state(**kwargs) -> ReplayState:
    return ReplayState.create(symbol="TEST", start=ts(0), end=ts(59), profile="utc_aligned", **kwargs)


def displayed(value: float) -> str:
    """What the API wire format shows for a stored float quantity."""
    return json.dumps(value)


def dec(value: float) -> Decimal:
    return Decimal(str(value))


def sum_fill_quantities(fills) -> Decimal:
    return sum((dec(fill.quantity) for fill in fills), dec(0.0))


@pytest.fixture()
def db(tmp_path, monkeypatch):
    for module in (config, repository):
        for name, relative in (("RAW_ROOT", "raw"), ("OHLCV_ROOT", "ohlcv"), ("DB_PATH", "sessions/db.sqlite3")):
            if hasattr(module, name):
                monkeypatch.setattr(module, name, tmp_path / relative)
    initialize()


def test_resolve_close_classifies_the_window():
    # Exact matches and float-representational residuals are final closes.
    assert resolve_close(0.1, 0.1) == 0.0
    assert resolve_close(0.3333333333333334, 0.3333333333333333) == 0.0
    # Genuine remainders, however small, are preserved.
    assert resolve_close(1.0, 0.9999999999995) == pytest.approx(5e-13, rel=1e-3)
    assert resolve_close(1e-12, 1e-13) == 9e-13
    assert resolve_close(1e-323, 5e-324) == 5e-324
    # True oversize (beyond the ULP window) is rejected.
    with pytest.raises(ValueError):
        resolve_close(0.3, 0.4)
    with pytest.raises(ValueError):
        resolve_close(0.3, 0.3000000001)
    with pytest.raises(ValueError):
        resolve_close(5e-324, 1e-321)


def test_zero_point_three_closed_in_tenths_displays_clean_remainders():
    state = make_state()
    trade = open_trade(state, ts(1), 10.0, "long", 0.3, None, None, 1)
    close_trade(state, trade, ts(2), 10.5, 0.1, "manual", 1)
    # Float arithmetic would leave 0.19999999999999998; the decimal grid
    # stores the float nearest to 0.2, which the wire format renders as 0.2.
    assert trade.status == "open"
    assert trade.remaining_quantity == pytest.approx(0.2)
    assert displayed(trade.remaining_quantity) == "0.2"
    close_trade(state, trade, ts(3), 10.5, 0.1, "manual", 1)
    assert displayed(trade.remaining_quantity) == "0.1"
    close_trade(state, trade, ts(4), 10.5, 0.1, "manual", 1)
    assert trade.status == "closed"
    assert trade.remaining_quantity == 0.0


def test_displayed_remainder_completes_position():
    state = make_state()
    trade = open_trade(state, ts(1), 10.0, "long", 0.3, None, None, 1)
    close_trade(state, trade, ts(2), 10.5, 0.1, "manual", 1)

    displayed_remainder = float(displayed(trade.remaining_quantity))
    assert displayed_remainder == 0.2
    final_fill = close_trade(state, trade, ts(3), 10.5, displayed_remainder, "manual", 1)

    assert final_fill.quantity == 0.2
    assert trade.status == "closed"
    assert trade.remaining_quantity == 0.0


def test_three_tenths_closes_complete_the_trade():
    state = make_state()
    trade = open_trade(state, ts(1), 10.0, "long", 0.3, None, None, 1)
    for minute in (2, 3, 4):
        close_trade(state, trade, ts(minute), 10.5, 0.1, "manual", 1)
    # The position completes only on the third close, with the remainder
    # stored exactly at 0.0.
    assert [fill.quantity for fill in state.fills[1:]] == [0.1, 0.1, 0.1]
    assert trade.status == "closed"
    assert trade.remaining_quantity == 0.0
    assert trade.final_exit_reason == "manual"
    assert state.closed_trades_total == 1


def test_repeated_thirds_tolerate_final_residual():
    state = make_state()
    trade = open_trade(state, ts(1), 10.0, "long", 1.0, None, None, 1)
    third = 0.3333333333333333
    close_trade(state, trade, ts(2), 10.5, third, "manual", 1)
    close_trade(state, trade, ts(3), 10.5, third, "manual", 1)
    assert trade.status == "open"
    # Two thirds in leaves a remainder of 0.3333333333333334 (float storage
    # artifact); the third close matches it within the ULP window, books the
    # actual pre-close remainder, and stores exactly 0.0.
    assert trade.remaining_quantity == pytest.approx(0.3333333333333334)
    fill = close_trade(state, trade, ts(4), 10.5, third, "manual", 1)
    assert trade.status == "closed"
    assert trade.remaining_quantity == 0.0
    assert fill.quantity == pytest.approx(0.3333333333333334)
    # Fill quantities plus the stored remainder reconcile to the initial
    # quantity in decimal arithmetic.
    assert sum_fill_quantities(state.fills[1:]) + dec(0.0) == dec(trade.initial_quantity)


def test_full_partial_and_true_oversize():
    state = make_state()
    trade = open_trade(state, ts(1), 10.0, "long", 1.0, None, None, 1)
    # Full close.
    close_trade(state, trade, ts(2), 10.5, 1.0, "manual", 1)
    assert trade.status == "closed"
    assert trade.remaining_quantity == 0.0

    state = make_state()
    trade = open_trade(state, ts(1), 10.0, "long", 1.0, None, None, 1)
    # Partial close keeps the exact decimal remainder.
    close_trade(state, trade, ts(2), 10.5, 0.4, "manual", 1)
    assert trade.status == "open"
    assert trade.remaining_quantity == pytest.approx(0.6)
    assert displayed(trade.remaining_quantity) == "0.6"

    # True oversize is rejected: 0.4 on 0.3, and any request genuinely beyond
    # the ULP window, without mutating state.
    fills_before = len(state.fills)
    with pytest.raises(ValueError):
        close_trade(state, trade, ts(3), 10.5, 0.7, "manual", 1)
    assert trade.status == "open"
    assert trade.remaining_quantity == pytest.approx(0.6)
    assert len(state.fills) == fills_before


def test_tiny_legitimate_remainders_stay_open():
    state = make_state()
    trade = open_trade(state, ts(1), 10.0, "long", 1e-12, None, None, 1)
    close_trade(state, trade, ts(2), 11.0, 1e-13, "manual", 1)
    # An absolute epsilon would zero the 9e-13 remainder and mark the trade
    # closed; the ULP-scaled window (scaled to these tiny operands) preserves it.
    assert trade.status == "open"
    assert trade.remaining_quantity == 9e-13
    close_trade(state, trade, ts(3), 12.0, trade.remaining_quantity, "manual", 1)
    assert trade.status == "closed"
    assert trade.remaining_quantity == 0.0

    state = make_state()
    trade = open_trade(state, ts(1), 10.0, "long", 1.0, None, None, 1)
    close_trade(state, trade, ts(2), 11.0, 0.9999999999995, "manual", 1)
    assert trade.status == "open"
    assert trade.remaining_quantity == pytest.approx(5e-13, rel=1e-3)
    close_trade(state, trade, ts(3), 12.0, trade.remaining_quantity, "manual", 1)
    assert trade.status == "closed"
    assert trade.remaining_quantity == 0.0


def test_tolerated_overshoot_books_pre_close_remainder():
    # A request a hair above the stored remainder (float-storage overshoot)
    # is the intended final close: the fill books the actual pre-close
    # remainder and the stored remainder becomes exactly 0.0.
    state = make_state()
    trade = open_trade(state, ts(1), 10.0, "long", 0.3, None, None, 1)
    fill = close_trade(state, trade, ts(2), 10.5, 0.30000000000000004, "manual", 1)
    assert trade.status == "closed"
    assert trade.remaining_quantity == 0.0
    assert fill.quantity == pytest.approx(0.3)


def test_long_and_short_close_symmetry():
    for direction, stop, target in (("long", 9.0, 11.0), ("short", 11.0, 9.0)):
        state = make_state()
        trade = open_trade(state, ts(1), 10.0, direction, 0.3, stop, target, 1)
        for minute in (2, 3, 4):
            close_trade(state, trade, ts(minute), 10.5, 0.1, "manual", 1)
        assert trade.status == "closed"
        assert trade.remaining_quantity == 0.0
        assert [fill.quantity for fill in state.fills[1:]] == [0.1, 0.1, 0.1]
        # Short closes above the entry lose: the signed gross P&L is correct
        # and every fill net is realized into the trade.
        assert trade.realized_pnl == pytest.approx(sum(fill.pnl for fill in state.fills))


def test_costs_are_booked_on_the_executed_quantity():
    state = make_state(spread=0.2, slippage=0.1, commission_per_quantity=2.0)
    trade = open_trade(state, ts(1), 10.0, "long", 0.3, None, None, 1)
    # Entry costs are charged once on the full initial quantity.
    assert state.fills[0].commission == pytest.approx(2.0 * 0.3)
    first = close_trade(state, trade, ts(2), 11.0, 0.1, "manual", 1)
    assert first.commission == pytest.approx(2.0 * 0.1)
    close_trade(state, trade, ts(3), 11.5, 0.1, "manual", 1)
    third = close_trade(state, trade, ts(4), 12.0, 0.1, "manual", 1)
    # The tolerated final close books the actual pre-close remainder (0.1),
    # so its costs and P&L scale with exactly what was executed.
    assert third.quantity == pytest.approx(0.1)
    assert third.commission == pytest.approx(2.0 * 0.1)
    assert trade.status == "closed"
    assert trade.total_commission == pytest.approx(sum(fill.commission for fill in state.fills))
    assert trade.total_spread_cost == pytest.approx(sum(fill.spread_cost for fill in state.fills))
    assert trade.total_slippage_cost == pytest.approx(sum(fill.slippage_cost for fill in state.fills))
    assert trade.realized_pnl == pytest.approx(sum(fill.pnl for fill in state.fills))


def test_reload_round_trip_preserves_policy(db):
    state = make_state()
    save_session(state, "session_started")
    trade = open_trade(state, ts(1), 10.0, "long", 0.3, None, None, 1)
    close_trade(state, trade, ts(2), 10.5, 0.1, "manual", 1)
    save_session(state, "position_closed", {"trade_id": trade.id, "quantity": 0.1})

    loaded = load_session(state.id)
    loaded_trade = loaded.trades[0]
    assert loaded_trade.status == "open"
    assert displayed(loaded_trade.remaining_quantity) == "0.2"
    # The policy continues on the hydrated floats: two more 0.1 closes land
    # on exactly 0.0 and persist a closed trade with a zero remainder.
    close_trade(loaded, loaded_trade, ts(3), 10.5, 0.1, "manual", 1)
    close_trade(loaded, loaded_trade, ts(4), 10.5, 0.1, "manual", 1)
    assert loaded_trade.status == "closed"
    assert loaded_trade.remaining_quantity == 0.0
    save_session(loaded, "position_closed", {"trade_id": loaded_trade.id, "quantity": 0.1})

    reloaded = load_session(state.id)
    assert reloaded.trades[0].status == "closed"
    assert reloaded.trades[0].remaining_quantity == 0.0
    assert reloaded.trades[0].final_exit_reason == "manual"


def test_close_against_stale_remainder_is_rejected():
    state = make_state()
    trade = open_trade(state, ts(1), 10.0, "long", 0.3, None, None, 1)
    close_trade(state, trade, ts(2), 10.5, 0.1, "manual", 1)
    # A second client computed its close against the pre-close remainder of
    # 0.3; against the current 0.2 remainder that is a true oversize and is
    # rejected without any state mutation.
    fills_before = len(state.fills)
    with pytest.raises(ValueError):
        close_trade(state, trade, ts(3), 10.5, 0.3, "manual", 1)
    assert trade.status == "open"
    assert trade.remaining_quantity == pytest.approx(0.2)
    assert len(state.fills) == fills_before
    assert state.closed_trades_total == 0
    # A close against the current remainder still completes.
    close_trade(state, trade, ts(4), 10.5, 0.2, "manual", 1)
    assert trade.status == "closed"
    assert trade.remaining_quantity == 0.0


def test_legacy_dust_remainder_closes_like_fresh():
    # A legacy session hydrated a float-dust remainder produced by old float
    # subtraction (0.6 - 0.1 - 0.1 - 0.1 leaves 0.30000000000000004 in binary
    # floats, while the legacy close fills were booked at 0.1 each). The
    # floats load without migration, and a close of the displayed 0.3 is the
    # tolerated final close: the fill books the actual dust remainder (never
    # fabricating quantity) and the stored remainder snaps to exactly 0.0.
    state = make_state()
    trade = Trade(
        id="t1", session_id=state.id, direction="long",
        initial_quantity=0.6, remaining_quantity=0.30000000000000004,
        entry_time=ts(1), entry_price=10.0, entry_market_price=10.0,
        realized_pnl=-1.0, status="open",
        total_commission=0.0, total_spread_cost=0.0, total_slippage_cost=0.0,
    )
    state.trades.append(trade)
    state.fills.append(Fill(
        id="f0", trade_id=trade.id, session_id=state.id, timestamp=ts(1),
        price=10.0, quantity=0.6, reason="entry", pnl=-1.0, market_price=10.0,
        gross_pnl=0.0, commission=0.0, spread_cost=0.0, slippage_cost=0.0,
    ))
    for index in range(3):
        state.fills.append(Fill(
            id=f"f{index + 1}", trade_id=trade.id, session_id=state.id, timestamp=ts(2 + index),
            price=10.1, quantity=0.1, reason="manual", pnl=0.0, market_price=10.1,
            gross_pnl=0.0, commission=0.0, spread_cost=0.0, slippage_cost=0.0,
        ))
    state.accumulator = build_accumulator_from_history(state, [trade], [fill for fill in state.fills])
    fill = close_trade(state, trade, ts(5), 10.5, 0.3, "manual", 1)
    assert trade.status == "closed"
    assert trade.remaining_quantity == 0.0
    # The tolerated close books the actual pre-close remainder — it never
    # fabricates quantity — and the stored remainder snaps to exactly 0.0.
    assert fill.quantity == pytest.approx(0.30000000000000004)
    assert fill.quantity == pytest.approx(trade.initial_quantity - sum(f.quantity for f in state.fills[1:4]))
    assert dec(trade.remaining_quantity) == dec(0.0)


def test_close_all_with_dust_remainder_completes(db):
    state = make_state()
    save_session(state, "session_started")
    trade = open_trade(state, ts(1), 10.0, "long", 0.3, None, None, 1)
    close_trade(state, trade, ts(2), 10.5, 0.1, "manual", 1)
    # close_all passes the stored remainder itself: an exact final close.
    fill = close_trade(state, trade, ts(3), 10.5, trade.remaining_quantity, "manual", 1)
    assert trade.status == "closed"
    assert trade.remaining_quantity == 0.0
    assert fill.quantity == pytest.approx(0.2)
    assert sum_fill_quantities(state.fills[1:]) + dec(0.0) == dec(trade.initial_quantity)


def test_accumulator_completion_and_reconciliation():
    state = make_state(initial_balance=1000, spread=0.2, slippage=0.1, commission_per_quantity=1.0)
    trade = open_trade(state, ts(1), 10.0, "long", 0.3, None, None, 1)
    for minute in (2, 3, 4):
        close_trade(state, trade, ts(minute), 11.0, 0.1, "manual", 1)
    # The completion is booked exactly once on the tolerated final close.
    assert state.accumulator.trades_opened == 1
    assert state.accumulator.trades_completed == 1
    inc = calculate_stats(state, current_market_price=11.0, contract_multiplier=1)
    hist = calculate_stats_from_history(state, current_market_price=11.0, contract_multiplier=1)
    assert inc["trades_completed"] == hist["trades_completed"] == 1
    assert inc["net_pnl"] == pytest.approx(hist["net_pnl"])
    # The reconstructed accumulator matches the incrementally booked one, so
    # backfilled legacy sessions report identical completion.
    rebuilt = build_accumulator_from_history(state, [trade], [fill for fill in state.fills])
    assert rebuilt.trades_completed == state.accumulator.trades_completed
    # Reconciliation: fill quantities plus the stored remainder (exactly 0.0)
    # equal the initial quantity, and realized P&L equals the fill sum.
    assert sum_fill_quantities(state.fills[1:]) + dec(0.0) == dec(trade.initial_quantity)
    assert trade.realized_pnl == pytest.approx(sum(fill.pnl for fill in state.fills))
    assert inc["net_pnl"] == pytest.approx(sum(fill.pnl for fill in state.fills))
