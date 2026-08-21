from __future__ import annotations

import copy
import json
import sqlite3
from collections import OrderedDict
from dataclasses import fields
from datetime import datetime, timezone
from pathlib import Path

from .config import DB_PATH, ensure_directories
from .domain import Fill, ReplayState, Trade, serializable, state_snapshot
from .migrations import migrate

# Bounded load windows for routine session hydration. Every open trade is
# always loaded (open risk must stay actionable); only closed trades and fills
# are capped, and at exactly the same bounds the response history is capped,
# so a routine load never reads more from disk than a response can carry.
RECENT_CLOSED_TRADES_LIMIT = 200
RECENT_FILLS_LIMIT = 1000


class StaleSessionError(Exception):
    """Raised when a save's optimistic revision no longer matches the stored row.

    The session was concurrently modified or deleted since it was loaded, so the
    whole save transaction (snapshot, event, orders, trades, fills) is rolled back.
    """


# Per-session write tracking, keyed by the revision the save started from:
# (session_id) -> (revision, persisted fill ids, {trade_id: field fingerprint}).
# Fills never change once persisted and trades are skipped by field fingerprint,
# so repeated saves neither rewrite nor even reserialize unchanged rows. The
# record is updated only after a commit, so a rolled-back CAS can never make the
# next save skip rows that were never persisted, and a revision mismatch forces
# a full rewrite. Restart-safe: an empty dict just rewrites everything once.
_PERSISTED_ROWS: OrderedDict[str, tuple[int, frozenset[str], dict[str, tuple]]] = OrderedDict()
_PERSISTED_ROWS_MAX = 64

# Bounded, revision-aware in-memory cache of hydrated states. A hit is only
# served when the cached revision still matches the database row's revision, so
# a concurrent save (here or in another process) always forces a fresh bounded
# load instead of masking a conflict; deletions and stale saves evict. The cache
# is a latency optimization only: persistent state remains authoritative, and
# callers receive their own deep copy so concurrent requests never share a
# mutable state object.
_STATE_CACHE: OrderedDict[str, tuple[int, ReplayState]] = OrderedDict()
_STATE_CACHE_MAX = 8

_TRADE_FIELDS = tuple(item.name for item in fields(Trade))

_TRADE_DATETIME_FIELDS = ("exit_time", "exit_window_start", "exit_window_end")
_FILL_DATETIME_FIELDS = ("execution_window_start", "execution_window_end")


def _trade_fingerprint(trade: Trade) -> tuple:
    """Full field tuple of a trade; equal iff no serialized field has changed.

    Comparing a small tuple of field values is far cheaper than re-serializing
    every immutable closed trade to JSON on each save, and it still catches any
    mutation (status, stops, targets, remaining quantity, realized PnL, ...).
    """
    return tuple(getattr(trade, name) for name in _TRADE_FIELDS)


def connect() -> sqlite3.Connection:
    ensure_directories()
    connection = sqlite3.connect(DB_PATH, timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def initialize() -> None:
    """Create or upgrade the database schema to the current version.

    All schema work (base tables, additive column migrations, session indexes,
    profile seeding) is delegated to migrations.migrate(), which is ordered,
    transactional, and idempotent, so re-running initialization never touches
    existing data.  A database recorded at a newer schema version is rejected.
    """
    with connect() as db:
        migrate(db)


def _cache_put(session_id: str, state: ReplayState) -> None:
    if state.revision is None:
        return
    _STATE_CACHE[session_id] = (state.revision, copy.deepcopy(state))
    _STATE_CACHE.move_to_end(session_id)
    while len(_STATE_CACHE) > _STATE_CACHE_MAX:
        _STATE_CACHE.popitem(last=False)


def _cache_pop(session_id: str) -> None:
    _STATE_CACHE.pop(session_id, None)


def _parse_trade(payload: dict[str, object]) -> Trade:
    payload = dict(payload)
    payload["entry_time"] = datetime.fromisoformat(str(payload["entry_time"]))
    for name in _TRADE_DATETIME_FIELDS:
        if payload.get(name) is not None:
            payload[name] = datetime.fromisoformat(str(payload[name]))
    return Trade(**payload)


def _parse_fill(payload: dict[str, object]) -> Fill:
    payload = dict(payload)
    payload["timestamp"] = datetime.fromisoformat(str(payload["timestamp"]))
    for name in _FILL_DATETIME_FIELDS:
        if payload.get(name) is not None:
            payload[name] = datetime.fromisoformat(str(payload[name]))
    return Fill(**payload)


def save_session(state: ReplayState, event_type: str, payload: dict[str, object] | None = None,
                 orders: list[dict[str, object]] | None = None) -> None:
    """Persist the session snapshot, event, trades, fills, indicators and optional
    order audit rows in one transaction so they commit or roll back together.

    Optimistic concurrency: a fresh state (revision None) inserts a new session at
    revision 1; a loaded state updates only when its revision still matches the
    stored row and increments the revision on success. A save based on a stale or
    deleted session raises StaleSessionError and rolls back every row of this save.
    """
    from uuid import uuid4

    now = datetime.now(timezone.utc).isoformat()
    tracked = _PERSISTED_ROWS.get(state.id)
    tracked_revision = tracked[0] if tracked is not None else None
    tracked_fill_ids = tracked[1] if tracked is not None and tracked_revision == state.revision else None
    tracked_trade_fps = tracked[2] if tracked is not None and tracked_revision == state.revision else None
    prior_revision = state.revision
    try:
        with connect() as db:
            # The snapshot excludes the normalized trade/fill histories (the
            # `trades`/`fills` tables are authoritative), so state_json stays
            # bounded no matter how long the session history grows.
            snapshot = json.dumps(state_snapshot(state), allow_nan=False)
            if state.revision is None:
                cursor = db.execute(
                    "INSERT OR IGNORE INTO replay_sessions(id,state_json,updated_at,revision) VALUES(?,?,?,1)",
                    (state.id, snapshot, now),
                )
                if cursor.rowcount == 0:
                    raise StaleSessionError(
                        f"session {state.id} already exists and was never loaded with a revision; reload and retry"
                    )
                state.revision = 1
            else:
                cursor = db.execute(
                    "UPDATE replay_sessions SET state_json=?, updated_at=?, revision=revision+1 "
                    "WHERE id=? AND revision=?",
                    (snapshot, now, state.id, state.revision),
                )
                if cursor.rowcount == 0:
                    raise StaleSessionError(
                        f"session {state.id} was modified or deleted by another client; reload and retry"
                    )
                state.revision += 1
            db.execute(
                "INSERT INTO replay_events(session_id,event_type,payload_json,created_at) VALUES(?,?,?,?)",
                (state.id, event_type, json.dumps(payload or {}, allow_nan=False), now),
            )
            db.execute("DELETE FROM session_indicators WHERE session_id=?", (state.id,))
            db.executemany(
                "INSERT INTO session_indicators(session_id,indicator_id) VALUES(?,?)",
                [(state.id, indicator_id) for indicator_id in state.enabled_indicators],
            )
            trade_fps: dict[str, tuple] = {}
            for trade in state.trades:
                fingerprint = _trade_fingerprint(trade)
                trade_fps[trade.id] = fingerprint
                if tracked_trade_fps is not None and tracked_trade_fps.get(trade.id) == fingerprint:
                    continue  # unchanged row: no reserialization, no rewrite
                payload = json.dumps(serializable(trade), allow_nan=False)
                db.execute(
                    "INSERT INTO trades(id,session_id,status,trade_json,updated_at) VALUES(?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET status=excluded.status,"
                    "trade_json=excluded.trade_json,updated_at=excluded.updated_at",
                    (trade.id, state.id, trade.status, payload, now),
                )
            for fill in state.fills:
                if tracked_fill_ids is not None and fill.id in tracked_fill_ids:
                    continue
                db.execute(
                    "INSERT OR IGNORE INTO fills(id,session_id,trade_id,fill_json,created_at) VALUES(?,?,?,?,?)",
                    (fill.id, state.id, fill.trade_id, json.dumps(serializable(fill), allow_nan=False), now),
                )
            for order in orders or []:
                db.execute(
                    "INSERT INTO orders(id,session_id,trade_id,order_type,payload_json,created_at) VALUES(?,?,?,?,?,?)",
                    (str(uuid4()), state.id, order.get("trade_id"), order["order_type"],
                     json.dumps(order["payload"], allow_nan=False), now),
                )
            # Hydrated history totals are recomputed from the authoritative
            # tables inside the same transaction so the in-memory state always
            # matches what committed.
            state.closed_trades_total = db.execute(
                "SELECT COUNT(*) FROM trades WHERE session_id=? AND status='closed'", (state.id,)
            ).fetchone()[0]
            state.fills_total = db.execute(
                "SELECT COUNT(*) FROM fills WHERE session_id=?", (state.id,)
            ).fetchone()[0]
    except BaseException:
        # The transaction rolled back; restore the in-memory revision so the
        # caller's state still matches the stored row and its next save CASes
        # against the correct revision instead of a phantom one. The state
        # diverged from the database, so the cached copy is evicted too.
        state.revision = prior_revision
        _cache_pop(state.id)
        raise
    # Only a committed save may advance the tracking; a failure above (stale CAS
    # or any post-CAS error) rolls the transaction back and leaves the record
    # untouched, so nothing that was never persisted is ever skipped later.
    _PERSISTED_ROWS[state.id] = (state.revision, frozenset(fill.id for fill in state.fills), trade_fps)
    _PERSISTED_ROWS.move_to_end(state.id)
    while len(_PERSISTED_ROWS) > _PERSISTED_ROWS_MAX:
        _PERSISTED_ROWS.popitem(last=False)
    _cache_put(state.id, state)


def load_session(session_id: str) -> ReplayState | None:
    """Hydrate a session from its bounded working set.

    Loads the snapshot, every open trade, the most recent closed trades and
    fills (the same bounds the response history is capped to), and the history
    totals from indexed counts. The complete closed/fill history stays in the
    normalized tables, reachable only through the bounded recent-history and
    paginated queries — never reconstructed in full on routine reads.
    """
    with connect() as db:
        row = db.execute("SELECT state_json, revision FROM replay_sessions WHERE id=?", (session_id,)).fetchone()
        if not row:
            return None
        value = json.loads(row["state_json"])
        value["start"] = datetime.fromisoformat(value["start"])
        value["end"] = datetime.fromisoformat(value["end"])
        # Current snapshots carry no history; legacy snapshots embed it. The
        # normalized tables are authoritative either way and rows are read in
        # insertion (rowid) order for deterministic statistics and resume.
        legacy_trades = value.pop("trades", None)
        legacy_fills = value.pop("fills", None)
        state = ReplayState(**value)
        trade_rows = db.execute(
            "SELECT trade_json FROM trades WHERE session_id=? AND status='open' ORDER BY rowid", (session_id,)
        ).fetchall()
        closed_rows = db.execute(
            "SELECT trade_json FROM trades WHERE session_id=? AND status='closed' "
            "ORDER BY rowid DESC LIMIT ?", (session_id, RECENT_CLOSED_TRADES_LIMIT)
        ).fetchall()
        fill_rows = db.execute(
            "SELECT fill_json FROM fills WHERE session_id=? ORDER BY rowid DESC LIMIT ?",
            (session_id, RECENT_FILLS_LIMIT)
        ).fetchall()
        closed_total = db.execute(
            "SELECT COUNT(*) FROM trades WHERE session_id=? AND status='closed'", (session_id,)
        ).fetchone()[0]
        fills_total = db.execute(
            "SELECT COUNT(*) FROM fills WHERE session_id=?", (session_id,)
        ).fetchone()[0]
    if trade_rows or closed_rows or fill_rows:
        state.trades = [_parse_trade(json.loads(item["trade_json"])) for item in trade_rows]
        state.trades.extend(reversed([_parse_trade(json.loads(item["trade_json"])) for item in closed_rows]))
        state.fills = list(reversed([_parse_fill(json.loads(item["fill_json"])) for item in fill_rows]))
        state.closed_trades_total = closed_total
        state.fills_total = fills_total
    else:
        # Backward compatibility: a legacy snapshot with no normalized rows yet.
        state.trades = [
            Trade(**{**item, "entry_time": datetime.fromisoformat(item["entry_time"])})
            for item in (legacy_trades or [])
        ]
        state.fills = [
            Fill(**{**item, "timestamp": datetime.fromisoformat(item["timestamp"])})
            for item in (legacy_fills or [])
        ]
        state.closed_trades_total = sum(1 for trade in state.trades if trade.status == "closed")
        state.fills_total = len(state.fills)
    # The stored row's revision is authoritative; the JSON copy is only a snapshot.
    state.revision = row["revision"]
    return state


def load_state_cached(session_id: str) -> ReplayState | None:
    """Bounded session load with a revision-aware in-memory cache.

    A cache entry is served only while its revision matches the database row,
    so it can never mask a concurrent save or deletion; the caller receives a
    deep copy. On a miss (or an evicted entry) a fresh bounded load is stored.
    """
    with connect() as db:
        row = db.execute("SELECT revision FROM replay_sessions WHERE id=?", (session_id,)).fetchone()
    if row is None:
        return None
    cached = _STATE_CACHE.get(session_id)
    if cached is not None and cached[0] == row["revision"]:
        _STATE_CACHE.move_to_end(session_id)
        return copy.deepcopy(cached[1])
    state = load_session(session_id)
    if state is not None:
        _cache_put(session_id, state)
    return state


def get_session_revision(session_id: str) -> int | None:
    with connect() as db:
        row = db.execute("SELECT revision FROM replay_sessions WHERE id=?", (session_id,)).fetchone()
    return row["revision"] if row else None


def all_trades(session_id: str) -> list[Trade]:
    """Every trade of a session in insertion order (maintenance path only)."""
    with connect() as db:
        rows = db.execute(
            "SELECT trade_json FROM trades WHERE session_id=? ORDER BY rowid", (session_id,)
        ).fetchall()
    return [_parse_trade(json.loads(item["trade_json"])) for item in rows]


def list_trades(session_id: str, status: str | None, limit: int,
                cursor: str | None = None) -> tuple[list[Trade], int, str | None]:
    """One deterministic page of trades, newest first (rowid descending).

    `cursor` is the id of the last item of the previous page; the page is the
    trades with a lower rowid, so pages are stable under concurrent appends.
    Returns (items, total, next_cursor) where next_cursor is None on the last
    page. `status` is "open" or "closed"; None means all trades.
    """
    clauses = ["session_id=?"]
    params: list[object] = [session_id]
    if status is not None:
        if status not in ("open", "closed"):
            raise ValueError("status must be 'open' or 'closed'")
        clauses.append("status=?")
        params.append(status)
    if cursor is not None:
        clauses.append("rowid < (SELECT rowid FROM trades WHERE id=?)")
        params.append(cursor)
    params.append(limit)
    with connect() as db:
        rows = db.execute(
            f"SELECT trade_json FROM trades WHERE {' AND '.join(clauses)} ORDER BY rowid DESC LIMIT ?",
            tuple(params),
        ).fetchall()
        count_clauses = ["session_id=?"]
        count_params: list[object] = [session_id]
        if status is not None:
            count_clauses.append("status=?")
            count_params.append(status)
        total = db.execute(
            f"SELECT COUNT(*) FROM trades WHERE {' AND '.join(count_clauses)}", tuple(count_params)
        ).fetchone()[0]
    items = [_parse_trade(json.loads(item["trade_json"])) for item in rows]
    next_cursor = items[-1].id if len(items) == limit and items else None
    return items, total, next_cursor


def list_fills(session_id: str, limit: int, cursor: str | None = None) -> tuple[list[Fill], int, str | None]:
    """One deterministic page of fills, newest first (rowid descending).

    Same cursor contract as `list_trades`.
    """
    clauses = ["session_id=?"]
    params: list[object] = [session_id]
    if cursor is not None:
        clauses.append("rowid < (SELECT rowid FROM fills WHERE id=?)")
        params.append(cursor)
    params.append(limit)
    with connect() as db:
        rows = db.execute(
            f"SELECT fill_json FROM fills WHERE {' AND '.join(clauses)} ORDER BY rowid DESC LIMIT ?",
            tuple(params),
        ).fetchall()
        total = db.execute("SELECT COUNT(*) FROM fills WHERE session_id=?", (session_id,)).fetchone()[0]
    items = [_parse_fill(json.loads(item["fill_json"])) for item in rows]
    next_cursor = items[-1].id if len(items) == limit and items else None
    return items, total, next_cursor


def get_trade(session_id: str, trade_id: str) -> Trade | None:
    with connect() as db:
        row = db.execute(
            "SELECT trade_json FROM trades WHERE session_id=? AND id=?", (session_id, trade_id)
        ).fetchone()
    return _parse_trade(json.loads(row["trade_json"])) if row else None


def upsert_trade_review(session_id: str, trade_id: str, note: str, tags: list[str]) -> dict[str, object]:
    """Persist the user review (note + tags) for one trade of a session."""
    now = datetime.now(timezone.utc).isoformat()
    with connect() as db:
        db.execute(
            "INSERT INTO trade_reviews(trade_id,session_id,note,tags_json,updated_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(trade_id) DO UPDATE SET note=excluded.note, "
            "tags_json=excluded.tags_json, updated_at=excluded.updated_at",
            (trade_id, session_id, note, json.dumps(tags, ensure_ascii=False), now),
        )
    return {"trade_id": trade_id, "session_id": session_id, "note": note, "tags": tags, "updated_at": now}


def get_trade_reviews(trade_ids: list[str]) -> dict[str, tuple[str, list[str]]]:
    """Review notes/tags for a batch of trade ids, keyed by trade id.

    A database that predates the trade_reviews table (created before schema v5
    and never migrated, e.g. an uninitialized default-path DB behind a
    directly constructed state) has no reviews by definition: the missing
    table is a no-op, not an error.
    """
    if not trade_ids:
        return {}
    placeholders = ",".join("?" for _ in trade_ids)
    with connect() as db:
        try:
            rows = db.execute(
                f"SELECT trade_id, note, tags_json FROM trade_reviews WHERE trade_id IN ({placeholders})",
                tuple(trade_ids),
            ).fetchall()
        except sqlite3.OperationalError:
            return {}
    return {
        row["trade_id"]: (row["note"], json.loads(row["tags_json"]))
        for row in rows
    }


def list_sessions() -> list[dict[str, object]]:
    with connect() as db:
        rows = db.execute(
            "SELECT id, state_json, updated_at FROM replay_sessions ORDER BY updated_at DESC"
        ).fetchall()
    summaries: list[dict[str, object]] = []
    for row in rows:
        try:
            value = json.loads(row["state_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        summaries.append({
            "id": row["id"],
            "symbol": value.get("symbol"),
            "start": value.get("start"),
            "end": value.get("end"),
            "status": value.get("status", "active"),
            "current_index": value.get("current_index", -1),
            "updated_at": row["updated_at"],
        })
    return summaries


def delete_session(session_id: str) -> bool:
    with connect() as db:
        for table in ("session_indicators", "orders", "trade_reviews", "trades", "fills", "replay_events"):
            db.execute(f"DELETE FROM {table} WHERE session_id=?", (session_id,))
        cursor = db.execute("DELETE FROM replay_sessions WHERE id=?", (session_id,))
        deleted = cursor.rowcount > 0
    if deleted:
        _cache_pop(session_id)
    return deleted


def list_symbols() -> list[dict[str, object]]:
    with connect() as db:
        return [dict(row) for row in db.execute("SELECT * FROM symbols ORDER BY symbol")]


def get_symbol(symbol: str) -> dict[str, object] | None:
    with connect() as db:
        row = db.execute("SELECT * FROM symbols WHERE symbol=?", (symbol,)).fetchone()
    return dict(row) if row else None


def publish_import(batch: dict[str, object], symbol_metadata: dict[str, object]) -> None:
    """Record import metadata and switch the symbol's current data version atomically.

    The fully staged version directory is already on disk; this single transaction
    makes it visible by flipping the `symbols` pointer in the same commit that
    records the batch, so readers never observe a half-published dataset and a
    failed commit leaves the previous pointer and every session untouched.
    """
    with connect() as db:
        db.execute(
            "INSERT INTO import_batches VALUES(?,?,?,?,?,?,?,?)",
            tuple(batch[key] for key in (
                "id", "symbol", "source_path", "schema_id", "status", "rows_imported", "validation_json", "created_at",
            )),
        )
        db.execute(
            """INSERT INTO symbols(symbol,asset_class,pnl_currency,price_precision,contract_multiplier,
               default_profile,first_timestamp,last_timestamp,data_version) VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(symbol) DO UPDATE SET asset_class=excluded.asset_class,pnl_currency=excluded.pnl_currency,
               price_precision=excluded.price_precision,contract_multiplier=excluded.contract_multiplier,
               default_profile=excluded.default_profile,first_timestamp=excluded.first_timestamp,
               last_timestamp=excluded.last_timestamp,data_version=excluded.data_version""",
            tuple(symbol_metadata[key] for key in (
                "symbol", "asset_class", "pnl_currency", "price_precision", "contract_multiplier",
                "default_profile", "first_timestamp", "last_timestamp", "data_version",
            )),
        )


def get_import_batch(batch_id: str) -> dict[str, object] | None:
    with connect() as db:
        row = db.execute("SELECT * FROM import_batches WHERE id=?", (batch_id,)).fetchone()
    return dict(row) if row else None
