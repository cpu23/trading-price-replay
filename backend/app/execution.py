from __future__ import annotations

from datetime import datetime
from math import isfinite
from uuid import uuid4

from .domain import Bar, Fill, ReplayState, Trade


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
               stop_price: float | None, target_price: float | None, contract_multiplier: float) -> Trade:
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
                  target_price, risk, entry_market_price=price)
    state.trades.append(trade)
    # Entry carries no gross P&L; its net is the negative of the entry-side costs,
    # charged exactly once and booked into realized P&L immediately.
    trade.realized_pnl = 0.0 - costs
    state.fills.append(Fill(
        id=str(uuid4()), trade_id=trade.id, session_id=state.id, timestamp=now,
        price=execution_price, quantity=quantity, reason="entry",
        pnl=0.0 - costs, market_price=price, gross_pnl=0.0,
        commission=commission, spread_cost=spread_cost, slippage_cost=slippage_cost,
    ))
    return trade


def close_trade(state: ReplayState, trade: Trade, now: datetime, price: float, quantity: float,
                reason: str, contract_multiplier: float) -> Fill:
    _require_finite(price, "price")
    _require_finite_positive(quantity, "quantity")
    _require_finite_positive(contract_multiplier, "contract_multiplier")
    if quantity > trade.remaining_quantity:
        raise ValueError("close quantity must be positive and no greater than remaining quantity")
    sign = 1 if trade.direction == "long" else -1
    execution_price = _execution_price(state, price, trade.direction, is_entry=False)
    # Gross P&L is measured at reference (mid) prices; the fill's own costs are
    # subtracted once below, so the adverse exit adjustment is not double-counted.
    gross_pnl = sign * (price - trade.entry_market_price) * quantity * contract_multiplier * state.conversion_rate
    commission, spread_cost, slippage_cost = state.fill_costs(quantity, contract_multiplier)
    net_pnl = gross_pnl - (commission + spread_cost + slippage_cost)
    if not all(isfinite(value) for value in (execution_price, gross_pnl, commission, spread_cost, slippage_cost, net_pnl)):
        raise ValueError("derived exit values must be finite")
    fill = Fill(
        id=str(uuid4()), trade_id=trade.id, session_id=state.id, timestamp=now,
        price=execution_price, quantity=quantity, reason=reason,
        pnl=net_pnl, market_price=price, gross_pnl=gross_pnl,
        commission=commission, spread_cost=spread_cost, slippage_cost=slippage_cost,
    )
    trade.remaining_quantity -= quantity
    trade.realized_pnl += net_pnl
    # A trade closes only when the requested quantity exactly matches the
    # pre-close remainder (the subtraction then yields exact zero). Any true
    # partial close preserves its remainder regardless of how small it is.
    if trade.remaining_quantity == 0:
        trade.status = "closed"
    state.fills.append(fill)
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
            close_trade(state, trade, bar.timestamp, target_fill, trade.remaining_quantity, "target", contract_multiplier)
        elif stop_fill is not None and _stop_crossed_at_open(trade, bar):
            close_trade(state, trade, bar.timestamp, stop_fill, trade.remaining_quantity, "stop", contract_multiplier)
        elif stop_fill is not None:
            close_trade(state, trade, bar.timestamp, stop_fill, trade.remaining_quantity, "stop", contract_multiplier)
        else:
            close_trade(state, trade, bar.timestamp, target_fill, trade.remaining_quantity, "target", contract_multiplier)
