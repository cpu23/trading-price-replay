from __future__ import annotations

from datetime import datetime
from math import isfinite
from uuid import uuid4

from .domain import Bar, Fill, ReplayState, TimePrecision, Trade, bar_reveal_time
from .quantity import resolve_close
from .stats import book_fill, book_trade_close


def _require_finite(value: float, name: str) -> None:
    if not isfinite(value):
        raise ValueError(f"{name} must be a finite number")


def _require_finite_positive(value: float, name: str) -> None:
    if not isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite positive number")


def _execution_price(state: ReplayState, market_price: float, direction: str, is_entry: bool) -> float:
    """Actual fill price: adverse half-spread plus slippage applied to the market price."""
    adverse = state.spread / 2.0 + state.slippage
    if direction == "long":
        return market_price + adverse if is_entry else market_price - adverse
    return market_price - adverse if is_entry else market_price + adverse


def open_trade(state: ReplayState, now: datetime, price: float, direction: str, quantity: float,
               stop_price: float | None, target_price: float | None, contract_multiplier: float,
               source_candle_time: datetime | None = None) -> Trade:
    """Open a market trade at a causally revealed price.

    `now` is the caller's causal execution time: for a market entry this is
    the reveal time of the latest revealed candle (its close), so the entry
    fill and the trade's entry time carry the exact close timestamp.
    `source_candle_time` is the candle whose revealed close generated the
    entry — the chart anchor for the entry marker (its open time, one minute
    before `now`); the execution timestamp itself is never repurposed for
    chart alignment.
    """
    _require_finite(price, "price")
    _require_finite_positive(quantity, "quantity")
    _require_finite_positive(contract_multiplier, "contract_multiplier")
    if direction not in ("long", "short"):
        raise ValueError("direction must be 'long' or 'short'")
    if stop_price is not None:
        _require_finite(stop_price, "stop_price")
    if target_price is not None:
        _require_finite(target_price, "target_price")
    if direction == "long" and ((stop_price is not None and stop_price >= price) or (target_price is not None and target_price <= price)):
        raise ValueError("long stop must be below and target above the current price")
    if direction == "short" and ((stop_price is not None and stop_price <= price) or (target_price is not None and target_price >= price)):
        raise ValueError("short stop must be above and target below the current price")
    execution_price = _execution_price(state, price, direction, is_entry=True)
    risk = abs(price - stop_price) * quantity * contract_multiplier * state.conversion_rate if stop_price is not None else None
    commission, spread_cost, slippage_cost = state.fill_costs(quantity, contract_multiplier)
    costs = commission + spread_cost + slippage_cost
    derived = [execution_price, commission, spread_cost, slippage_cost, costs]
    if risk is not None:
        derived.append(risk)
    if not all(isfinite(value) for value in derived):
        raise ValueError("derived entry values must be finite")
    trade = Trade(str(uuid4()), state.id, direction, quantity, quantity, now, execution_price, stop_price,
                  target_price, risk, entry_market_price=price,
                  mfe_gross_pnl=0.0, mae_gross_pnl=0.0,
                  mfe_close_price_delta=0.0, mae_close_price_delta=0.0,
                  total_commission=commission, total_spread_cost=spread_cost, total_slippage_cost=slippage_cost,
                  entry_source_candle_time=source_candle_time)
    state.trades.append(trade)
    # Entry carries no gross P&L; its net is the negative of the entry-side costs,
    # charged exactly once and booked into realized P&L immediately.
    trade.realized_pnl = 0.0 - costs
    state.fills.append(Fill(
        id=str(uuid4()), trade_id=trade.id, session_id=state.id, timestamp=now,
        price=execution_price, quantity=quantity, reason="entry",
        pnl=0.0 - costs, market_price=price, gross_pnl=0.0,
        commission=commission, spread_cost=spread_cost, slippage_cost=slippage_cost,
        time_precision="exact", execution_window_start=now, execution_window_end=now,
        source_candle_time=source_candle_time,
    ))
    state.fills_total += 1
    state.accumulator.trades_opened += 1
    book_fill(state, direction, state.fills[-1])
    return trade


def close_trade(state: ReplayState, trade: Trade, now: datetime, price: float, quantity: float,
                reason: str, contract_multiplier: float,
                precision: TimePrecision = "exact",
                window_start: datetime | None = None,
                window_end: datetime | None = None,
                source_candle_time: datetime | None = None) -> Fill:
    """Close `quantity` of a trade at a causally revealed price.

    `now` plus `precision`/`window_start`/`window_end` express the known
    execution-time precision: exact fills (market orders at a revealed close,
    opening-gap stops/targets) collapse the window onto `now`; bar_interval
    fills (ordinary intrabar stop/target touches) keep `now` as the effective
    ordering time (the candle open) and expose the candle interval as the
    execution window. `source_candle_time` is the candle the execution
    belongs to for chart rendering (the touched candle's open for
    stop/target fills, the revealed candle for manual closes); the execution
    timestamp is never repurposed for chart alignment. The executed quantity
    and the stored remainder are resolved here through the centralized
    quantity policy (`resolve_close`), so every close path — manual, close-all,
    stop/target — inherits the same semantics: a close is final when the
    requested quantity matches the stored remainder exactly or within the
    ULP-scaled tolerance for representational residual/overshoot; the fill
    then books the actual pre-close remainder, the stored remainder is set to
    exactly 0.0, and the trade's final-exit metadata and trade-level
    statistics are persisted. A partial close outside the representational
    window preserves its remainder at any quantity scale.
    """
    _require_finite(price, "price")
    _require_finite_positive(quantity, "quantity")
    _require_finite_positive(contract_multiplier, "contract_multiplier")
    remaining_quantity = resolve_close(trade.remaining_quantity, quantity)
    final_close = remaining_quantity == 0.0
    # A final close books the actual pre-close remainder: the request may
    # differ from it only by float-storage artifacts. A partial close books
    # exactly the requested quantity.
    executed_quantity = trade.remaining_quantity if final_close else quantity
    # Exact fills collapse the window onto the exact timestamp (callers that
    # omit the window get this for free; bar_interval fills must pass it).
    if precision == "exact" and window_start is None:
        window_start = window_end = now
    sign = 1 if trade.direction == "long" else -1
    execution_price = _execution_price(state, price, trade.direction, is_entry=False)
    # Gross P&L is measured at reference (mid) prices; the fill's own costs are
    # subtracted once below, so the adverse exit adjustment is not double-counted.
    gross_pnl = sign * (price - trade.entry_market_price) * executed_quantity * contract_multiplier * state.conversion_rate
    commission, spread_cost, slippage_cost = state.fill_costs(executed_quantity, contract_multiplier)
    net_pnl = gross_pnl - (commission + spread_cost + slippage_cost)
    if not all(isfinite(value) for value in (execution_price, gross_pnl, commission, spread_cost, slippage_cost, net_pnl)):
        raise ValueError("derived exit values must be finite")
    fill = Fill(
        id=str(uuid4()), trade_id=trade.id, session_id=state.id, timestamp=now,
        price=execution_price, quantity=executed_quantity, reason=reason,
        pnl=net_pnl, market_price=price, gross_pnl=gross_pnl,
        commission=commission, spread_cost=spread_cost, slippage_cost=slippage_cost,
        time_precision=precision, execution_window_start=window_start, execution_window_end=window_end,
        source_candle_time=source_candle_time,
    )
    trade.remaining_quantity = remaining_quantity
    trade.realized_pnl += net_pnl
    trade.total_commission += commission
    trade.total_spread_cost += spread_cost
    trade.total_slippage_cost += slippage_cost
    if final_close:
        trade.status = "closed"
        trade.exit_market_price = price
        trade.exit_price = execution_price
        trade.exit_time = now
        trade.exit_time_precision = precision
        trade.exit_window_start = window_start
        trade.exit_window_end = window_end
        trade.final_exit_reason = reason
    state.fills.append(fill)
    state.fills_total += 1
    book_fill(state, trade.direction, fill)
    if final_close:
        state.closed_trades_total += 1
        book_trade_close(state, trade, now)
    return fill


def _stop_fill_price(trade: Trade, bar: Bar) -> float | None:
    if trade.stop_price is None:
        return None
    if trade.direction == "long" and bar.low <= trade.stop_price:
        return min(trade.stop_price, bar.open)
    if trade.direction == "short" and bar.high >= trade.stop_price:
        return max(trade.stop_price, bar.open)
    return None


def _target_fill_price(trade: Trade, bar: Bar) -> float | None:
    if trade.target_price is None:
        return None
    if trade.direction == "long" and bar.high >= trade.target_price:
        return max(trade.target_price, bar.open)
    if trade.direction == "short" and bar.low <= trade.target_price:
        return min(trade.target_price, bar.open)
    return None


def _stop_crossed_at_open(trade: Trade, bar: Bar) -> bool:
    if trade.stop_price is None:
        return False
    if trade.direction == "long":
        return bar.open <= trade.stop_price
    return bar.open >= trade.stop_price


def _target_crossed_at_open(trade: Trade, bar: Bar) -> bool:
    if trade.target_price is None:
        return False
    if trade.direction == "long":
        return bar.open >= trade.target_price
    return bar.open <= trade.target_price


def process_bar(state: ReplayState, bar: Bar, contract_multiplier: float) -> None:
    """Apply one newly revealed M1 candle to every open trade.

    Execution times carry their precision explicitly: a level crossed by the
    opening gap executes exactly at the candle's opening timestamp
    (`time_precision="exact"`), while an ordinary intrabar touch is only known
    to lie within the candle interval, so the fill keeps the candle open as
    its effective ordering timestamp and exposes the interval as the
    execution window (`time_precision="bar_interval"`).
    """
    for name in ("open", "high", "low", "close"):
        _require_finite(getattr(bar, name), f"bar {name}")
    for trade in [item for item in state.trades if item.status == "open"]:
        stop_fill = _stop_fill_price(trade, bar)
        target_fill = _target_fill_price(trade, bar)
        if stop_fill is None and target_fill is None:
            continue
        # A level crossed by the opening gap executes first at its contracted
        # gap price: a target gap can beat a stop touched later in the same
        # candle. Conservative stop-first applies only to ambiguous intrabar
        # touches where the open left both levels valid.
        if target_fill is not None and _target_crossed_at_open(trade, bar):
            close_trade(state, trade, bar.timestamp, target_fill, trade.remaining_quantity, "target",
                        contract_multiplier, precision="exact",
                        window_start=bar.timestamp, window_end=bar.timestamp,
                        source_candle_time=bar.timestamp)
        elif stop_fill is not None and _stop_crossed_at_open(trade, bar):
            close_trade(state, trade, bar.timestamp, stop_fill, trade.remaining_quantity, "stop",
                        contract_multiplier, precision="exact",
                        window_start=bar.timestamp, window_end=bar.timestamp,
                        source_candle_time=bar.timestamp)
        elif stop_fill is not None:
            close_trade(state, trade, bar.timestamp, stop_fill, trade.remaining_quantity, "stop",
                        contract_multiplier, precision="bar_interval",
                        window_start=bar.timestamp, window_end=bar_reveal_time(bar),
                        source_candle_time=bar.timestamp)
        else:
            close_trade(state, trade, bar.timestamp, target_fill, trade.remaining_quantity, "target",
                        contract_multiplier, precision="bar_interval",
                        window_start=bar.timestamp, window_end=bar_reveal_time(bar),
                        source_candle_time=bar.timestamp)


def update_close_excursions(state: ReplayState, bar: Bar, contract_multiplier: float) -> None:
    """Track close-based MAE/MFE for open trades from one revealed candle close.

    Must run after `process_bar` for the same candle, so a close-based
    excursion never includes a candle whose intrabar stop/target already
    closed the trade. The stored basis is the close-to-entry *price* delta
    (signed from the trade's perspective, against the reference entry price);
    the gross P&L projections scale that delta by the trade's *initial*
    quantity, so partial exits never change the reported excursions. OHLC
    resolution only, not an exact intrabar path. A trade whose excursion was
    never measured (legacy) starts from this first observed close.
    """
    for trade in state.trades:
        if trade.status != "open" or trade.remaining_quantity <= 0:
            continue
        delta = (bar.close - trade.entry_market_price) if trade.direction == "long" else (trade.entry_market_price - bar.close)
        if trade.mfe_close_price_delta is None:
            trade.mfe_close_price_delta = delta
            trade.mae_close_price_delta = delta
        else:
            trade.mfe_close_price_delta = max(trade.mfe_close_price_delta, delta)
            trade.mae_close_price_delta = min(trade.mae_close_price_delta, delta)
        scale = trade.initial_quantity * contract_multiplier * state.conversion_rate
        trade.mfe_gross_pnl = trade.mfe_close_price_delta * scale
        trade.mae_gross_pnl = trade.mae_close_price_delta * scale
