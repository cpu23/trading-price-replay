from __future__ import annotations

from datetime import datetime

from .domain import Fill, ReplayState, StatsAccumulator, Trade


def book_fill(state: ReplayState, trade_direction: str, fill: Fill) -> None:
    """Book one committed fill into the session accumulator.

    Must be called in the same transaction (same state mutation) that appends
    the fill, so the accumulator can never drift from the fill ledger. The
    realized-balance peak/drawdown are maintained along the fill path, seeded
    with the initial balance on the first booking.
    """
    acc = state.accumulator
    acc.commission_sum += fill.commission
    acc.spread_cost_sum += fill.spread_cost
    acc.slippage_sum += fill.slippage_cost
    acc.gross_pnl_sum += fill.gross_pnl
    acc.net_pnl_sum += fill.pnl
    if trade_direction == "long":
        acc.long_pnl_sum += fill.pnl
    else:
        acc.short_pnl_sum += fill.pnl
    balance = state.initial_balance + acc.net_pnl_sum
    if acc.peak_realized_balance is None:
        acc.peak_realized_balance = state.initial_balance
    acc.peak_realized_balance = max(acc.peak_realized_balance, balance)
    acc.max_realized_drawdown = max(acc.max_realized_drawdown, acc.peak_realized_balance - balance)


def book_trade_close(state: ReplayState, trade: Trade, exit_time: datetime | None) -> None:
    """Book the final close of a trade (remaining quantity reached exactly zero).

    Partial closes never call this; final trade statistics are only booked once
    the trade is actually closed. `exit_time` is the final fill's effective
    execution time; holding duration is measured from the trade's entry time.
    """
    acc = state.accumulator
    acc.trades_completed += 1
    if trade.realized_pnl > 0:
        acc.winning_trades += 1
        acc.winning_pnl_sum += trade.realized_pnl
    elif trade.realized_pnl < 0:
        acc.losing_trades += 1
        acc.losing_pnl_sum += trade.realized_pnl
    if trade.initial_risk:
        r = trade.realized_pnl / trade.initial_risk
        acc.r_count += 1
        acc.r_sum += r
        if r > 0:
            acc.winning_r_count += 1
            acc.winning_r_sum += r
        elif r < 0:
            acc.losing_r_count += 1
            acc.losing_r_sum += r
    if exit_time is not None:
        acc.holding_seconds_sum += (exit_time - trade.entry_time).total_seconds()


def calculate_stats(state: ReplayState, current_market_price: float | None = None,
                    contract_multiplier: float | None = None) -> dict[str, float | int]:
    """Session statistics from the persisted accumulator plus current open risk.

    Historical metrics (P&L, costs, trade counts, R, drawdown, holding) come
    from the incrementally maintained accumulator, so this is independent of
    how long the session's fill history is. Only the current unrealized P&L is
    computed live, from the open trades at the current causal price; it
    deducts projected liquidation costs, keeping equity a liquidation value.
    The max drawdown preserves the historical semantics: the deepest realized
    move, plus the current equity when it sits below the historical peak.
    """
    acc = state.accumulator
    if acc.trades_opened == 0 and state.trades:
        # The accumulator is authoritative only once it has booked trades. A
        # state that carries hydrated history but a zero accumulator — built
        # directly, or a legacy session whose v6 backfill could not parse its
        # snapshot — falls back to the full-history reference calculation so
        # the response never reports an empty session that has trades.
        return calculate_stats_from_history(state, current_market_price, contract_multiplier)
    completed = acc.trades_completed
    net_pnl = acc.net_pnl_sum
    balance = state.initial_balance + net_pnl

    unrealized_pnl = 0.0
    if current_market_price is not None:
        multiplier = contract_multiplier if contract_multiplier is not None else 1.0
        for trade in state.trades:
            if trade.status != "open" or trade.remaining_quantity <= 0:
                continue
            sign = 1 if trade.direction == "long" else -1
            quantity = trade.remaining_quantity
            gross = sign * (current_market_price - trade.entry_market_price) * quantity * multiplier * state.conversion_rate
            commission, spread_cost, slippage_cost = state.fill_costs(quantity, multiplier)
            # Liquidation value: reference gross move minus the estimated exit-side
            # costs on the open remainder (balance already carries the entry costs).
            unrealized_pnl += gross - (commission + spread_cost + slippage_cost)
    equity = balance + unrealized_pnl

    peak = state.initial_balance if acc.peak_realized_balance is None else max(state.initial_balance, acc.peak_realized_balance)
    max_drawdown = max(acc.max_realized_drawdown, peak - equity)

    r_count = acc.r_count
    r_sum = acc.r_sum
    return {
        "trades_opened": acc.trades_opened,
        "trades_completed": completed,
        "win_rate": acc.winning_trades / completed * 100 if completed else 0,
        "net_pnl": net_pnl,
        "gross_pnl": acc.gross_pnl_sum,
        "trading_costs": acc.commission_sum + acc.spread_cost_sum + acc.slippage_sum,
        "commission_paid": acc.commission_sum,
        "spread_cost": acc.spread_cost_sum,
        "slippage_cost": acc.slippage_sum,
        "unrealized_pnl": unrealized_pnl,
        "balance": balance,
        "equity": equity,
        "total_r": r_sum,
        "average_r": r_sum / r_count if r_count else 0,
        "average_win_r": acc.winning_r_sum / acc.winning_r_count if acc.winning_r_count else 0,
        "average_losing_r": acc.losing_r_sum / acc.losing_r_count if acc.losing_r_count else 0,
        "average_win": acc.winning_pnl_sum / acc.winning_trades if acc.winning_trades else 0,
        "average_loss": acc.losing_pnl_sum / acc.losing_trades if acc.losing_trades else 0,
        "profit_factor": acc.winning_pnl_sum / abs(acc.losing_pnl_sum) if acc.losing_pnl_sum else 0,
        "max_drawdown": max_drawdown,
        "long_pnl": acc.long_pnl_sum,
        "short_pnl": acc.short_pnl_sum,
        "average_holding_seconds": acc.holding_seconds_sum / completed if completed else 0,
    }


def calculate_stats_from_history(state: ReplayState, current_market_price: float | None = None,
                                 contract_multiplier: float | None = None) -> dict[str, float | int]:
    """The previous full-history calculation, retained as the reference
    implementation for legacy backfill equivalence checks and tests."""
    closed = [trade for trade in state.trades if trade.status == "closed"]
    wins = [trade.realized_pnl for trade in closed if trade.realized_pnl > 0]
    losses = [trade.realized_pnl for trade in closed if trade.realized_pnl < 0]

    gross_pnl = sum(fill.gross_pnl for fill in state.fills)
    commission_paid = sum(fill.commission for fill in state.fills)
    spread_cost = sum(fill.spread_cost for fill in state.fills)
    slippage_cost = sum(fill.slippage_cost for fill in state.fills)
    trading_costs = commission_paid + spread_cost + slippage_cost
    net_pnl = sum(fill.pnl for fill in state.fills)
    balance = state.initial_balance + net_pnl

    unrealized_pnl = 0.0
    if current_market_price is not None:
        multiplier = contract_multiplier if contract_multiplier is not None else 1.0
        for trade in state.trades:
            if trade.status != "open" or trade.remaining_quantity <= 0:
                continue
            sign = 1 if trade.direction == "long" else -1
            quantity = trade.remaining_quantity
            gross = sign * (current_market_price - trade.entry_market_price) * quantity * multiplier * state.conversion_rate
            commission, spread_cost, slippage_cost = state.fill_costs(quantity, multiplier)
            unrealized_pnl += gross - (commission + spread_cost + slippage_cost)
    equity = balance + unrealized_pnl

    equity_curve = [state.initial_balance]
    for fill in state.fills:
        equity_curve.append(equity_curve[-1] + fill.pnl)
    equity_curve.append(equity)
    peak = max_drawdown = 0.0
    for point in equity_curve:
        peak = max(peak, point)
        max_drawdown = max(max_drawdown, peak - point)

    r_values = [trade.realized_pnl / trade.initial_risk for trade in closed if trade.initial_risk]
    winning_r = [value for value in r_values if value > 0]
    losing_r = [value for value in r_values if value < 0]
    # Final exit time per closed trade: the persisted exit_time, or (legacy
    # rows that recorded none) the trade's last fill timestamp.
    last_fill_time: dict[str, datetime] = {}
    for fill in state.fills:
        last_fill_time[fill.trade_id] = fill.timestamp
    holding_seconds = 0.0
    for trade in closed:
        exit_time = trade.exit_time or last_fill_time.get(trade.id)
        if exit_time is not None:
            holding_seconds += (exit_time - trade.entry_time).total_seconds()
    return {
        "trades_opened": len(state.trades),
        "trades_completed": len(closed),
        "win_rate": len(wins) / len(closed) * 100 if closed else 0,
        "net_pnl": net_pnl,
        "gross_pnl": gross_pnl,
        "trading_costs": trading_costs,
        "commission_paid": commission_paid,
        "spread_cost": spread_cost,
        "slippage_cost": slippage_cost,
        "unrealized_pnl": unrealized_pnl,
        "balance": balance,
        "equity": equity,
        "total_r": sum(r_values),
        "average_r": sum(r_values) / len(r_values) if r_values else 0,
        "average_win_r": sum(winning_r) / len(winning_r) if winning_r else 0,
        "average_losing_r": sum(losing_r) / len(losing_r) if losing_r else 0,
        "average_win": sum(wins) / len(wins) if wins else 0,
        "average_loss": sum(losses) / len(losses) if losses else 0,
        "profit_factor": sum(wins) / abs(sum(losses)) if losses else 0,
        "max_drawdown": max_drawdown,
        "long_pnl": sum(trade.realized_pnl for trade in state.trades if trade.direction == "long"),
        "short_pnl": sum(trade.realized_pnl for trade in state.trades if trade.direction == "short"),
        "average_holding_seconds": holding_seconds / len(closed) if closed else 0,
    }


def build_accumulator_from_history(state: ReplayState, trades: list[Trade],
                                   fills: list[Fill]) -> StatsAccumulator:
    """Reconstruct the accumulator from complete trade/fill history.

    Used by schema migration v6 to backfill legacy sessions exactly once.
    `trades` and `fills` must be in insertion (rowid) order, matching the
    authoritative ledger order: fills are booked in global order so the
    realized-balance path is reproduced exactly, and trade closes are booked
    when each trade's final fill (its last ledger entry) is reached. Bookings
    are written to `state.accumulator` itself, which is installed on the state
    before the first booking and returned.
    """
    acc = StatsAccumulator()
    state.accumulator = acc
    trade_by_id = {trade.id: trade for trade in trades}
    fills_by_trade: dict[str, list[Fill]] = {}
    for fill in fills:
        fills_by_trade.setdefault(fill.trade_id, []).append(fill)
    for trade in trades:
        acc.trades_opened += 1
    for fill in fills:
        trade = trade_by_id.get(fill.trade_id)
        if trade is None:
            continue  # orphan fill: no trade to attribute direction or close to
        book_fill(state, trade.direction, fill)
        if trade.status == "closed" and fills_by_trade[fill.trade_id][-1] is fill:
            book_trade_close(state, trade, fill.timestamp)
    for trade in trades:
        # Degenerate legacy rows: a trade recorded closed without any ledger
        # fill. Its close is still counted (no holding duration is known) so
        # the reconstruction matches the full-history trade counts exactly.
        if trade.status == "closed" and not fills_by_trade.get(trade.id):
            book_trade_close(state, trade, None)
    return acc
