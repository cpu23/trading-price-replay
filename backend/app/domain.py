from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta
from math import isfinite
from typing import Literal
from uuid import uuid4

Timeframe = Literal["1m", "5m", "15m", "1h", "4h", "1d"]
Profile = Literal["utc_aligned", "new_york_close"]
Direction = Literal["long", "short"]
# How precisely a fill's execution time is known. M1 OHLC data identifies an
# opening-gap execution exactly, but only the candle interval for an ordinary
# intrabar touch. `None` marks legacy fills persisted before the concept
# existed; the API boundary normalizes them to `"legacy"`.
TimePrecision = Literal["exact", "bar_interval"]


def bar_reveal_time(bar: "Bar") -> datetime:
    """The time at which a canonical M1 bar's close becomes causally available.

    `bar.timestamp` is the candle's *opening* time; the candle covers the
    half-open interval `[timestamp, timestamp + 1 minute)` and its close price
    cannot influence execution, indicators, or displayed higher-timeframe
    candles before the interval ends. This helper is the single place that
    encodes the one-minute M1 interval, so no other module re-derives it.
    """
    return bar.timestamp + timedelta(minutes=1)

@dataclass(slots=True)
class Bar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(slots=True)
class DisplayBar(Bar):
    timeframe: Timeframe = "1m"
    is_partial: bool = False
    source_1m_start_time: datetime | None = None
    source_1m_end_time: datetime | None = None


@dataclass(slots=True)
class Trade:
    id: str
    session_id: str
    direction: Direction
    initial_quantity: float
    remaining_quantity: float
    entry_time: datetime
    entry_price: float
    stop_price: float | None = None
    target_price: float | None = None
    initial_risk: float | None = None
    realized_pnl: float = 0.0
    status: Literal["open", "closed"] = "open"
    entry_market_price: float | None = None
    # Final-exit metadata, persisted the moment the remaining quantity reaches
    # exactly zero. Partial exits never set these; they describe only the fill
    # that closed the final remainder. All None on legacy closed trades, which
    # recorded no exit metadata.
    exit_market_price: float | None = None
    exit_price: float | None = None
    exit_time: datetime | None = None
    exit_time_precision: TimePrecision | None = None
    exit_window_start: datetime | None = None
    exit_window_end: datetime | None = None
    final_exit_reason: str | None = None
    # Close-based excursions: the most favorable / adverse gross P&L observed
    # at causally revealed candle closes while the trade remained open. Measured
    # from the reference entry price; a stop/target close on a candle excludes
    # that candle's close because the intrabar touch order is unknown. None on
    # legacy trades (never measured); 0.0 on a new trade that never went in
    # favor. These are OHLC-resolution metrics, not exact intrabar MAE/MFE.
    mfe_gross_pnl: float | None = None
    mae_gross_pnl: float | None = None
    # Per-trade execution costs, booked incrementally by the entry and exit
    # fills (never by scanning the fill ledger). Zero on legacy trades, whose
    # fills carried no cost components.
    total_commission: float = 0.0
    total_spread_cost: float = 0.0
    total_slippage_cost: float = 0.0

    def __post_init__(self) -> None:
        if self.entry_market_price is None:
            # Legacy sessions predate the cost-aware contract: the recorded
            # entry price was the reference market price.
            self.entry_market_price = self.entry_price


@dataclass(slots=True)
class Fill:
    id: str
    trade_id: str
    session_id: str
    timestamp: datetime
    price: float
    quantity: float
    reason: str
    pnl: float
    market_price: float | None = None
    gross_pnl: float = 0.0
    commission: float = 0.0
    spread_cost: float = 0.0
    slippage_cost: float = 0.0
    # Execution-time precision. For `exact` fills the timestamp is the true
    # execution time (an opening-gap fill) and the window collapses to it. For
    # `bar_interval` fills the timestamp is the effective ordering time (the
    # candle open) and the execution is only known to lie within
    # [execution_window_start, execution_window_end) of one M1 candle. None on
    # legacy fills: the recorded timestamp is shown as-is without claiming any
    # precision.
    time_precision: TimePrecision | None = None
    execution_window_start: datetime | None = None
    execution_window_end: datetime | None = None

    def __post_init__(self) -> None:
        if self.market_price is None:
            # Legacy fills recorded a single price and no cost components.
            self.market_price = self.price
            self.gross_pnl = self.pnl


@dataclass(slots=True)
class StatsAccumulator:
    """Durable, incrementally updated session statistics.

    Booked transactionally alongside the authoritative mutation (entry/exit
    fills and final trade closes), so routine state responses never scan the
    historical fill or trade tables. `r_values` holds the realized R of every
    closed R-bearing trade in close order (bounded by the number of trades,
    never by the number of fills) and powers the median. Legacy sessions are
    backfilled once by schema migration v6 from the normalized tables.
    """
    trades_opened: int = 0
    trades_completed: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    winning_pnl_sum: float = 0.0
    losing_pnl_sum: float = 0.0
    gross_pnl_sum: float = 0.0
    net_pnl_sum: float = 0.0
    commission_sum: float = 0.0
    spread_cost_sum: float = 0.0
    slippage_sum: float = 0.0
    long_pnl_sum: float = 0.0
    short_pnl_sum: float = 0.0
    # Max realized balance over the fill path (including the initial balance,
    # which seeds it on the first booking); None until the first fill.
    peak_realized_balance: float | None = None
    # Deepest peak-to-trough move over the realized balance path; the current
    # (possibly unrealized) equity below the peak is folded in at read time.
    max_realized_drawdown: float = 0.0
    # Sum of (final exit time - entry time) over closed trades, in seconds.
    holding_seconds_sum: float = 0.0
    r_values: list[float] = field(default_factory=list)

    @classmethod
    def from_mapping(cls, value: dict[str, object]) -> "StatsAccumulator":
        """Build from a persisted snapshot dict, tolerating missing (legacy) and
        unknown (future) keys so old and newer databases both load safely."""
        kwargs: dict[str, object] = {}
        for item in fields(cls):
            if item.name in value:
                kwargs[item.name] = value[item.name]
        r_values = kwargs.get("r_values", [])
        kwargs["r_values"] = [float(item) for item in r_values]
        return cls(**kwargs)


@dataclass(slots=True)
class ReplayState:
    id: str
    symbol: str
    start: datetime
    end: datetime
    profile: Profile
    visible_timeframe: Timeframe = "1m"
    advance_step_minutes: int = 1
    chart_context_1m_bars: int = 1000
    indicator_warmup_margin: int = 15
    current_index: int = -1
    account_currency: str = "USD"
    conversion_rate: float = 1.0
    enabled_indicators: list[str] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    status: Literal["active", "completed"] = "active"
    initial_balance: float = 10000.0
    spread: float = 0.0
    slippage: float = 0.0
    commission_per_quantity: float = 0.0
    # Pinned instrument/dataset snapshot for reproducible sessions. None on
    # legacy states; the service falls back to the then-current source.
    contract_multiplier: float | None = None
    data_version: str | None = None
    # Pinned instrument display metadata for reproducible session formatting.
    # None on legacy states; consumers fall back to the then-current symbol row.
    price_precision: int | None = None
    pnl_currency: str | None = None
    # Optimistic concurrency: None until the first save assigns revision 1;
    # loaded states carry the database revision and saves CAS on it.
    revision: int | None = None
    # Incremental session statistics, persisted with the snapshot and booked
    # transactionally with each mutation; routine reads never scan history.
    accumulator: StatsAccumulator = field(default_factory=StatsAccumulator)
    # Hydrated history totals from the normalized tables. Excluded from the
    # persisted snapshot (recomputed at load/save); a fresh state has none.
    closed_trades_total: int = 0
    fills_total: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.accumulator, dict):
            # Persisted snapshots carry the accumulator as a plain mapping.
            self.accumulator = StatsAccumulator.from_mapping(self.accumulator)
        if not isfinite(self.initial_balance) or self.initial_balance <= 0:
            raise ValueError("initial_balance must be a finite positive number")
        if not isfinite(self.conversion_rate) or self.conversion_rate <= 0:
            raise ValueError("conversion_rate must be a finite positive number")
        if self.contract_multiplier is not None and (not isfinite(self.contract_multiplier) or self.contract_multiplier <= 0):
            raise ValueError("contract_multiplier must be a finite positive number when set")
        if self.price_precision is not None and (isinstance(self.price_precision, bool) or self.price_precision < 0):
            raise ValueError("price_precision must be a non-negative integer when set")
        if self.pnl_currency is not None and (not isinstance(self.pnl_currency, str) or not self.pnl_currency.strip()):
            raise ValueError("pnl_currency must be a non-empty string when set")
        for name in ("spread", "slippage", "commission_per_quantity"):
            value = getattr(self, name)
            if not isfinite(value) or value < 0:
                raise ValueError(f"{name} must be a finite non-negative number")

    @classmethod
    def create(cls, **kwargs: object) -> "ReplayState":
        return cls(id=str(uuid4()), **kwargs)

    def fill_costs(self, quantity: float, contract_multiplier: float) -> tuple[float, float, float]:
        """Commission, spread cost, and slippage cost for one fill, in account currency."""
        scale = quantity * contract_multiplier * self.conversion_rate
        return self.commission_per_quantity * quantity, self.spread / 2.0 * scale, self.slippage * scale


def serializable(value: object) -> object:
    if hasattr(value, "__dataclass_fields__"):
        return {key: serializable(item) for key, item in asdict(value).items()}
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [serializable(item) for item in value]
    if isinstance(value, dict):
        return {key: serializable(item) for key, item in value.items()}
    return value
def state_snapshot(state: "ReplayState") -> dict[str, object]:
    """Serializable state snapshot without the normalized trade/fill histories.

    Walks the dataclass fields directly (no `asdict` deep copy, which would
    recursively serialize every trade and fill), so the snapshot stays bounded
    even for sessions with very long trade histories. The normalized `trades`
    and `fills` tables are authoritative; `load_session` reconstructs the open
    set plus bounded recent windows from them. `closed_trades_total` and
    `fills_total` are hydrated metadata, recomputed at load/save, never stored.
    """
    return {
        item.name: serializable(getattr(state, item.name))
        for item in fields(state)
        if item.name not in ("trades", "fills", "closed_trades_total", "fills_total")
    }
