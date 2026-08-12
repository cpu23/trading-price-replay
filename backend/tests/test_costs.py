from dataclasses import fields
from datetime import datetime, timezone
from math import isfinite

import pytest

from app.domain import Bar, Fill, ReplayState, Trade
from app.execution import close_trade, open_trade, process_bar
from app.stats import calculate_stats


def ts(minute: int = 0) -> datetime:
    return datetime(2026, 1, 2, 12, minute, tzinfo=timezone.utc)


def bar(minute: int, open_: float = 10, high: float = 11, low: float = 9, close: float = 10) -> Bar:
    return Bar(ts(minute), open_, high, low, close, 1)


def make_state(**kwargs) -> ReplayState:
    return ReplayState.create(symbol="TEST", start=ts(0), end=ts(59), profile="utc_aligned", **kwargs)


def test_long_entry_fill_is_adverse_and_charges_costs_once():
    state = make_state(initial_balance=10000, spread=0.2, slippage=0.1, commission_per_quantity=2.0)
    trade = open_trade(state, ts(1), 10.0, "long", 2.0, 9.0, 11.0, 1)
    assert len(state.fills) == 1
    fill = state.fills[0]
    assert fill.reason == "entry"
    assert fill.market_price == 10.0
    assert fill.price == pytest.approx(10.2)  # + half spread (0.1) + slippage (0.1)
    assert fill.gross_pnl == 0.0
    assert fill.commission == 4.0
    assert fill.spread_cost == pytest.approx(0.2)
    assert fill.slippage_cost == pytest.approx(0.2)
    assert fill.pnl == pytest.approx(-4.4)
    assert trade.entry_price == pytest.approx(10.2)
    assert trade.entry_market_price == 10.0
    assert trade.remaining_quantity == 2.0
    assert trade.realized_pnl == pytest.approx(-4.4)


def test_short_close_fill_is_adverse():
    state = make_state(spread=0.2, slippage=0.1, commission_per_quantity=1.0)
    trade = open_trade(state, ts(1), 10.0, "short", 1.0, 11.0, 9.0, 1)
    assert state.fills[0].price == pytest.approx(9.8)  # entry: market - half spread - slippage
    assert state.fills[0].pnl == pytest.approx(-1.2)  # entry-side costs booked as realized
    fill = close_trade(state, trade, ts(2), 9.0, 1.0, "manual", 1)
    assert fill.reason == "manual"
    assert fill.market_price == 9.0
    assert fill.price == pytest.approx(9.2)  # exit: market + half spread + slippage
    assert fill.gross_pnl == pytest.approx(1.0)  # reference mid move: -(9.0 - 10.0)
    assert fill.commission == 1.0
    assert fill.spread_cost == pytest.approx(0.1)
    assert fill.slippage_cost == pytest.approx(0.1)
    assert fill.pnl == pytest.approx(-0.2)  # net of this exit's costs only
    assert trade.realized_pnl == pytest.approx(-1.4)  # entry + exit costs
    assert trade.status == "closed"


def test_partial_exits_charge_exit_costs_per_fill_and_entry_once():
    state = make_state(spread=0.2, slippage=0.1, commission_per_quantity=2.0)
    trade = open_trade(state, ts(1), 10.0, "long", 2.0, None, None, 1)
    assert state.fills[0].commission == 4.0  # entry-side, once, full quantity
    first = close_trade(state, trade, ts(2), 11.0, 1.0, "manual", 1)
    assert first.quantity == 1.0
    assert first.commission == 2.0
    assert first.gross_pnl == pytest.approx(1.0)  # reference mid move: 11.0 - 10.0
    assert trade.realized_pnl == pytest.approx(-5.6)  # -4.4 entry + (-1.2) first exit
    assert trade.status == "open"
    second = close_trade(state, trade, ts(3), 12.0, 1.0, "manual", 1)
    assert second.commission == 2.0
    assert trade.status == "closed"
    assert trade.realized_pnl == pytest.approx(-5.8)  # equals sum of all fill pnls
    assert [fill.commission for fill in state.fills] == [4.0, 2.0, 2.0]


def test_tiny_partial_close_keeps_position_open_and_full_close_snaps():
    state = make_state()
    trade = open_trade(state, ts(1), 10.0, "long", 1e-12, None, None, 1)
    close_trade(state, trade, ts(2), 11.0, 1e-13, "manual", 1)
    # A partial exit of a tiny valid position must not read as a full close:
    # an absolute epsilon would zero the 9e-13 remainder and mark it closed.
    assert trade.status == "open"
    assert trade.remaining_quantity == 9e-13
    close_trade(state, trade, ts(3), 12.0, trade.remaining_quantity, "manual", 1)
    assert trade.status == "closed"
    assert trade.remaining_quantity == 0


def test_near_full_partial_close_preserves_remainder():
    state = make_state()
    trade = open_trade(state, ts(1), 10.0, "long", 1.0, None, None, 1)
    close_trade(state, trade, ts(2), 11.0, 0.9999999999995, "manual", 1)
    # A near-full exit is still a partial close: the tiny remainder survives
    # and the position stays open, with exact accounting round-tripping to
    # the initial quantity.
    assert trade.status == "open"
    assert trade.remaining_quantity == pytest.approx(5e-13, rel=1e-3)
    assert trade.remaining_quantity + 0.9999999999995 == 1.0


def test_completed_trade_cost_reconciliation_and_stats():
    state = make_state(initial_balance=1000, spread=0.2, slippage=0.1, commission_per_quantity=1.0)
    trade = open_trade(state, ts(1), 10.0, "long", 2.0, None, None, 1)
    close_trade(state, trade, ts(2), 11.0, 1.0, "manual", 1)
    close_trade(state, trade, ts(3), 12.0, 1.0, "manual", 1)
    gross = sum(fill.gross_pnl for fill in state.fills)
    costs = sum(fill.commission + fill.spread_cost + fill.slippage_cost for fill in state.fills)
    net = sum(fill.pnl for fill in state.fills)
    assert gross - costs == pytest.approx(net)
    assert trade.realized_pnl == pytest.approx(net)  # fully closed: sum of its fills
    stats = calculate_stats(state, current_market_price=12.0, contract_multiplier=1)
    assert stats["gross_pnl"] == pytest.approx(gross)
    assert stats["trading_costs"] == pytest.approx(costs)
    assert stats["net_pnl"] == pytest.approx(net)
    assert stats["balance"] == pytest.approx(1000 + net)
    assert stats["equity"] == pytest.approx(1000 + net)
    assert stats["unrealized_pnl"] == 0.0
    assert stats["trades_opened"] == 1
    assert stats["trades_completed"] == 1


def test_economic_example_mid_move_net_of_adverse_costs():
    # Mid 10 -> 11 with 0.2 adverse per side (spread 0.4, no slippage/commission):
    # gross 1.0, entry cost 0.2, exit cost 0.2 => net 0.6 (not 0.2).
    state = make_state(initial_balance=1000, spread=0.4)
    trade = open_trade(state, ts(1), 10.0, "long", 1.0, None, None, 1)
    entry = state.fills[0]
    assert entry.price == pytest.approx(10.2)
    assert entry.pnl == pytest.approx(-0.2)
    assert trade.realized_pnl == pytest.approx(-0.2)
    mark = calculate_stats(state, current_market_price=11.0, contract_multiplier=1)
    assert mark["balance"] == pytest.approx(999.8)  # entry costs already realized
    assert mark["unrealized_pnl"] == pytest.approx(0.8)  # 1.0 gross move - 0.2 estimated exit cost
    assert mark["equity"] == pytest.approx(1000.6)
    flat = calculate_stats(state, current_market_price=10.0, contract_multiplier=1)
    assert flat["unrealized_pnl"] == pytest.approx(-0.2)
    assert flat["equity"] == pytest.approx(999.6)  # full round-trip loss if liquidated at mid
    fill = close_trade(state, trade, ts(2), 11.0, 1.0, "manual", 1)
    assert fill.price == pytest.approx(10.8)
    assert fill.gross_pnl == pytest.approx(1.0)
    assert fill.pnl == pytest.approx(0.8)  # exit costs subtracted once
    assert trade.realized_pnl == pytest.approx(0.6)
    assert trade.status == "closed"
    stats = calculate_stats(state, current_market_price=11.0, contract_multiplier=1)
    assert stats["net_pnl"] == pytest.approx(0.6)
    assert stats["gross_pnl"] == pytest.approx(1.0)
    assert stats["trading_costs"] == pytest.approx(0.4)
    assert stats["equity"] == pytest.approx(1000.6)
    assert stats["gross_pnl"] - stats["trading_costs"] == pytest.approx(stats["net_pnl"])


def test_costs_scale_with_contract_multiplier_and_conversion():
    state = make_state(spread=0.2, slippage=0.1, commission_per_quantity=3.0, conversion_rate=2.0)
    open_trade(state, ts(1), 10.0, "long", 1.0, None, None, 10)
    fill = state.fills[0]
    assert fill.price == pytest.approx(10.2)
    assert fill.spread_cost == pytest.approx(0.1 * 10 * 2)
    assert fill.slippage_cost == pytest.approx(0.1 * 10 * 2)
    assert fill.commission == 3.0  # account currency per quantity unit, no multiplier
    assert fill.pnl == pytest.approx(-(0.1 * 10 * 2 + 0.1 * 10 * 2 + 3.0))


def test_open_trade_marks_to_market():
    state = make_state(initial_balance=500)
    open_trade(state, ts(1), 10.0, "long", 2.0, None, None, 1)
    stats = calculate_stats(state, current_market_price=11.0, contract_multiplier=1)
    assert stats["unrealized_pnl"] == pytest.approx(2.0)
    assert stats["balance"] == 500.0
    assert stats["equity"] == pytest.approx(502.0)
    assert stats["net_pnl"] == 0.0


def test_stop_gap_fills_at_worse_of_trigger_and_open():
    state = make_state()
    trade = open_trade(state, ts(0), 10.0, "long", 1.0, 9.0, None, 1)
    process_bar(state, bar(1, open_=8.5, high=8.5, low=8.0, close=8.2), 1)
    fill = state.fills[-1]
    assert fill.reason == "stop"
    assert fill.market_price == 8.5  # open gapped below the stop: worse fill
    assert trade.status == "closed"


def test_target_gap_fills_at_better_open():
    state = make_state()
    trade = open_trade(state, ts(0), 10.0, "long", 1.0, None, 11.0, 1)
    process_bar(state, bar(1, open_=12.0, high=12.0, low=11.5, close=11.8), 1)
    fill = state.fills[-1]
    assert fill.reason == "target"
    assert fill.market_price == 12.0  # open gapped above the target: better fill
    assert trade.status == "closed"


def test_short_gap_fills_are_mirrored():
    state = make_state()
    stopped = open_trade(state, ts(0), 10.0, "short", 1.0, 11.0, 9.0, 1)
    process_bar(state, bar(1, open_=11.5, high=12.0, low=11.2, close=11.4), 1)
    assert stopped.status == "closed"
    assert state.fills[-1].reason == "stop"
    assert state.fills[-1].market_price == 11.5  # open gapped above the stop: worse fill
    targeted = open_trade(state, ts(2), 10.0, "short", 1.0, 11.0, 9.0, 1)
    process_bar(state, bar(3, open_=8.5, high=9.2, low=8.0, close=8.4), 1)
    assert targeted.status == "closed"
    assert state.fills[-1].reason == "target"
    assert state.fills[-1].market_price == 8.5  # open gapped below the target: better fill


def test_stop_first_when_both_touch_with_gaps():
    state = make_state()
    trade = open_trade(state, ts(0), 10.0, "long", 1.0, 9.0, 11.0, 1)
    process_bar(state, bar(1, open_=8.5, high=13.0, low=8.0, close=9.0), 1)
    assert trade.status == "closed"
    assert state.fills[-1].reason == "stop"
    assert state.fills[-1].market_price == 8.5


def test_open_and_partial_trades_do_not_distort_completed_stats():
    state = make_state()
    winner = open_trade(state, ts(0), 10.0, "long", 1.0, None, None, 1)
    close_trade(state, winner, ts(1), 11.0, 1.0, "manual", 1)
    partial = open_trade(state, ts(2), 10.0, "short", 2.0, None, None, 1)
    close_trade(state, partial, ts(3), 9.0, 1.0, "manual", 1)
    open_trade(state, ts(4), 10.0, "long", 1.0, None, None, 1)
    stats = calculate_stats(state, current_market_price=9.5, contract_multiplier=1)
    assert stats["trades_opened"] == 3
    assert stats["trades_completed"] == 1
    assert stats["win_rate"] == 100.0
    assert stats["average_win"] == pytest.approx(1.0)
    assert stats["average_loss"] == 0.0
    assert stats["profit_factor"] == 0.0  # no completed losses
    assert stats["total_r"] == 0.0  # winner carries no stop, hence no R value
    assert stats["long_pnl"] == pytest.approx(1.0)
    assert stats["short_pnl"] == pytest.approx(1.0)
    assert stats["unrealized_pnl"] == pytest.approx(0.0)  # short remainder +0.5 offsets open long -0.5 at 9.5
    assert stats["balance"] == pytest.approx(10002.0)
    assert stats["equity"] == pytest.approx(10002.0)


def test_zero_cost_behavior_has_entry_fill_and_unchanged_net():
    state = make_state()
    trade = open_trade(state, ts(0), 10.0, "long", 1.0, 9.0, 11.0, 1)
    process_bar(state, bar(1, high=12, low=8), 1)
    assert [fill.reason for fill in state.fills] == ["entry", "stop"]
    entry, stop = state.fills
    assert entry.price == 10.0
    assert entry.market_price == 10.0
    assert entry.pnl == 0.0
    assert stop.price == 9.0
    assert stop.gross_pnl == -1.0
    stats = calculate_stats(state)
    assert stats["net_pnl"] == -1.0
    assert stats["gross_pnl"] == -1.0
    assert stats["trading_costs"] == 0.0
    assert trade.realized_pnl == -1.0


def test_drawdown_includes_entry_costs_and_mark():
    state = make_state(initial_balance=100, spread=0.2, slippage=0.1, commission_per_quantity=1.0)
    open_trade(state, ts(1), 10.0, "long", 1.0, None, None, 1)
    stats = calculate_stats(state, current_market_price=10.0, contract_multiplier=1)
    assert stats["unrealized_pnl"] == pytest.approx(-1.2)  # estimated exit costs on open remainder
    assert stats["equity"] == pytest.approx(97.6)  # liquidation value: entry + exit costs
    assert stats["max_drawdown"] == pytest.approx(2.4)


def test_legacy_session_loads_with_cost_defaults():
    state = ReplayState(
        id="s1", symbol="TEST", start=ts(0), end=ts(59), profile="utc_aligned",
    )
    assert state.initial_balance == 10000.0
    assert state.spread == 0.0
    assert state.slippage == 0.0
    assert state.commission_per_quantity == 0.0
    assert state.conversion_rate == 1.0
    assert state.contract_multiplier is None  # legacy: service falls back to symbol metadata
    assert state.data_version is None  # legacy: reads the retained 1m dataset


def test_legacy_fill_and_trade_load_with_defaults():
    trade = Trade(
        id="t1", session_id="s1", direction="long", initial_quantity=1, remaining_quantity=0,
        entry_time=ts(0), entry_price=10.0, realized_pnl=-1.0, status="closed",
    )
    assert trade.entry_market_price == 10.0
    fill = Fill(
        id="f1", trade_id="t1", session_id="s1", timestamp=ts(1), price=10.0, quantity=1.0,
        reason="manual", pnl=-1.0,
    )
    assert fill.market_price == 10.0
    assert fill.gross_pnl == -1.0
    assert fill.commission == 0.0
    assert fill.spread_cost == 0.0
    assert fill.slippage_cost == 0.0


def test_invalid_cost_configuration_is_rejected():
    with pytest.raises(ValueError):
        make_state(initial_balance=0)
    with pytest.raises(ValueError):
        make_state(spread=-1)
    with pytest.raises(ValueError):
        make_state(slippage=-0.5)
    with pytest.raises(ValueError):
        make_state(commission_per_quantity=-1)
    with pytest.raises(ValueError):
        make_state(initial_balance=float("nan"))
    with pytest.raises(ValueError):
        make_state(initial_balance=float("inf"))
    with pytest.raises(ValueError):
        make_state(spread=float("nan"))
    with pytest.raises(ValueError):
        make_state(slippage=float("inf"))
    with pytest.raises(ValueError):
        make_state(commission_per_quantity=float("-inf"))


def test_open_trade_rejects_unknown_direction():
    state = make_state()
    with pytest.raises(ValueError):
        open_trade(state, ts(1), 10.0, "sideways", 1.0, None, None, 1)


def test_conversion_rate_must_be_finite_positive():
    for bad in (0, -1, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            make_state(conversion_rate=bad)


def test_contract_multiplier_must_be_finite_positive_when_set():
    for bad in (0, -1, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            make_state(contract_multiplier=bad)
    state = make_state(contract_multiplier=10)
    assert state.contract_multiplier == 10


def test_engine_inputs_are_validated():
    state = make_state()
    # Prices may be any finite value (instruments can trade at zero or below);
    # quantities and the contract multiplier must be finite positive.
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            open_trade(state, ts(1), bad, "long", 1.0, None, None, 1)
        with pytest.raises(ValueError):
            open_trade(state, ts(1), 10.0, "long", 1.0, bad, None, 1)
        with pytest.raises(ValueError):
            open_trade(state, ts(1), 10.0, "long", 1.0, None, bad, 1)
    for bad in (0, -1, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            open_trade(state, ts(1), 10.0, "long", bad, None, None, 1)
        with pytest.raises(ValueError):
            open_trade(state, ts(1), 10.0, "long", 1.0, None, None, bad)
    trade = open_trade(state, ts(1), 10.0, "long", 1.0, 9.0, 11.0, 1)
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            close_trade(state, trade, ts(2), bad, 1.0, "manual", 1)
    for bad in (0, -1, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            close_trade(state, trade, ts(2), 10.0, bad, "manual", 1)
    assert trade.remaining_quantity == 1.0  # rejected calls never mutated state


def test_negative_prices_execute_normally():
    state = make_state()
    trade = open_trade(state, ts(1), -5.0, "long", 1.0, -6.0, -4.0, 1)
    assert trade.entry_market_price == -5.0
    assert trade.entry_price == -5.0
    # The open gaps through the negative target; the stop is touched later intrabar.
    process_bar(state, bar(1, open_=-3.5, high=-3.5, low=-6.5, close=-4.0), 1)
    assert trade.status == "closed"
    assert state.fills[-1].reason == "target"
    assert state.fills[-1].market_price == -3.5  # contracted gap price at the open
    assert state.fills[-1].pnl == pytest.approx(1.5)


def test_overflowing_derived_values_are_rejected():
    state = make_state()
    with pytest.raises(ValueError):
        open_trade(state, ts(1), 10.0, "long", 1e308, None, None, 1e308)
    trade = open_trade(state, ts(1), 10.0, "long", 1.0, None, None, 1)
    with pytest.raises(ValueError):
        close_trade(state, trade, ts(2), 11.0, 1e308, "manual", 1e308)
    assert len(state.trades) == 1  # second trade never opened
    assert trade.status == "open"


def test_process_bar_rejects_non_finite_bar_prices():
    state = make_state()
    trade = open_trade(state, ts(0), 10.0, "long", 1.0, 9.0, 11.0, 1)
    for bad in (float("nan"), float("inf"), float("-inf")):
        for field in ("open_", "high", "low", "close"):
            kwargs = {"open_": 10, "high": 11, "low": 9, "close": 10, field: bad}
            with pytest.raises(ValueError):
                process_bar(state, bar(1, **kwargs), 1)
    assert trade.status == "open"  # rejected bars never closed anything


def test_stored_fills_and_trades_are_finite():
    state = make_state(spread=0.2, slippage=0.1, commission_per_quantity=1.0)
    trade = open_trade(state, ts(1), 10.0, "long", 2.0, 9.0, 11.0, 1)
    close_trade(state, trade, ts(2), 11.0, 1.0, "manual", 1)
    for obj in [trade, *state.fills]:
        for field in fields(obj):
            value = getattr(obj, field.name)
            if isinstance(value, float):
                assert isfinite(value), f"{type(obj).__name__}.{field.name} not finite"
