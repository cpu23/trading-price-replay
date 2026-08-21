"""Ordered, transactional schema migrations for the price replay database.

Version history
---------------
1.  Base schema: the nine original tables (symbols, import_batches,
    replay_sessions, timeframe_profiles, session_indicators, orders, trades,
    fills, replay_events).
2.  Additive: ``symbols.data_version`` for immutable published dataset versions.
3.  Additive: ``replay_sessions.revision`` for optimistic-concurrency CAS saves.
4.  Additive: ``session_id`` indexes on the normalized child tables whose
    primary key is the row id (orders, trades, fills, replay_events), so
    per-session lookup and cascade-style deletion are indexed.  The
    session_indicators composite primary key already prefixes session_id, so it
    needs no separate index.
5.  Additive: normalized ``trades.status`` (backfilled from each row's
    ``trade_json``) with a ``(session_id, status)`` index so routine hydration
    loads only open trades by SQL filter, plus the ``trade_reviews`` table
    (user notes/tags per trade) and its ``session_id`` index.
6.  Data: one-time backfill of the persisted incremental statistics
    accumulator into each session's ``state_json``, reconstructed exactly from
    the normalized trade/fill tables (or the legacy embedded snapshot when the
    tables are empty).  Sessions whose normalized tables are non-empty also
    have the now-redundant embedded ``trades``/``fills`` arrays dropped from
    ``state_json`` so the snapshot stays bounded.  Idempotent: sessions whose
    snapshot already carries an accumulator are skipped.

Version metadata is stored in a ``schema_meta`` key/value table.  Databases
that predate the metadata (or were created without it) are baselined from their
actual shape: the presence of the additive *columns* pins the version, never an
assumption about the database's age, and an incomplete base schema (a fresh
file or one holding only some tables) baselines at 0 so the guarded v1 table
creation still fills in what is missing.  Each migration runs in its own
transaction together with the version bump, so an interrupted or failed run
rolls back completely and re-runs from the recorded version on the next call.
A database whose recorded version is newer than this application is rejected
before anything is touched.
"""

from __future__ import annotations

import sqlite3

CURRENT_SCHEMA_VERSION = 6

_VERSION_KEY = "schema_version"

# The original v1 schema, without the two additive columns that later versions
# introduced.  CREATE TABLE IF NOT EXISTS keeps this safe to re-run over any
# partial or already-migrated database.
_BASE_TABLE_NAMES = (
    "symbols", "import_batches", "replay_sessions", "timeframe_profiles",
    "session_indicators", "orders", "trades", "fills", "replay_events",
)

_BASE_TABLE_STATEMENTS = (
    "CREATE TABLE IF NOT EXISTS symbols ("
    "symbol TEXT PRIMARY KEY, asset_class TEXT NOT NULL, pnl_currency TEXT NOT NULL, "
    "price_precision INTEGER NOT NULL, contract_multiplier REAL NOT NULL, "
    "default_profile TEXT NOT NULL, first_timestamp TEXT NOT NULL, last_timestamp TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS import_batches ("
    "id TEXT PRIMARY KEY, symbol TEXT NOT NULL, source_path TEXT NOT NULL, schema_id TEXT NOT NULL, "
    "status TEXT NOT NULL, rows_imported INTEGER NOT NULL, validation_json TEXT NOT NULL, "
    "created_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS replay_sessions ("
    "id TEXT PRIMARY KEY, state_json TEXT NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS timeframe_profiles ("
    "id TEXT PRIMARY KEY, timezone TEXT NOT NULL, session_anchor TEXT, implemented INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS session_indicators ("
    "session_id TEXT NOT NULL, indicator_id TEXT NOT NULL, PRIMARY KEY(session_id, indicator_id))",
    "CREATE TABLE IF NOT EXISTS orders ("
    "id TEXT PRIMARY KEY, session_id TEXT NOT NULL, trade_id TEXT, order_type TEXT NOT NULL, "
    "payload_json TEXT NOT NULL, created_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS trades ("
    "id TEXT PRIMARY KEY, session_id TEXT NOT NULL, trade_json TEXT NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS fills ("
    "id TEXT PRIMARY KEY, session_id TEXT NOT NULL, trade_id TEXT NOT NULL, fill_json TEXT NOT NULL, "
    "created_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS replay_events ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, event_type TEXT NOT NULL, "
    "payload_json TEXT NOT NULL, created_at TEXT NOT NULL)",
)

# (index name, table) for every normalized child table whose session lookups
# are not already covered by its primary key.
_SESSION_ID_INDEXES = (
    ("ix_orders_session_id", "orders"),
    ("ix_trades_session_id", "trades"),
    ("ix_fills_session_id", "fills"),
    ("ix_replay_events_session_id", "replay_events"),
)

_PROFILE_SEEDS = (
    ("utc_aligned", "UTC", "00:00", 1),
    ("new_york_close", "America/New_York", "17:00", 1),
    ("custom_session_anchor", "configurable", None, 0),
)


class SchemaVersionError(Exception):
    """The database records a schema version newer than this application."""


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _has_column(connection: sqlite3.Connection, table: str, column: str) -> bool:
    if not _table_exists(connection, table):
        return False
    return any(row[1] == column for row in connection.execute(f"PRAGMA table_info({table})"))


def _detect_baseline(connection: sqlite3.Connection) -> int:
    """Infer the schema version of an unversioned database from its shape.

    The additive upgrades are column additions, so the columns themselves
    (never the database's age) pin the baseline, and a database that predates
    the complete v1 schema (a brand-new file or one holding only some tables)
    baselines at 0 so the guarded v1 table creation still runs:

    - any of the nine base tables missing -> 0 (incomplete v1 or fresh file)
    - all base tables present, no additive columns -> 1
    - ``symbols.data_version`` present -> 2
    - ``replay_sessions.revision`` present -> 3
    """
    if not all(_table_exists(connection, table) for table in _BASE_TABLE_NAMES):
        return 0
    if not _has_column(connection, "symbols", "data_version"):
        return 1
    if not _has_column(connection, "replay_sessions", "revision"):
        return 2
    return 3


def _run_in_transaction(connection: sqlite3.Connection, fn) -> None:
    """Run ``fn(connection)`` inside an explicit transaction.

    The sqlite3 module's implicit transaction management does not cover DDL, so
    atomicity for migrations is guaranteed with explicit BEGIN/COMMIT/ROLLBACK
    while the connection is in autocommit mode.
    """
    connection.execute("BEGIN")
    try:
        fn(connection)
    except BaseException:
        try:
            connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass  # keep the original error; nothing further to roll back
        raise
    connection.execute("COMMIT")


def _ensure_metadata_table(connection: sqlite3.Connection) -> None:
    def _body(c: sqlite3.Connection) -> None:
        c.execute("CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")

    _run_in_transaction(connection, _body)


def _read_recorded_version(connection: sqlite3.Connection) -> int | None:
    try:
        row = connection.execute(
            "SELECT value FROM schema_meta WHERE key=?", (_VERSION_KEY,)
        ).fetchone()
    except sqlite3.OperationalError:
        return None  # no metadata table yet: an unversioned database
    if row is None:
        return None
    try:
        version = int(row[0])
    except (TypeError, ValueError):
        raise SchemaVersionError(
            f"invalid schema version metadata {row[0]!r} in schema_meta; refusing to touch the database"
        ) from None
    if version < 0:
        raise SchemaVersionError(
            f"invalid schema version {version} recorded in schema_meta; refusing to touch the database"
        )
    return version


def _write_recorded_version(connection: sqlite3.Connection, version: int) -> None:
    connection.execute(
        "INSERT OR REPLACE INTO schema_meta(key,value) VALUES(?,?)", (_VERSION_KEY, str(version))
    )


def _migrate_v1_base_schema(connection: sqlite3.Connection) -> None:
    for statement in _BASE_TABLE_STATEMENTS:
        connection.execute(statement)


def _migrate_v2_data_version(connection: sqlite3.Connection) -> None:
    if not _has_column(connection, "symbols", "data_version"):
        connection.execute("ALTER TABLE symbols ADD COLUMN data_version TEXT")


def _migrate_v3_revision(connection: sqlite3.Connection) -> None:
    if not _has_column(connection, "replay_sessions", "revision"):
        # Existing rows are treated as revision 1, their first CAS save.
        connection.execute("ALTER TABLE replay_sessions ADD COLUMN revision INTEGER NOT NULL DEFAULT 1")


def _migrate_v4_session_indexes(connection: sqlite3.Connection) -> None:
    for index_name, table in _SESSION_ID_INDEXES:
        connection.execute(f"CREATE INDEX IF NOT EXISTS {index_name} ON {table}(session_id)")


def _migrate_v5_trade_status_and_reviews(connection: sqlite3.Connection) -> None:
    if not _has_column(connection, "trades", "status"):
        # New rows default to 'open' (matching a fresh trade); the backfill
        # below then promotes the closed ones from their authoritative JSON.
        connection.execute("ALTER TABLE trades ADD COLUMN status TEXT NOT NULL DEFAULT 'open'")
    # Backfill from trade_json: only the accepted values are honored, anything
    # else (missing or malformed) stays 'open', the safe default for a row
    # whose status cannot be proven. Idempotent: re-running is a no-op.
    connection.execute(
        "UPDATE trades SET status='closed' WHERE status='open' AND json_extract(trade_json, '$.status')='closed'"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS ix_trades_session_status ON trades(session_id, status)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS trade_reviews ("
        "trade_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, note TEXT NOT NULL, "
        "tags_json TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS ix_trade_reviews_session_id ON trade_reviews(session_id)"
    )


def _v6_parse_trade(payload: dict) -> "object":
    from datetime import datetime

    from .domain import Trade

    payload = dict(payload)
    payload["entry_time"] = datetime.fromisoformat(str(payload["entry_time"]))
    for name in ("exit_time", "exit_window_start", "exit_window_end"):
        if payload.get(name) is not None:
            payload[name] = datetime.fromisoformat(str(payload[name]))
    return Trade(**payload)


def _v6_parse_fill(payload: dict) -> "object":
    from datetime import datetime

    from .domain import Fill

    payload = dict(payload)
    payload["timestamp"] = datetime.fromisoformat(str(payload["timestamp"]))
    for name in ("execution_window_start", "execution_window_end"):
        if payload.get(name) is not None:
            payload[name] = datetime.fromisoformat(str(payload[name]))
    return Fill(**payload)


def _migrate_v6_stats_accumulator(connection: sqlite3.Connection) -> None:
    """One-time exact backfill of the persisted statistics accumulator.

    For every session whose snapshot has no accumulator yet, the normalized
    trade/fill tables are the source (matching how `load_session` chooses its
    source); only sessions with empty tables fall back to the legacy embedded
    snapshot. The reconstruction reuses the same incremental booking functions
    the live engine uses, in ledger order, so the result is numerically
    identical to a full-history scan. When the tables are authoritative the
    redundant embedded ``trades``/``fills`` arrays are dropped from
    ``state_json`` so the snapshot stays bounded.
    """
    import json
    from dataclasses import asdict
    from datetime import datetime

    from .domain import ReplayState
    from .stats import build_accumulator_from_history

    for row in connection.execute("SELECT id, state_json FROM replay_sessions"):
        session_id = row[0]
        try:
            value = json.loads(row[1])
            if not isinstance(value, dict) or "accumulator" in value:
                continue
            trade_rows = connection.execute(
                "SELECT trade_json FROM trades WHERE session_id=? ORDER BY rowid", (session_id,)
            ).fetchall()
            fill_rows = connection.execute(
                "SELECT fill_json FROM fills WHERE session_id=? ORDER BY rowid", (session_id,)
            ).fetchall()
            if trade_rows or fill_rows:
                trades = [_v6_parse_trade(json.loads(item["trade_json"])) for item in trade_rows]
                fills = [_v6_parse_fill(json.loads(item["fill_json"])) for item in fill_rows]
                # The normalized tables are authoritative: the embedded copies
                # in the snapshot are now redundant and would only bloat it.
                value.pop("trades", None)
                value.pop("fills", None)
            else:
                trades = [_v6_parse_trade(item) for item in (value.get("trades") or [])]
                fills = [_v6_parse_fill(item) for item in (value.get("fills") or [])]
            history_value = {
                key: item for key, item in value.items() if key not in ("trades", "fills", "accumulator")
            }
            history_value["start"] = datetime.fromisoformat(str(history_value["start"]))
            history_value["end"] = datetime.fromisoformat(str(history_value["end"]))
            state = ReplayState(**history_value)
            accumulator = build_accumulator_from_history(state, list(trades), list(fills))
            value["accumulator"] = asdict(accumulator)
            connection.execute(
                "UPDATE replay_sessions SET state_json=? WHERE id=?",
                (json.dumps(value, allow_nan=False), session_id),
            )
        except (TypeError, ValueError, KeyError, AttributeError, OverflowError):
            # A session whose snapshot or ledger rows predate the fields the
            # reconstruction needs keeps its snapshot untouched; the read path
            # falls back to a full-history scan for it. One odd session must
            # never block the backfill of the rest.
            continue


_MIGRATIONS = {
    1: _migrate_v1_base_schema,
    2: _migrate_v2_data_version,
    3: _migrate_v3_revision,
    4: _migrate_v4_session_indexes,
    5: _migrate_v5_trade_status_and_reviews,
    6: _migrate_v6_stats_accumulator,
}


def _seed_timeframe_profiles(connection: sqlite3.Connection) -> None:
    connection.executemany(
        "INSERT OR IGNORE INTO timeframe_profiles(id,timezone,session_anchor,implemented) VALUES(?,?,?,?)",
        _PROFILE_SEEDS,
    )


def read_schema_version(connection: sqlite3.Connection) -> int | None:
    """Return the recorded schema version, or None for an unversioned database.

    None means the database carries no version metadata: it predates versioning
    or was never initialized.  Present-but-invalid metadata (a non-integer or
    negative version) raises SchemaVersionError, and a corrupt or non-SQLite
    file raises sqlite3.Error, so callers can distinguish "unknown, safe to
    migrate" from "malformed" and "newer than this application".
    """
    return _read_recorded_version(connection)


def migrate(connection: sqlite3.Connection) -> None:
    """Bring an open database to CURRENT_SCHEMA_VERSION.

    - Brand-new databases (no tables) are created in full.
    - Unversioned legacy databases are baselined from their detected shape
      (columns, never age) and then every guarded migration runs in order from
      v1; each is a no-op for effects that are already present, so no row is
      touched.  A database missing any base table is completed the same way,
      even when it carries recorded metadata.
    - Versioned databases resume from the recorded version; each migration
      commits atomically with its version bump, so a failed or interrupted
      migration rolls back and re-runs on the next call.
    - A recorded version newer than CURRENT_SCHEMA_VERSION raises
      SchemaVersionError before anything is touched.
    - Timeframe profile seeding stays idempotent (INSERT OR IGNORE).

    The connection's isolation level is temporarily switched to autocommit for
    explicit transaction control and restored afterwards.  WAL is not required.
    """
    previous_isolation = connection.isolation_level
    connection.isolation_level = None
    try:
        _ensure_metadata_table(connection)
        recorded = _read_recorded_version(connection)
        if recorded is not None and recorded > CURRENT_SCHEMA_VERSION:
            raise SchemaVersionError(
                f"database schema version {recorded} is newer than this application "
                f"(supports up to {CURRENT_SCHEMA_VERSION}); refusing to touch it"
            )
        if recorded is None:
            baseline = _detect_baseline(connection)
            _run_in_transaction(connection, lambda c: _write_recorded_version(c, baseline))
            # Unversioned databases carry no migration history, so every
            # guarded migration runs in order from v1; each one is a no-op for
            # effects that are already present.
            start = 1
        else:
            baseline = recorded
            # A recorded version implies the full v1 base schema was created,
            # but a partial database can still carry metadata (a hand-built
            # fragment or a dropped table); recreate what is missing by
            # re-running every guarded migration in order.
            incomplete = not all(_table_exists(connection, table) for table in _BASE_TABLE_NAMES)
            start = 1 if incomplete else baseline + 1
        for version in range(start, CURRENT_SCHEMA_VERSION + 1):
            migration_fn = _MIGRATIONS[version]
            _run_in_transaction(
                connection, lambda c, fn=migration_fn, v=version: (fn(c), _write_recorded_version(c, v))
            )
        _run_in_transaction(connection, _seed_timeframe_profiles)
    finally:
        connection.isolation_level = previous_isolation
