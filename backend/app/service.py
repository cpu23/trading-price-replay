from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timedelta, timezone

from .domain import ReplayState, Trade, bar_reveal_time, serializable, state_snapshot
from .execution import close_trade, open_trade, process_bar, update_close_excursions
from .indicators import sma
from .market_data import RangeBars, bars_signature, load_bars_before
from .repository import (_trade_fingerprint, get_last_fill_anchor, get_symbol, get_trade,
                         get_trade_fills, get_trade_reviews, list_fills, list_trades,
                         load_state_cached, save_session, upsert_trade_review)
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
# Persisted state is never trimmed — the normalized tables keep the full history.
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


def _as_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include an explicit UTC offset")
    return value.astimezone(timezone.utc)


def create_session(symbol: str, start: datetime, end: datetime, profile: str | None, visible_timeframe: str,
                   advance_step_minutes: int, chart_context_1m_bars: int, account_currency: str,
                   conversion_rate: float, initial_balance: float = 10000.0, spread: float = 0.0,
                   slippage: float = 0.0, commission_per_quantity: float = 0.0) -> dict[str, object]:
    metadata = get_symbol(symbol.strip().upper())
    if not metadata:
        raise ValueError("unknown symbol")
    symbol = metadata["symbol"]
    data_version = metadata.get("data_version")
    start, end = _as_utc(start, "start"), _as_utc(end, "end")
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


def _trade_wire(trades: list[Trade]) -> list[dict[str, object]]:
    """Serialize trades for the wire: hydrate review notes/tags (one bounded
    batch query) and normalize legacy rows — a missing exit precision renders
    as ``legacy``, and a missing chart anchor falls back to the recorded entry
    time, which for legacy rows was already the source candle's open."""
    trade_dicts = serializable(list(trades))
    reviews = get_trade_reviews([trade.id for trade in trades])
    for item in trade_dicts:
        note, tags = reviews.get(item["id"], ("", []))
        item["review_note"] = note
        item["review_tags"] = tags
        # A legacy closed trade recorded no exit precision: normalize null to
        # "legacy" so the wire contract stays a closed vocabulary. Open trades
        # keep null (no exit has happened yet).
        if item["status"] == "closed" and item["exit_time_precision"] is None:
            item["exit_time_precision"] = "legacy"
        if item["entry_source_candle_time"] is None:
            item["entry_source_candle_time"] = item["entry_time"]
    return trade_dicts


def _fill_wire(fills) -> list[dict[str, object]]:
    """Serialize fills for the wire, normalizing legacy rows: a missing
    precision renders as ``legacy`` and a missing chart anchor falls back to
    the recorded timestamp, which for legacy rows was already the source
    candle's open."""
    fill_dicts = serializable(list(fills))
    for item in fill_dicts:
        if item["time_precision"] is None:
            item["time_precision"] = "legacy"
        if item["source_candle_time"] is None:
            item["source_candle_time"] = item["timestamp"]
    return fill_dicts


class _WorkingSet:
    """A session's in-memory history before a mutation, for building the
    delta: the open set with fingerprints, the closed ids, and the fill count."""

    __slots__ = ("open_ids", "open_fingerprints", "closed_ids", "fill_count")

    def __init__(self, state: ReplayState) -> None:
        self.open_ids: set[str] = set()
        self.open_fingerprints: dict[str, tuple] = {}
        self.closed_ids: set[str] = set()
        for trade in state.trades:
            if trade.status == "open":
                self.open_ids.add(trade.id)
                self.open_fingerprints[trade.id] = _trade_fingerprint(trade)
            else:
                self.closed_ids.add(trade.id)
        self.fill_count = len(state.fills)


def _capture_delta(state: ReplayState, working_set: _WorkingSet) -> dict[str, list]:
    """The mutation's trade/fill delta, read from the in-memory state BEFORE
    `save_session` prunes the working set back to the response caps.

    At exactly the cap, a fill appended by the mutation (or a close-all that
    pushes the closed window past its cap) is pruned in the same call that
    commits it; building the delta from the state afterwards would see an
    empty tail and the client would never receive the mutation. Holds domain
    objects; the response layer serializes them.
    """
    upserts: list[Trade] = []
    removed: list[str] = []
    newly_closed: list[Trade] = []
    for trade in state.trades:
        if trade.id in working_set.open_ids:
            if trade.status == "closed":
                removed.append(trade.id)
                newly_closed.append(trade)
            elif working_set.open_fingerprints[trade.id] != _trade_fingerprint(trade):
                upserts.append(trade)
        elif trade.status == "open":
            # A trade opened by this mutation was not in the pre-mutation open
            # set: the client has never seen it, so it is upserted whole.
            upserts.append(trade)
    return {
        "trade_upserts": upserts,
        "trade_removals_from_open": removed,
        "newly_closed_trades": newly_closed,
        "new_fills": list(state.fills[working_set.fill_count:]),
    }


def _rendered(state: ReplayState, before: list, replay: list) -> dict[str, object]:
    """The shared rendered core of a snapshot/update: scalars, bounded chart,
    indicators, warnings, stats, and history totals. The internal accumulator
    (persistence bookkeeping) never reaches the wire."""
    current_index = state.current_index
    source = _tail(before, replay, current_index, state.chart_context_1m_bars)
    displayed = resample(source, state.visible_timeframe, state.profile)
    response = state_snapshot(state)
    response.pop("accumulator")
    response["trades"] = []
    response["fills"] = []
    # Persisted totals are authoritative (maintained incrementally, recomputed
    # from the tables once for legacy snapshots at load); the max() keeps a
    # directly constructed in-memory state honest.
    closed_trades = [trade for trade in state.trades if trade.status == "closed"]
    closed_total = max(state.closed_trades_total, len(closed_trades))
    fills_total = max(state.fills_total, len(state.fills))
    response["closed_trades_total"] = closed_total
    response["fills_total"] = fills_total
    response["closed_trades_truncated"] = closed_total > min(len(closed_trades), MAX_RESPONSE_CLOSED_TRADES)
    response["fills_truncated"] = fills_total > min(len(state.fills), MAX_RESPONSE_FILLS)
    current_bar = _current_bar(before, replay, current_index)
    # The market clock shows the time at which the current price became
    # causally available (the latest revealed candle's close). The underlying
    # candle's opening time is exposed separately for chart alignment.
    response["current_market_time"] = bar_reveal_time(current_bar).isoformat() if current_bar else None
    response["current_candle_time"] = current_bar.timestamp.isoformat() if current_bar else None
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


def _snapshot_response(state: ReplayState, before: list, replay: list) -> dict[str, object]:
    """The full bounded snapshot (creation/resume/reconcile): every open trade
    plus the bounded recent closed trades and recent fills (insertion order),
    with review notes/tags and legacy normalization applied at the boundary."""
    response = _rendered(state, before, replay)
    open_trades = [trade for trade in state.trades if trade.status == "open"]
    closed_trades = [trade for trade in state.trades if trade.status == "closed"]
    response["trades"] = _trade_wire(open_trades + closed_trades[-MAX_RESPONSE_CLOSED_TRADES:])
    response["fills"] = _fill_wire(state.fills[-MAX_RESPONSE_FILLS:])
    return response


def state_response(state: ReplayState) -> dict[str, object]:
    before, replay = session_bars(state)
    return _snapshot_response(state, before, replay)


def _update_response(state: ReplayState, before: list, replay: list,
                     delta: dict[str, list]) -> dict[str, object]:
    """A mutation delta: everything an installed snapshot needs to catch up
    without re-sending the recent history. `delta` was captured before the
    save pruned the working set (see `_capture_delta`), so a fill or close
    that the prune removed in the same commit still reaches the client;
    `revision` is the revision the mutation committed."""
    rendered = _rendered(state, before, replay)
    return {
        "id": state.id,
        "revision": state.revision,
        "status": state.status,
        "current_index": state.current_index,
        "current_market_time": rendered["current_market_time"],
        "current_candle_time": rendered["current_candle_time"],
        "current_price": rendered["current_price"],
        "remaining_bars": rendered["remaining_bars"],
        "visible_timeframe": state.visible_timeframe,
        "advance_step_minutes": state.advance_step_minutes,
        "enabled_indicators": list(state.enabled_indicators),
        "displayed_bars": rendered["displayed_bars"],
        "indicators": rendered["indicators"],
        "warnings": rendered["warnings"],
        "stats": rendered["stats"],
        "trade_upserts": _trade_wire(delta["trade_upserts"]),
        "trade_removals_from_open": delta["trade_removals_from_open"],
        "new_fills": _fill_wire(delta["new_fills"]),
        # Insertion order (oldest first): the client appends to its history.
        "newly_closed_trades": _trade_wire(delta["newly_closed_trades"]),
        "closed_trades_total": rendered["closed_trades_total"],
        "fills_total": rendered["fills_total"],
    }


def get_state(session_id: str) -> ReplayState:
    state = load_state_cached(session_id)
    if not state:
        raise SessionNotFoundError("unknown session")
    return state


def step(state: ReplayState) -> dict[str, object]:
    if state.status == "completed":
        raise ValueError("session is completed")
    multiplier = _contract_multiplier(state)
    before, replay = session_bars(state)
    working_set = _WorkingSet(state)
    for _ in range(state.advance_step_minutes):
        next_index = state.current_index + 1
        if next_index >= len(replay):
            state.status = "completed"
            break
        state.current_index = next_index
        bar = replay[next_index]
        process_bar(state, bar, multiplier)
        update_close_excursions(state, bar, multiplier)
    if state.current_index >= len(replay) - 1:
        state.status = "completed"
    delta = _capture_delta(state, working_set)
    save_session(state, "replay_stepped", {"step": state.advance_step_minutes})
    return _update_response(state, before, replay, delta)


def update_settings(state: ReplayState, visible_timeframe: str | None, advance_step_minutes: int | None) -> dict[str, object]:
    if visible_timeframe:
        state.visible_timeframe = visible_timeframe
    if advance_step_minutes is not None:
        if advance_step_minutes <= 0:
            raise ValueError("step size must be positive")
        state.advance_step_minutes = advance_step_minutes
    before, replay = session_bars(state)
    working_set = _WorkingSet(state)
    delta = _capture_delta(state, working_set)
    save_session(state, "settings_changed")
    return _update_response(state, before, replay, delta)


def toggle_indicator(state: ReplayState, indicator_id: str) -> dict[str, object]:
    if indicator_id != "sma_close_35":
        raise ValueError("unknown indicator")
    if indicator_id in state.enabled_indicators:
        state.enabled_indicators.remove(indicator_id)
    else:
        state.enabled_indicators.append(indicator_id)
    before, replay = session_bars(state)
    working_set = _WorkingSet(state)
    delta = _capture_delta(state, working_set)
    save_session(state, "indicator_toggled", {"indicator_id": indicator_id})
    return _update_response(state, before, replay, delta)


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
    working_set = _WorkingSet(state)
    # The market entry executes exactly at the causal reveal time of the
    # latest revealed candle (its close); the candle itself is keyed at its
    # open, which is recorded as the chart anchor for the entry marker.
    trade = open_trade(state, bar_reveal_time(current_bar), float(current_bar.close), direction, quantity,
                       stop_price, target_price, multiplier, source_candle_time=current_bar.timestamp)
    delta = _capture_delta(state, working_set)
    save_session(state, "order_filled", {"direction": direction, "quantity": quantity},
                 orders=[{"trade_id": trade.id, "order_type": "market_entry",
                          "payload": {"direction": direction, "quantity": quantity}}])
    return _update_response(state, before, replay, delta)


def close_position(state: ReplayState, trade_id: str, quantity: float) -> dict[str, object]:
    multiplier = _contract_multiplier(state)
    before, replay = session_bars(state)
    trade = next((item for item in state.trades if item.id == trade_id and item.status == "open"), None)
    if not trade:
        raise TradeNotFoundError("open trade not found")
    current_bar = _current_bar(before, replay, state.current_index)
    if current_bar is None:
        raise ValueError("no causal market price is available")
    working_set = _WorkingSet(state)
    close_trade(state, trade, bar_reveal_time(current_bar), float(current_bar.close), quantity, "manual", multiplier,
                source_candle_time=current_bar.timestamp)
    delta = _capture_delta(state, working_set)
    save_session(state, "position_closed", {"trade_id": trade_id, "quantity": quantity},
                 orders=[{"trade_id": trade.id, "order_type": "market_close", "payload": {"quantity": quantity}}])
    return _update_response(state, before, replay, delta)


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
    working_set = _WorkingSet(state)
    orders = []
    if open_trades:
        for trade in open_trades:
            quantity = trade.remaining_quantity
            close_trade(state, trade, bar_reveal_time(current_bar), float(current_bar.close), quantity, reason, multiplier,
                        source_candle_time=current_bar.timestamp)
            orders.append({"trade_id": trade.id, "order_type": "close_all",
                           "payload": {"quantity": quantity, "reason": reason}})
    delta = _capture_delta(state, working_set)
    save_session(state, "positions_closed_all", {"closed": len(open_trades)}, orders=orders)
    return _update_response(state, before, replay, delta)


# Review-note/tag bounds: a review is a short human annotation, not a document.
MAX_REVIEW_NOTE_LENGTH = 5000
MAX_REVIEW_TAGS = 20
MAX_REVIEW_TAG_LENGTH = 64


def update_trade_review(state: ReplayState, trade_id: str, review_note: str,
                        review_tags: list[str]) -> dict[str, object]:
    """Persist the user review (note + tags) for one session trade.

    The trade must exist in the session (any status, in or out of the hydrated
    window). Reviews live in the trade_reviews table, not in the trade row: the
    mutation never rewrites the trade or bumps the session revision. Returns the
    small dedicated review record.
    """
    if get_trade(state.id, trade_id) is None:
        raise TradeNotFoundError("trade not found in session")
    note = review_note.strip()
    if len(note) > MAX_REVIEW_NOTE_LENGTH:
        raise ValueError(f"review note must be at most {MAX_REVIEW_NOTE_LENGTH} characters")
    tags: list[str] = []
    for raw in review_tags:
        cleaned = str(raw).strip()
        if not cleaned:
            continue
        if len(cleaned) > MAX_REVIEW_TAG_LENGTH:
            raise ValueError(f"review tags must be at most {MAX_REVIEW_TAG_LENGTH} characters")
        if cleaned not in tags:
            tags.append(cleaned)
    if len(tags) > MAX_REVIEW_TAGS:
        raise ValueError(f"at most {MAX_REVIEW_TAGS} review tags are allowed")
    # Review mutations change no replay state, so they return the small
    # dedicated record — not a snapshot or update — and never bump the
    # session revision.
    return upsert_trade_review(state.id, trade_id, note, tags)


def update_trade_stop(state: ReplayState, trade_id: str, price: float | None) -> dict[str, object]:
    """Move an open trade's stop to `price` (None clears it).

    A stop may not be placed on the wrong side of the current causal price.
    Returns the mutation delta.
    """
    trade = next((item for item in state.trades if item.id == trade_id and item.status == "open"), None)
    if not trade:
        raise TradeNotFoundError("open trade not found")
    current_price = _current_price(state)
    if price is not None and (
        (trade.direction == "long" and price >= current_price)
        or (trade.direction == "short" and price <= current_price)
    ):
        raise ValueError("stop is already crossed by the current price")
    before, replay = session_bars(state)
    working_set = _WorkingSet(state)
    trade.stop_price = price
    delta = _capture_delta(state, working_set)
    save_session(state, "stop_moved", {"trade_id": trade_id, "price": price})
    return _update_response(state, before, replay, delta)


def update_trade_target(state: ReplayState, trade_id: str, price: float | None) -> dict[str, object]:
    """Move an open trade's target to `price` (None clears it).

    A target may not be placed on the wrong side of the current causal price.
    Returns the mutation delta.
    """
    trade = next((item for item in state.trades if item.id == trade_id and item.status == "open"), None)
    if not trade:
        raise TradeNotFoundError("open trade not found")
    current_price = _current_price(state)
    if price is not None and (
        (trade.direction == "long" and price <= current_price)
        or (trade.direction == "short" and price >= current_price)
    ):
        raise ValueError("target is already crossed by the current price")
    before, replay = session_bars(state)
    working_set = _WorkingSet(state)
    trade.target_price = price
    delta = _capture_delta(state, working_set)
    save_session(state, "target_moved", {"trade_id": trade_id, "price": price})
    return _update_response(state, before, replay, delta)


def _current_price(state: ReplayState) -> float:
    """The current causal market price (the latest revealed close)."""
    before, replay = session_bars(state)
    current_bar = _current_bar(before, replay, state.current_index)
    if current_bar is None:
        raise ValueError("no causal market price is available")
    return float(current_bar.close)


def trade_history_page(state: ReplayState, status: str, limit: int,
                       cursor: str | None = None) -> dict[str, object]:
    """One bounded page of the session's trade history (newest first).

    Closed trades include review notes/tags and normalized legacy timing. An
    unknown or foreign-session cursor raises ValueError (a 400, not a silent
    wrong page).
    """
    items, total, next_cursor = list_trades(state.id, status, limit, cursor)
    return {"items": _trade_wire(items), "total": total, "next_cursor": next_cursor}


def fill_history_page(state: ReplayState, limit: int, cursor: str | None = None) -> dict[str, object]:
    """One bounded page of the session's fill history (newest first)."""
    items, total, next_cursor = list_fills(state.id, limit, cursor)
    return {"items": _fill_wire(items), "total": total, "next_cursor": next_cursor}


# Bounds for the historical chart focus window: a closed trade may sit far
# outside the current chart context, so the endpoint fetches a bounded window
# around the trade (its span plus context on each side), never the whole
# session's chart.
CHART_FOCUS_MAX_CONTEXT_BARS = 500
CHART_FOCUS_MAX_BARS = 2000


def chart_history(state: ReplayState, trade_id: str, context_bars: int) -> dict[str, object]:
    """A bounded historical chart window anchored around one (possibly old)
    trade.

    The trade is resolved from the authoritative table (it may sit far outside
    the current chart context and the in-memory working set); the market-data
    read is a bounded paged window over the trade's span plus up to
    `context_bars` on each side, never more than CHART_FOCUS_MAX_BARS bars
    in total. While the replay is still active the window's end is clamped to
    the open time of the most recently revealed candle, so focusing an old
    trade can never reveal bars the replay has not causally reached (the
    trade's exit timestamp is the source candle's reveal time, one candle
    past its chart key); for a completed session the clamp is a no-op. A
    trade whose own span exceeds CHART_FOCUS_MAX_BARS gets the first
    CHART_FOCUS_MAX_BARS bars from its entry, flagged ``truncated``: the rest
    of the trade lies beyond the window's right edge. Fills are read for the
    returned window only. The client's live chart returns to the current
    replay window afterwards (the chart instance is not recreated).
    """
    trade = get_trade(state.id, trade_id)
    if trade is None:
        raise TradeNotFoundError("trade not found in session")
    context = min(max(context_bars, 1), CHART_FOCUS_MAX_CONTEXT_BARS)
    entry_anchor = trade.entry_source_candle_time or trade.entry_time
    before, replay = session_bars(state)
    current_bar = _current_bar(before, replay, state.current_index)
    if trade.exit_time is not None:
        # The exit marker's chart anchor: the source candle of the trade's
        # final exit fill (one indexed read, ledger-length independent).
        anchor_end = get_last_fill_anchor(state.id, trade_id) or trade.exit_time
    else:
        anchor_end = current_bar.timestamp if current_bar else entry_anchor
    if current_bar is not None:
        # Causal clamp: the window may not end past the open of the most
        # recently revealed candle (a closed trade's exit timestamp is its
        # source candle's reveal time, one candle past its chart key).
        anchor_end = min(anchor_end, current_bar.timestamp)
    span_minutes = max(int((anchor_end - entry_anchor).total_seconds() // 60), 0)
    truncated = False
    if span_minutes >= CHART_FOCUS_MAX_BARS:
        # Very long trade: the first CHART_FOCUS_MAX_BARS bars from the entry
        # marker's candle; the remainder is beyond the right edge (flagged
        # truncated).
        start = entry_anchor
        end = entry_anchor + timedelta(minutes=CHART_FOCUS_MAX_BARS - 1)
        truncated = True
    else:
        # Split the remaining bar budget around the span, capping each side
        # at the requested context, so the window never exceeds
        # CHART_FOCUS_MAX_BARS (market gaps only make it smaller).
        budget = CHART_FOCUS_MAX_BARS - span_minutes - 1
        front = min(context, budget // 2)
        back = min(context, budget - front)
        start = entry_anchor - timedelta(minutes=front)
        end = anchor_end + timedelta(minutes=back)
    if current_bar is not None:
        # The trailing context must not reveal bars the replay has not
        # causally reached: the window ends at the open of the most recently
        # revealed candle (a no-op for a completed session).
        end = min(end, current_bar.timestamp)
    window = RangeBars(state.symbol, start, end, state.data_version)
    source = list(window)
    displayed = resample(source, state.visible_timeframe, state.profile)
    fills = get_trade_fills(state.id, trade_id, start, end)
    response: dict[str, object] = {
        "trade": _trade_wire([trade])[0],
        "fills": _fill_wire(fills),
        "displayed_bars": serializable(displayed),
        "indicators": {},
        "truncated": truncated,
    }
    if "sma_close_35" in state.enabled_indicators:
        response["indicators"] = {"sma_close_35": sma(displayed)}
    return response
