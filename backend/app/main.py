from __future__ import annotations
import json
import math
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .market_data import import_file, inspect_path
from .migrations import CURRENT_SCHEMA_VERSION, read_schema_version
from .repository import (
    StaleSessionError, connect, delete_session, get_import_batch, get_symbol, initialize, list_sessions, list_symbols,
    save_session,
)
from .service import (
    SessionNotFoundError, TradeNotFoundError, close_all_positions, close_position, create_session, get_state,
    market_order, state_response, step, toggle_indicator, update_settings, update_trade_review,
)

@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize()
    yield


app = FastAPI(title="Trading Price Replay", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173"], allow_methods=["*"], allow_headers=["*"])


def _json_safe(value: object) -> object:
    """Strip non-finite floats from validation error payloads so a rejected 1e309-style
    input renders as a 422 instead of crashing the error response serialization."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    return value


@app.exception_handler(RequestValidationError)
def validation_exception_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": _json_safe(exc.errors())})


@app.exception_handler(StaleSessionError)
def stale_session_exception_handler(_: Request, exc: StaleSessionError) -> JSONResponse:
    """Any stale save maps to 409, including routes that call save_session directly
    (e.g. stop/target) and never pass through `attempt`."""
    return JSONResponse(status_code=409, content={"detail": str(exc)})


def attempt(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except HTTPException:
        raise
    except SessionNotFoundError as error:
        raise HTTPException(404, str(error)) from error
    except TradeNotFoundError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


def load_state(session_id: str):
    """Resolve a session for a route; `get_state` is evaluated inside the try so the 404 mapping applies."""
    try:
        return get_state(session_id)
    except SessionNotFoundError as error:
        raise HTTPException(404, str(error)) from error


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


# --- Response models ---------------------------------------------------------
# The FastAPI/OpenAPI contract is the single source of truth for the wire
# format: the frontend's API types are generated from this schema, so every
# response the API can produce is described here and validated on the way out.


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
    entry_market_price: float | None
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
    total_commission: float
    total_spread_cost: float
    total_slippage_cost: float
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
    time_precision: Literal["exact", "bar_interval", "legacy"]
    execution_window_start: str | None
    execution_window_end: str | None


class StatsAccumulator(BaseModel):
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
    slippage_cost_sum: float = 0.0
    long_pnl_sum: float = 0.0
    short_pnl_sum: float = 0.0
    peak_realized_balance: float | None = None
    max_realized_drawdown: float = 0.0
    holding_seconds_sum: float = 0.0
    r_values: list[float] = Field(default_factory=list)


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
    median_r: float
    average_win_r: float
    average_losing_r: float
    average_win: float
    average_loss: float
    profit_factor: float
    max_drawdown: float
    long_pnl: float
    short_pnl: float
    average_holding_seconds: float


class StateResponse(BaseModel):
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
    contract_multiplier: float | None
    data_version: str | None
    price_precision: int | None
    pnl_currency: str | None
    revision: int | None
    accumulator: StatsAccumulator
    trades: list[Trade]
    fills: list[Fill]
    closed_trades_total: int
    fills_total: int
    closed_trades_truncated: bool
    fills_truncated: bool
    # Revealed causal market time (latest revealed candle's close time); the
    # underlying candle's opening time is exposed separately.
    current_market_time: str | None
    current_candle_time: str | None
    current_price: float | None
    displayed_bars: list[DisplayBar]
    indicators: dict[str, list[IndicatorPoint]]
    warnings: list[str]
    stats: SessionStats
    remaining_bars: int


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
    symbol: str
    asset_class: str
    pnl_currency: str
    price_precision: int
    contract_multiplier: float
    default_profile: str
    first_timestamp: str
    last_timestamp: str
    data_version: str


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


@app.get("/api/health")
def health():
    try:
        with connect() as db:
            version = read_schema_version(db)
            db.execute("SELECT 1 FROM replay_sessions LIMIT 1").fetchone()
    except Exception as error:
        return JSONResponse(
            status_code=503,
            content={"status": "error", "database": "unavailable", "detail": str(error)},
        )
    if version != CURRENT_SCHEMA_VERSION:
        return JSONResponse(
            status_code=503,
            content={
                "status": "error",
                "database": "migration_required",
                "schema_version": version,
                "expected_schema_version": CURRENT_SCHEMA_VERSION,
            },
        )
    return {"status": "ok", "database": "ok", "schema_version": version}


@app.post("/api/imports/inspect-path", response_model=InspectPathResponse)
def inspect(request: PathRequest):
    return attempt(inspect_path, request.path)


@app.post("/api/imports", response_model=ImportBatch)
def create_import(request: ImportRequest):
    return attempt(import_file, **request.model_dump())


@app.get("/api/imports/{batch_id}", response_model=ImportBatch)
def import_status(batch_id: str):
    batch = get_import_batch(batch_id) or (_ for _ in ()).throw(HTTPException(404, "unknown import"))
    value = dict(batch)
    raw = value.get("validation_json")
    value["validation"] = json.loads(raw) if raw else None
    return value


@app.get("/api/symbols", response_model=list[SymbolMetadata])
def symbols():
    return list_symbols()


@app.get("/api/symbols/{symbol}/ranges", response_model=SymbolRange)
def ranges(symbol: str):
    metadata = get_symbol(symbol)
    if not metadata:
        raise HTTPException(404, "unknown symbol")
    return {"symbol": symbol, "start": metadata["first_timestamp"], "end": metadata["last_timestamp"]}


@app.get("/api/timeframe-profiles", response_model=list[TimeframeProfile])
def profiles():
    return [
        {"id": "utc_aligned", "implemented": True, "default": True},
        {"id": "new_york_close", "implemented": True, "timezone": "America/New_York", "anchor": "17:00"},
        {"id": "custom_session_anchor", "implemented": False},
    ]


@app.post("/api/replay/sessions", response_model=StateResponse)
def sessions(request: SessionRequest):
    return attempt(create_session, **request.model_dump())


@app.get("/api/replay/sessions", response_model=list[SessionSummary])
def session_list():
    return list_sessions()


@app.delete("/api/replay/sessions/{session_id}", response_model=DeleteResponse)
def session_delete(session_id: str):
    if not delete_session(session_id):
        raise HTTPException(404, "unknown session")
    return {"id": session_id, "deleted": True}


@app.get("/api/replay/sessions/{session_id}/state", response_model=StateResponse)
def session_state(session_id: str):
    return attempt(state_response, load_state(session_id))


@app.post("/api/replay/sessions/{session_id}/step", response_model=StateResponse)
def session_step(session_id: str):
    return attempt(step, load_state(session_id))


@app.post("/api/replay/sessions/{session_id}/close-all", response_model=StateResponse)
def session_close_all(session_id: str):
    return attempt(close_all_positions, load_state(session_id))


@app.patch("/api/replay/sessions/{session_id}/settings", response_model=StateResponse)
def settings(session_id: str, request: SettingsRequest):
    return attempt(update_settings, load_state(session_id), **request.model_dump())


@app.post("/api/replay/sessions/{session_id}/indicators/{indicator_id}/toggle", response_model=StateResponse)
def indicators(session_id: str, indicator_id: str):
    return attempt(toggle_indicator, load_state(session_id), indicator_id)


@app.post("/api/replay/sessions/{session_id}/orders/market", response_model=StateResponse)
def orders(session_id: str, request: MarketOrderRequest):
    return attempt(market_order, load_state(session_id), **request.model_dump())


@app.post("/api/trades/{trade_id}/close", response_model=StateResponse)
def close(trade_id: str, request: CloseRequest):
    return attempt(close_position, load_state(request.session_id), trade_id, request.quantity)


@app.put("/api/trades/{trade_id}/stop")
def stop(trade_id: str, request: PriceRequest):
    state = load_state(request.session_id)
    trade = next((item for item in state.trades if item.id == trade_id and item.status == "open"), None)
    if not trade:
        raise HTTPException(404, "open trade not found")
    response = state_response(state)
    price = response["current_price"]
    if price is None:
        raise HTTPException(400, "no causal market price is available")
    price = float(price)
    if request.price is not None and (
        (trade.direction == "long" and request.price >= price)
        or (trade.direction == "short" and request.price <= price)
    ):
        raise HTTPException(400, "stop is already crossed by the current price")
    trade.stop_price = request.price
    save_session(state, "stop_moved", {"trade_id": trade_id, "price": request.price})
    return state_response(state)


@app.put("/api/trades/{trade_id}/target")
def target(trade_id: str, request: PriceRequest):
    state = load_state(request.session_id)
    trade = next((item for item in state.trades if item.id == trade_id and item.status == "open"), None)
    if not trade:
        raise HTTPException(404, "open trade not found")
    response = state_response(state)
    price = response["current_price"]
    if price is None:
        raise HTTPException(400, "no causal market price is available")
    price = float(price)
    if request.price is not None and (
        (trade.direction == "long" and request.price <= price)
        or (trade.direction == "short" and request.price >= price)
    ):
        raise HTTPException(400, "target is already crossed by the current price")
    trade.target_price = request.price
    save_session(state, "target_moved", {"trade_id": trade_id, "price": request.price})
    return state_response(state)


@app.patch("/api/trades/{trade_id}/review", response_model=StateResponse)
def review(trade_id: str, request: ReviewRequest):
    """Persist the user review (note + tags) for one session trade."""
    return attempt(update_trade_review, load_state(request.session_id), trade_id,
                   request.review_note, request.review_tags)


@app.get("/api/replay/sessions/{session_id}/stats", response_model=SessionStats)
def stats(session_id: str):
    return state_response(load_state(session_id))["stats"]
