"""Public API models (the wire contract).

These Pydantic models are the single source of truth for the wire format:
the FastAPI routes declare them as request/response models, the OpenAPI
schema is generated from them, and the frontend's API types are generated
from that schema. They describe exactly what the API sends and receives —
internal bookkeeping (the statistics accumulator, the in-memory working set)
never leaks through the wire.

Replay protocol:
- `ReplaySnapshot` is the full bounded authoritative state; it is sent on
  session creation, resume, and explicit refresh/reconciliation.
- `ReplayUpdate` is a mutation delta for an installed snapshot; it is sent
  by every replay mutation. Its `revision` is the revision the mutation
  committed, so clients can reject stale updates and detect gaps.
- History beyond the bounded snapshot windows is fetched through the
  paginated `TradeHistoryPage` / `FillHistoryPage` endpoints.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


class PathRequest(BaseModel):
    path: str


class ImportRequest(PathRequest):
    symbol: str = Field(pattern=r"^[A-Za-z0-9._-]+$")
    asset_class: str = Field("forex", min_length=1)
    pnl_currency: str = Field("USD", min_length=1)
    price_precision: int = Field(5, ge=0)
    contract_multiplier: float = Field(1.0, gt=0, allow_inf_nan=False)
    default_profile: Literal["utc_aligned", "new_york_close"] = "utc_aligned"


class SessionRequest(BaseModel):
    symbol: str
    start: datetime
    end: datetime
    profile: Literal["utc_aligned", "new_york_close"] | None = None
    visible_timeframe: Literal["1m", "5m", "15m", "1h", "4h", "1d"] = "1m"
    advance_step_minutes: int = Field(1, ge=1)
    chart_context_1m_bars: int = Field(1000, ge=500, le=2000)
    account_currency: str = "USD"
    conversion_rate: float = Field(1.0, gt=0, allow_inf_nan=False)
    initial_balance: float = Field(10000.0, gt=0, allow_inf_nan=False)
    spread: float = Field(0.0, ge=0, allow_inf_nan=False)
    slippage: float = Field(0.0, ge=0, allow_inf_nan=False)
    commission_per_quantity: float = Field(0.0, ge=0, allow_inf_nan=False)


class SettingsRequest(BaseModel):
    visible_timeframe: Literal["1m", "5m", "15m", "1h", "4h", "1d"] | None = None
    advance_step_minutes: int | None = Field(None, ge=1)


class MarketOrderRequest(BaseModel):
    direction: Literal["long", "short"]
    quantity: float = Field(gt=0, allow_inf_nan=False)
    stop_price: float | None = Field(None, allow_inf_nan=False)
    target_price: float | None = Field(None, allow_inf_nan=False)


class CloseRequest(BaseModel):
    session_id: str
    quantity: float = Field(gt=0, allow_inf_nan=False)


class PriceRequest(BaseModel):
    session_id: str
    price: float | None = Field(None, allow_inf_nan=False)


class ReviewRequest(BaseModel):
    session_id: str
    review_note: str = Field("", max_length=5000)
    review_tags: list[Annotated[str, Field(max_length=64)]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Shared wire models
# ---------------------------------------------------------------------------


class IndicatorPoint(BaseModel):
    time: str
    value: float


class DisplayBar(BaseModel):
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    timeframe: str
    is_partial: bool
    source_1m_start_time: str | None
    source_1m_end_time: str | None


class Trade(BaseModel):
    id: str
    session_id: str
    direction: Literal["long", "short"]
    initial_quantity: float
    remaining_quantity: float
    entry_time: str
    entry_price: float
    stop_price: float | None
    target_price: float | None
    initial_risk: float | None
    realized_pnl: float
    status: Literal["open", "closed"]
    entry_market_price: float
    exit_market_price: float | None
    exit_price: float | None
    exit_time: str | None
    # Null while the trade is still open; "legacy" for closed trades persisted
    # before execution precision existed.
    exit_time_precision: Literal["exact", "bar_interval", "legacy"] | None
    exit_window_start: str | None
    exit_window_end: str | None
    final_exit_reason: str | None
    mfe_gross_pnl: float | None
    mae_gross_pnl: float | None
    mfe_close_price_delta: float | None
    mae_close_price_delta: float | None
    total_commission: float
    total_spread_cost: float
    total_slippage_cost: float
    # Chart anchor for the entry marker: the candle whose revealed close
    # generated the market entry (legacy rows: the recorded entry time, which
    # was already the source candle's open). Never null on the wire; the
    # execution timestamp (`entry_time`) is the causal reveal time and is
    # deliberately not reused for chart alignment.
    entry_source_candle_time: str
    # User review, hydrated from the trade_reviews table by the service.
    review_note: str = ""
    review_tags: list[str] = Field(default_factory=list)


class Fill(BaseModel):
    id: str
    trade_id: str
    session_id: str
    timestamp: str
    price: float
    quantity: float
    reason: str
    pnl: float
    market_price: float | None
    gross_pnl: float
    commission: float
    spread_cost: float
    slippage_cost: float
    # "legacy" normalizes fills persisted before execution precision existed.
    # Never null on the wire.
    time_precision: Literal["exact", "bar_interval", "legacy"]
    execution_window_start: str | None
    execution_window_end: str | None
    # Chart anchor: the candle the execution belongs to (legacy rows: the
    # recorded timestamp, which was already the source candle's open). Never
    # null on the wire; the execution timestamp is deliberately not reused
    # for chart alignment.
    source_candle_time: str


class SessionStats(BaseModel):
    trades_opened: int
    trades_completed: int
    win_rate: float
    net_pnl: float
    gross_pnl: float
    trading_costs: float
    commission_paid: float
    spread_cost: float
    slippage_cost: float
    unrealized_pnl: float
    balance: float
    equity: float
    total_r: float
    average_r: float
    average_win_r: float
    average_losing_r: float
    average_win: float
    average_loss: float
    profit_factor: float
    max_drawdown: float
    long_pnl: float
    short_pnl: float
    average_holding_seconds: float


class SessionSummary(BaseModel):
    id: str
    symbol: str
    start: str
    end: str
    status: str
    current_index: int
    updated_at: str


class DeleteResponse(BaseModel):
    id: str
    deleted: bool


class SymbolMetadata(BaseModel):
    """One symbol's current instrument/dataset metadata.

    `data_version` is null on legacy rows published before immutable dataset
    versions existed; such datasets are the then-current source and sessions
    created from them pin no version.
    """

    symbol: str
    asset_class: str
    pnl_currency: str
    price_precision: int
    contract_multiplier: float
    default_profile: str
    first_timestamp: str
    last_timestamp: str
    data_version: str | None = None


class SymbolRange(BaseModel):
    symbol: str
    start: str
    end: str


class TimeframeProfile(BaseModel):
    id: str
    implemented: bool
    default: bool | None = None
    timezone: str | None = None
    anchor: str | None = None


class InspectPathResponse(BaseModel):
    kind: str
    files: list[str]


class ImportValidation(BaseModel):
    duplicates: int = 0
    subminute: int = 0
    invalid_ohlc: int = 0
    invalid_numeric: int = 0
    invalid_volume: int = 0
    invalid_timestamps: int = 0
    gap_count: int = 0
    source_non_monotonic: bool = False


class ImportBatch(BaseModel):
    id: str
    symbol: str
    source_path: str
    schema_id: str
    status: str
    rows_imported: int
    validation_json: str | None = None
    created_at: str
    validation: ImportValidation | None = None


# ---------------------------------------------------------------------------
# Replay state protocol
# ---------------------------------------------------------------------------


class ReplaySnapshot(BaseModel):
    """The full bounded authoritative state of a session.

    Sent on session creation, resume, and explicit refresh/reconciliation.
    `revision` is the persisted revision the snapshot was read from. History
    is bounded: every open trade plus the most recent closed trades and
    fills; the `*_total` fields and truncation flags say how much more
    exists in the authoritative tables (fetched via the history endpoints).
    The internal statistics accumulator is never exposed.
    """

    id: str
    symbol: str
    start: str
    end: str
    profile: str
    visible_timeframe: str
    advance_step_minutes: int
    chart_context_1m_bars: int
    indicator_warmup_margin: int
    current_index: int
    account_currency: str
    conversion_rate: float
    enabled_indicators: list[str]
    status: Literal["active", "completed"]
    initial_balance: float
    spread: float
    slippage: float
    commission_per_quantity: float
    contract_multiplier: float | None = None
    data_version: str | None = None
    price_precision: int | None = None
    pnl_currency: str | None = None
    # The persisted revision this snapshot was read from.
    revision: int
    closed_trades_total: int
    fills_total: int
    closed_trades_truncated: bool
    fills_truncated: bool
    # Revealed causal market time (latest revealed candle's close time); the
    # underlying candle's opening time is exposed separately.
    current_market_time: str | None
    current_candle_time: str | None
    current_price: float | None
    remaining_bars: int
    displayed_bars: list[DisplayBar]
    indicators: dict[str, list[IndicatorPoint]]
    warnings: list[str]
    stats: SessionStats
    # Every open trade plus the bounded recent closed trades (insertion
    # order), with review notes/tags and normalized legacy timing/chart
    # anchors.
    trades: list[Trade]
    # The bounded recent fills (insertion order), normalized as above.
    fills: list[Fill]


class ReplayUpdate(BaseModel):
    """A mutation delta for an installed snapshot.

    Sent by every replay mutation (step, orders, closes, stop/target moves,
    settings, indicator toggles). `revision` is the revision the mutation
    committed (after the commit, never a speculative in-memory value), so a
    client can reject stale updates (`revision <= installed`), apply the
    next one (`revision == installed + 1`), and fetch a fresh snapshot on a
    gap (`revision > installed + 1`). The chart remains a full bounded
    payload; the history is delta-based.
    """

    id: str
    # The revision this update committed to.
    revision: int
    status: Literal["active", "completed"]
    current_index: int
    current_market_time: str | None
    current_candle_time: str | None
    current_price: float | None
    remaining_bars: int
    visible_timeframe: str
    advance_step_minutes: int
    enabled_indicators: list[str]
    displayed_bars: list[DisplayBar]
    indicators: dict[str, list[IndicatorPoint]]
    warnings: list[str]
    stats: SessionStats
    # Trades that changed while remaining open (or that differ from the
    # installed snapshot), for upsert by id.
    trade_upserts: list[Trade]
    # Trade ids that left the open set since the installed state.
    trade_removals_from_open: list[str]
    # Fills appended since the installed state (dedupe by id on the client).
    new_fills: list[Fill]
    # Trades that closed since the installed state, insertion order, with
    # review notes/tags and normalized legacy timing/chart anchors.
    newly_closed_trades: list[Trade]
    # Authoritative history totals after the mutation.
    closed_trades_total: int
    fills_total: int


class TradeHistoryPage(BaseModel):
    """One page of a session's trade history (newest first, cursor-based).

    Closed trades include their review notes/tags and normalized legacy
    timing; `next_cursor` is null on the final page.
    """

    items: list[Trade]
    total: int
    next_cursor: str | None = None


class FillHistoryPage(BaseModel):
    """One page of a session's fill history (newest first, cursor-based).

    `next_cursor` is null on the final page.
    """

    items: list[Fill]
    total: int
    next_cursor: str | None = None


class ChartHistoryResponse(BaseModel):
    """A bounded historical chart window anchored around one (possibly old)
    trade.

    The window holds enough source candles around the trade for the chart to
    focus on it; it never loads the entire session's chart and does not
    recreate the chart instance on the client. The trade's own fills come
    from the authoritative fill ledger (the trade may no longer be in the
    bounded in-memory working set).
    """

    trade: Trade
    fills: list[Fill]
    displayed_bars: list[DisplayBar]
    indicators: dict[str, list[IndicatorPoint]]


class ReviewRecord(BaseModel):
    """The persisted review (note + tags) for one trade.

    Review mutations return this small dedicated record — not a replay
    snapshot or update — because they change no replay state and must not
    bump the session revision.
    """

    trade_id: str
    session_id: str
    note: str
    tags: list[str]
    updated_at: str
