from __future__ import annotations

import json
import math
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api_models import (ChartHistoryResponse, CloseRequest, DeleteResponse, FillHistoryPage,
                         ImportBatch, ImportRequest, InspectPathResponse, MarketOrderRequest,
                         PathRequest, PriceRequest, ReplaySnapshot, ReplayUpdate, ReviewRecord,
                         ReviewRequest, SessionRequest, SessionStats, SessionSummary,
                         SettingsRequest, SymbolMetadata, SymbolRange, TimeframeProfile,
                         TradeHistoryPage)
from .market_data import import_file, inspect_path
from .migrations import CURRENT_SCHEMA_VERSION, read_schema_version
from .repository import (StaleSessionError, connect, delete_session, get_import_batch, get_symbol,
                         initialize, list_sessions, list_symbols)
from .service import (SessionNotFoundError, TradeNotFoundError, chart_history,
                      close_all_positions, close_position, create_session, fill_history_page,
                      get_state, market_order, state_response, step, toggle_indicator,
                      trade_history_page, update_settings, update_trade_review, update_trade_stop,
                      update_trade_target)


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize()
    yield


app = FastAPI(title="Trading Price Replay", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173"], allow_methods=["*"], allow_headers=["*"])


def _json_safe(value: object) -> object:
    """Strip non-finite floats from validation error payloads so a rejected 1e309-style
    number still produces a valid JSON body instead of a 500 during error reporting."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
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


# --- Replay protocol ---------------------------------------------------------
# Session creation, resume, and explicit reconciliation return the full bounded
# `ReplaySnapshot`; every replay mutation returns a `ReplayUpdate` delta whose
# `revision` is the committed revision, so clients can reject stale updates and
# detect gaps. History beyond the snapshot windows is paginated separately.


@app.post("/api/replay/sessions", response_model=ReplaySnapshot)
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


@app.get("/api/replay/sessions/{session_id}/state", response_model=ReplaySnapshot)
def session_state(session_id: str):
    return attempt(state_response, load_state(session_id))


@app.post("/api/replay/sessions/{session_id}/step", response_model=ReplayUpdate)
def session_step(session_id: str):
    return attempt(step, load_state(session_id))


@app.post("/api/replay/sessions/{session_id}/close-all", response_model=ReplayUpdate)
def session_close_all(session_id: str):
    return attempt(close_all_positions, load_state(session_id))


@app.patch("/api/replay/sessions/{session_id}/settings", response_model=ReplayUpdate)
def settings(session_id: str, request: SettingsRequest):
    return attempt(update_settings, load_state(session_id), **request.model_dump())


@app.post("/api/replay/sessions/{session_id}/indicators/{indicator_id}/toggle", response_model=ReplayUpdate)
def indicators(session_id: str, indicator_id: str):
    return attempt(toggle_indicator, load_state(session_id), indicator_id)


@app.post("/api/replay/sessions/{session_id}/orders/market", response_model=ReplayUpdate)
def orders(session_id: str, request: MarketOrderRequest):
    return attempt(market_order, load_state(session_id), **request.model_dump())


@app.get("/api/replay/sessions/{session_id}/trades", response_model=TradeHistoryPage)
def session_trades(session_id: str,
                   status: Literal["open", "closed"] = "closed",
                   limit: int = Query(50, ge=1, le=200),
                   cursor: str | None = Query(None, min_length=1)):
    """One bounded page of the session's trade history (newest first).

    Closed trades include review notes/tags; an unknown or foreign-session
    cursor is rejected with 400.
    """
    return attempt(trade_history_page, load_state(session_id), status, limit, cursor)


@app.get("/api/replay/sessions/{session_id}/fills", response_model=FillHistoryPage)
def session_fills(session_id: str,
                  limit: int = Query(100, ge=1, le=500),
                  cursor: str | None = Query(None, min_length=1)):
    """One bounded page of the session's fill history (newest first)."""
    return attempt(fill_history_page, load_state(session_id), limit, cursor)


@app.get("/api/replay/sessions/{session_id}/trades/{trade_id}/chart-history", response_model=ChartHistoryResponse)
def session_trade_chart(session_id: str, trade_id: str,
                        context_bars: int = Query(100, ge=1, le=500)):
    """A bounded historical chart window anchored around one (possibly old) trade."""
    return attempt(chart_history, load_state(session_id), trade_id, context_bars)


@app.post("/api/trades/{trade_id}/close", response_model=ReplayUpdate)
def close(trade_id: str, request: CloseRequest):
    return attempt(close_position, load_state(request.session_id), trade_id, request.quantity)


@app.put("/api/trades/{trade_id}/stop", response_model=ReplayUpdate)
def stop(trade_id: str, request: PriceRequest):
    return attempt(update_trade_stop, load_state(request.session_id), trade_id, request.price)


@app.put("/api/trades/{trade_id}/target", response_model=ReplayUpdate)
def target(trade_id: str, request: PriceRequest):
    return attempt(update_trade_target, load_state(request.session_id), trade_id, request.price)


@app.patch("/api/trades/{trade_id}/review", response_model=ReviewRecord)
def review(trade_id: str, request: ReviewRequest):
    """Persist the user review (note + tags) for one session trade."""
    return attempt(update_trade_review, load_state(request.session_id), trade_id,
                   request.review_note, request.review_tags)


@app.get("/api/replay/sessions/{session_id}/stats", response_model=SessionStats)
def stats(session_id: str):
    return state_response(load_state(session_id))["stats"]
