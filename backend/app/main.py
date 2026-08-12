from __future__ import annotations

import math
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Literal

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
    market_order, state_response, step, toggle_indicator, update_settings,
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


@app.post("/api/imports/inspect-path")
def inspect(request: PathRequest):
    return attempt(inspect_path, request.path)


@app.post("/api/imports")
def create_import(request: ImportRequest):
    return attempt(import_file, **request.model_dump())


@app.get("/api/imports/{batch_id}")
def import_status(batch_id: str):
    return get_import_batch(batch_id) or (_ for _ in ()).throw(HTTPException(404, "unknown import"))


@app.get("/api/symbols")
def symbols():
    return list_symbols()


@app.get("/api/symbols/{symbol}/ranges")
def ranges(symbol: str):
    metadata = get_symbol(symbol)
    if not metadata:
        raise HTTPException(404, "unknown symbol")
    return {"symbol": symbol, "start": metadata["first_timestamp"], "end": metadata["last_timestamp"]}


@app.get("/api/timeframe-profiles")
def profiles():
    return [
        {"id": "utc_aligned", "implemented": True, "default": True},
        {"id": "new_york_close", "implemented": True, "timezone": "America/New_York", "anchor": "17:00"},
        {"id": "custom_session_anchor", "implemented": False},
    ]


@app.post("/api/replay/sessions")
def sessions(request: SessionRequest):
    return attempt(create_session, **request.model_dump())


@app.get("/api/replay/sessions")
def session_list():
    return list_sessions()


@app.delete("/api/replay/sessions/{session_id}")
def session_delete(session_id: str):
    if not delete_session(session_id):
        raise HTTPException(404, "unknown session")
    return {"id": session_id, "deleted": True}


@app.get("/api/replay/sessions/{session_id}/state")
def session_state(session_id: str):
    return attempt(state_response, load_state(session_id))


@app.post("/api/replay/sessions/{session_id}/step")
def session_step(session_id: str):
    return attempt(step, load_state(session_id))


@app.post("/api/replay/sessions/{session_id}/close-all")
def session_close_all(session_id: str):
    return attempt(close_all_positions, load_state(session_id))


@app.patch("/api/replay/sessions/{session_id}/settings")
def settings(session_id: str, request: SettingsRequest):
    return attempt(update_settings, load_state(session_id), **request.model_dump())


@app.post("/api/replay/sessions/{session_id}/indicators/{indicator_id}/toggle")
def indicators(session_id: str, indicator_id: str):
    return attempt(toggle_indicator, load_state(session_id), indicator_id)


@app.post("/api/replay/sessions/{session_id}/orders/market")
def orders(session_id: str, request: MarketOrderRequest):
    return attempt(market_order, load_state(session_id), **request.model_dump())


@app.post("/api/trades/{trade_id}/close")
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


@app.get("/api/replay/sessions/{session_id}/stats")
def stats(session_id: str):
    return state_response(load_state(session_id))["stats"]
