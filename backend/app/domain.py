from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from math import isfinite
from typing import Literal
from uuid import uuid4

Timeframe = Literal["1m", "5m", "15m", "1h", "4h", "1d"]
Profile = Literal["utc_aligned", "new_york_close"]
Direction = Literal["long", "short"]


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

    def __post_init__(self) -> None:
        if self.market_price is None:
            # Legacy fills recorded a single price and no cost components.
            self.market_price = self.price
            self.gross_pnl = self.pnl


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

    def __post_init__(self) -> None:
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
    and `fills` tables are authoritative; `load_session` reconstructs the full
    history from them.
    """
    return {
        item.name: serializable(getattr(state, item.name))
        for item in fields(state)
        if item.name not in ("trades", "fills")
    }
