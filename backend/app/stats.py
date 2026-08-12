from __future__ import annotations

from .domain import ReplayState


def calculate_stats(state: ReplayState, current_market_price: float | None = None,
                    contract_multiplier: float | None = None) -> dict[str, float | int]:
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
            # Liquidation value: reference gross move minus the estimated exit-side
            # costs on the open remainder (balance already carries the entry costs).
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
        "average_win": sum(wins) / len(wins) if wins else 0,
        "average_loss": sum(losses) / len(losses) if losses else 0,
        "profit_factor": sum(wins) / abs(sum(losses)) if losses else 0,
        "max_drawdown": max_drawdown,
        "long_pnl": sum(trade.realized_pnl for trade in state.trades if trade.direction == "long"),
        "short_pnl": sum(trade.realized_pnl for trade in state.trades if trade.direction == "short"),
    }
