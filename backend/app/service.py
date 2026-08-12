from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timezone

from .domain import ReplayState, serializable, state_snapshot
from .execution import close_trade, open_trade, process_bar
from .indicators import sma
from .market_data import RangeBars, bars_signature, load_bars_before
from .repository import get_symbol, load_session, save_session
from .stats import calculate_stats
from .timeframes import MINUTES, resample

# The chart response is a bounded sliding window: the last `chart_context_1m_bars` causal 1m bars
# ending at the current market time. SMA 35 needs the last 35 displayed buckets of the visible
# timeframe for every displayed bucket, so the indicator source extends the chart window by 36
# periods of the visible timeframe (bucket alignment margin included). Both tails stay bounded as
# the replay advances; the full revealed history is never resent or resampled.
_WARMUP_PERIODS = 36

# Response-history bounds: every open trade is always included so open risk stays
# actionable, while only the closed-trade and fill history is capped. The frontend
# renders honest totals from `*_total` and flags truncation via `*_truncated`.
# Persisted state is never trimmed — sessions reload complete history for statistics.
MAX_RESPONSE_CLOSED_TRADES = 200
MAX_RESPONSE_FILLS = 1000

# Per-session (before, replay) cache, invalidated by the published-file signature (any re-import
# changes file mtimes/sizes, and import_file also invalidates explicitly). Restart-safe: purely
# in-memory and always revalidated against the current signature.
_SESSION_BARS_CACHE: OrderedDict[str, tuple] = OrderedDict()
_SESSION_BARS_CACHE_MAX = 8


class SessionNotFoundError(ValueError):
    pass


class TradeNotFoundError(ValueError):
    pass


def create_session(symbol: str, start: datetime, end: datetime, profile: str | None, visible_timeframe: str,
                   advance_step_minutes: int, chart_context_1m_bars: int, account_currency: str,
                   conversion_rate: float, initial_balance: float = 10000.0, spread: float = 0.0,
                   slippage: float = 0.0, commission_per_quantity: float = 0.0) -> dict[str, object]:
    metadata = get_symbol(symbol.strip().upper())
    if not metadata:
        raise ValueError("unknown symbol")
    symbol = metadata["symbol"]
    data_version = metadata.get("data_version")
    start, end = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    # Validate through the lazy reader's partition counts: an invalid or empty
    # range is rejected without materializing any bars.
    if len(RangeBars(symbol, start, end, data_version)) == 0:
        raise ValueError("selected range is invalid or contains no data")
    selected_profile = profile or str(metadata["default_profile"]) or "utc_aligned"
    if selected_profile not in ("utc_aligned", "new_york_close"):
        raise ValueError("custom session anchors are not implemented in v1")
    if not 500 <= chart_context_1m_bars <= 2000:
        raise ValueError("chart context must be between 500 and 2000")
    if advance_step_minutes <= 0 or conversion_rate <= 0:
        raise ValueError("step size and conversion rate must be positive")
    if initial_balance <= 0:
        raise ValueError("initial balance must be positive")
    if spread < 0 or slippage < 0 or commission_per_quantity < 0:
        raise ValueError("spread, slippage and commission per quantity must be non-negative")
    state = ReplayState.create(
        symbol=symbol, start=start, end=end, profile=selected_profile, visible_timeframe=visible_timeframe,
        advance_step_minutes=advance_step_minutes, chart_context_1m_bars=chart_context_1m_bars,
        account_currency=account_currency, conversion_rate=conversion_rate,
        initial_balance=initial_balance, spread=spread, slippage=slippage,
        commission_per_quantity=commission_per_quantity,
        contract_multiplier=float(metadata["contract_multiplier"]), data_version=data_version,
        price_precision=int(metadata["price_precision"]), pnl_currency=str(metadata["pnl_currency"]),
    )
    save_session(state, "session_started")
    return state_response(state)


def _context_window_bars(state: ReplayState) -> int:
    """One-minute bars needed for the chart context plus the indicator warmup."""
    return state.chart_context_1m_bars + _WARMUP_PERIODS * MINUTES[state.visible_timeframe]


def session_bars(state: ReplayState) -> tuple[list, RangeBars]:
    """Causal bars for a session: bounded `before` window plus the lazy session `replay`.

    `before` is the last `_context_window_bars` one-minute bars strictly before
    the session start, read by actual bar count rather than by subtracting wall-
    clock minutes, so weekends and accepted gaps no longer erase available
    history. `replay` is the session range [start, end] as a lazy, paged
    `RangeBars` sequence: `len(replay)`, integer indexing and slicing all work,
    but only the pages actually touched are ever read from disk, so long replay
    ranges are never fully loaded or cached. Both read only the year partitions
    they need from the session's pinned dataset version and cache per session; a
    re-import publishes a new version and never changes the bars or cursor
    meaning of an existing session.
    """
    window = _context_window_bars(state)
    key = (state.symbol, state.data_version, state.start, state.end, window,
           bars_signature(state.symbol, state.data_version))
    cached = _SESSION_BARS_CACHE.get(state.id)
    if cached is not None and cached[0] == key:
        _SESSION_BARS_CACHE.move_to_end(state.id)
        return cached[1], cached[2]
    before = load_bars_before(state.symbol, state.start, window, state.data_version)
    replay = RangeBars(state.symbol, state.start, state.end, state.data_version)
    _SESSION_BARS_CACHE[state.id] = (key, before, replay)
    _SESSION_BARS_CACHE.move_to_end(state.id)
    while len(_SESSION_BARS_CACHE) > _SESSION_BARS_CACHE_MAX:
        _SESSION_BARS_CACHE.popitem(last=False)
    return before, replay


def _tail(before: list, replay: list, current_index: int, window: int) -> list:
    """The last `window` causal 1m bars ending at the current market time, bounded regardless
    of how far the replay has advanced. Revealed bars are only ever sliced up to `window`."""
    if current_index < 0:
        return before[-window:]
    total_before = len(before)
    if total_before + current_index + 1 <= window:
        return before + replay[: current_index + 1]
    start = total_before + current_index + 1 - window
    if start >= total_before:
        return replay[start - total_before: current_index + 1]
    return before[start:] + replay[: current_index + 1]


def _current_bar(before: list, replay: list, current_index: int):
    last_index = min(current_index, len(replay) - 1)
    if last_index >= 0:
        return replay[last_index]
    return before[-1] if before else None


def _contract_multiplier(state: ReplayState) -> float:
    """Session-pinned multiplier; only legacy sessions (None) fall back to current symbol metadata."""
    if state.contract_multiplier is not None:
        return state.contract_multiplier
    metadata = get_symbol(state.symbol)
    return float(metadata["contract_multiplier"]) if metadata else 1.0


def _state_response(state: ReplayState, before: list, replay: list) -> dict[str, object]:
    current_index = state.current_index
    source = _tail(before, replay, current_index, state.chart_context_1m_bars)
    displayed = resample(source, state.visible_timeframe, state.profile)
    # Snapshot without the normalized histories: full trade/fill lists are only
    # ever serialized here as the capped response arrays below, never in full.
    response = state_snapshot(state)
    # Bound the response history: keep every open trade, cap closed trades and
    # fills to the most recent entries, and report honest totals/truncation.
    open_trades = [trade for trade in state.trades if trade.status == "open"]
    closed_trades = [trade for trade in state.trades if trade.status == "closed"]
    response["trades"] = serializable(open_trades + closed_trades[-MAX_RESPONSE_CLOSED_TRADES:])
    response["fills"] = serializable(state.fills[-MAX_RESPONSE_FILLS:])
    response["closed_trades_total"] = len(closed_trades)
    response["fills_total"] = len(state.fills)
    response["closed_trades_truncated"] = len(closed_trades) > MAX_RESPONSE_CLOSED_TRADES
    response["fills_truncated"] = len(state.fills) > MAX_RESPONSE_FILLS
    current_bar = _current_bar(before, replay, current_index)
    response["current_market_time"] = current_bar.timestamp.isoformat() if current_bar else None
    response["current_price"] = current_bar.close if current_bar else None
    response["displayed_bars"] = serializable(displayed)
    indicator_source = _tail(before, replay, current_index, _context_window_bars(state))
    indicator_bars = resample(indicator_source, state.visible_timeframe, state.profile)
    response["indicators"] = {"sma_close_35": sma(indicator_bars)} if "sma_close_35" in state.enabled_indicators else {}
    response["warnings"] = []
    if "sma_close_35" in state.enabled_indicators and len(indicator_bars) < 35:
        response["warnings"].append("SMA 35 has insufficient causal warmup history")
    multiplier = _contract_multiplier(state)
    current_price = response["current_price"]
    response["stats"] = (
        calculate_stats(state, current_price, multiplier) if current_price is not None else calculate_stats(state)
    )
    response["remaining_bars"] = max(0, len(replay) - current_index - 1)
    return response


def state_response(state: ReplayState) -> dict[str, object]:
    before, replay = session_bars(state)
    return _state_response(state, before, replay)


def get_state(session_id: str) -> ReplayState:
    state = load_session(session_id)
    if not state:
        raise SessionNotFoundError("unknown session")
    return state


def step(state: ReplayState) -> dict[str, object]:
    if state.status == "completed":
        raise ValueError("session is completed")
    multiplier = _contract_multiplier(state)
    before, replay = session_bars(state)
    for _ in range(state.advance_step_minutes):
        next_index = state.current_index + 1
        if next_index >= len(replay):
            state.status = "completed"
            break
        state.current_index = next_index
        process_bar(state, replay[next_index], multiplier)
    if state.current_index >= len(replay) - 1:
        state.status = "completed"
    save_session(state, "replay_stepped", {"step": state.advance_step_minutes})
    return _state_response(state, before, replay)


def update_settings(state: ReplayState, visible_timeframe: str | None, advance_step_minutes: int | None) -> dict[str, object]:
    if visible_timeframe:
        state.visible_timeframe = visible_timeframe
    if advance_step_minutes is not None:
        if advance_step_minutes <= 0:
            raise ValueError("step size must be positive")
        state.advance_step_minutes = advance_step_minutes
    save_session(state, "settings_changed")
    return state_response(state)


def toggle_indicator(state: ReplayState, indicator_id: str) -> dict[str, object]:
    if indicator_id != "sma_close_35":
        raise ValueError("unknown indicator")
    if indicator_id in state.enabled_indicators:
        state.enabled_indicators.remove(indicator_id)
    else:
        state.enabled_indicators.append(indicator_id)
    save_session(state, "indicator_toggled", {"indicator_id": indicator_id})
    return state_response(state)


def market_order(state: ReplayState, direction: str, quantity: float, stop_price: float | None,
                 target_price: float | None) -> dict[str, object]:
    multiplier = _contract_multiplier(state)
    before, replay = session_bars(state)
    if state.status == "completed":
        raise ValueError("session is completed")
    if state.current_index < 0:
        raise ValueError("no causal market price is available until the first replay bar is revealed")
    current_bar = _current_bar(before, replay, state.current_index)
    if current_bar is None:
        raise ValueError("no causal market price is available")
    trade = open_trade(state, current_bar.timestamp, float(current_bar.close), direction, quantity,
                       stop_price, target_price, multiplier)
    save_session(state, "order_filled", {"direction": direction, "quantity": quantity},
                 orders=[{"trade_id": trade.id, "order_type": "market_entry",
                          "payload": {"direction": direction, "quantity": quantity}}])
    return _state_response(state, before, replay)


def close_position(state: ReplayState, trade_id: str, quantity: float) -> dict[str, object]:
    multiplier = _contract_multiplier(state)
    before, replay = session_bars(state)
    trade = next((item for item in state.trades if item.id == trade_id and item.status == "open"), None)
    if not trade:
        raise TradeNotFoundError("open trade not found")
    current_bar = _current_bar(before, replay, state.current_index)
    if current_bar is None:
        raise ValueError("no causal market price is available")
    close_trade(state, trade, current_bar.timestamp, float(current_bar.close), quantity, "manual", multiplier)
    save_session(state, "position_closed", {"trade_id": trade_id, "quantity": quantity},
                 orders=[{"trade_id": trade.id, "order_type": "market_close", "payload": {"quantity": quantity}}])
    return _state_response(state, before, replay)


def close_all_positions(state: ReplayState) -> dict[str, object]:
    multiplier = _contract_multiplier(state)
    before, replay = session_bars(state)
    open_trades = [item for item in state.trades if item.status == "open"]
    current_bar = _current_bar(before, replay, state.current_index)
    if open_trades and current_bar is None:
        raise ValueError("no causal market price is available")
    # An active close-all is a manual exit; a completed session's close-all is the
    # session-end liquidation that lets the frontend settle remaining positions.
    reason = "session_end" if state.status == "completed" else "manual"
    orders = []
    if open_trades:
        for trade in open_trades:
            quantity = trade.remaining_quantity
            close_trade(state, trade, current_bar.timestamp, float(current_bar.close), quantity, reason, multiplier)
            orders.append({"trade_id": trade.id, "order_type": "close_all",
                           "payload": {"quantity": quantity, "reason": reason}})
    save_session(state, "positions_closed_all", {"closed": len(open_trades)}, orders=orders)
    return _state_response(state, before, replay)
