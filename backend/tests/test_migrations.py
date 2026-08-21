"""Schema migration contracts: fresh installs, legacy baselining, and durability.

Every test builds its own database by hand (never through migrate) so the
upgrade path is exercised from the exact legacy shape being claimed, then
asserts observable state: recorded version, tables/columns/indexes, and that
every pre-existing row survived.
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from app import config, migrations, repository
from app.migrations import (
    CURRENT_SCHEMA_VERSION,
    SchemaVersionError,
    migrate,
    read_schema_version,
)

EXPECTED_TABLES = {
    "symbols", "import_batches", "replay_sessions", "timeframe_profiles",
    "session_indicators", "orders", "trades", "fills", "replay_events",
    "schema_meta",
}
EXPECTED_SESSION_INDEXES = {
    "ix_orders_session_id", "ix_trades_session_id", "ix_fills_session_id",
    "ix_replay_events_session_id",
}


def _tables(db: sqlite3.Connection) -> set[str]:
    rows = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {row[0] for row in rows}


def _columns(db: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in db.execute(f"PRAGMA table_info({table})")]


def _indexes(db: sqlite3.Connection) -> set[str]:
    rows = db.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
    return {row[0] for row in rows}


def _set_version(db: sqlite3.Connection, version: int | str) -> None:
    """Stamp the version metadata directly, as an interrupted newer-version or
    corrupted database would carry it."""
    db.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('schema_version',?)", (str(version),))
    db.commit()


def _build_legacy(db_path, data_version=None, revision=None) -> None:
    """Create a hand-built database with the pre-versioning nine-table schema.

    ``data_version``/``revision`` simulate the additive upgrades having been
    applied by an older app without version metadata being present.  Every
    table gets a row so upgrades can be proven lossless.
    """
    with sqlite3.connect(db_path) as db:
        db.executescript(
            """
            CREATE TABLE symbols (
              symbol TEXT PRIMARY KEY, asset_class TEXT NOT NULL, pnl_currency TEXT NOT NULL,
              price_precision INTEGER NOT NULL, contract_multiplier REAL NOT NULL,
              default_profile TEXT NOT NULL, first_timestamp TEXT NOT NULL, last_timestamp TEXT NOT NULL
            );
            CREATE TABLE import_batches (
              id TEXT PRIMARY KEY, symbol TEXT NOT NULL, source_path TEXT NOT NULL, schema_id TEXT NOT NULL,
              status TEXT NOT NULL, rows_imported INTEGER NOT NULL, validation_json TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE replay_sessions (
              id TEXT PRIMARY KEY, state_json TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE timeframe_profiles (
              id TEXT PRIMARY KEY, timezone TEXT NOT NULL, session_anchor TEXT, implemented INTEGER NOT NULL
            );
            CREATE TABLE session_indicators (
              session_id TEXT NOT NULL, indicator_id TEXT NOT NULL, PRIMARY KEY(session_id, indicator_id)
            );
            CREATE TABLE orders (
              id TEXT PRIMARY KEY, session_id TEXT NOT NULL, trade_id TEXT, order_type TEXT NOT NULL,
              payload_json TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE trades (
              id TEXT PRIMARY KEY, session_id TEXT NOT NULL, trade_json TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE fills (
              id TEXT PRIMARY KEY, session_id TEXT NOT NULL, trade_id TEXT NOT NULL, fill_json TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE replay_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, event_type TEXT NOT NULL,
              payload_json TEXT NOT NULL, created_at TEXT NOT NULL
            );
            """
        )
        if data_version is not None:
            db.execute("ALTER TABLE symbols ADD COLUMN data_version TEXT")
        if revision is not None:
            db.execute("ALTER TABLE replay_sessions ADD COLUMN revision INTEGER NOT NULL DEFAULT 1")
        db.execute(
            "INSERT INTO symbols(symbol,asset_class,pnl_currency,price_precision,contract_multiplier,"
            "default_profile,first_timestamp,last_timestamp"
            + (",data_version" if data_version is not None else "")
            + ") VALUES(?,?,?,?,?,?,?,?"
            + (",?" if data_version is not None else "")
            + ")",
            ("EURUSD", "forex", "USD", 5, 100000.0, "utc_aligned",
             "2026-01-01T00:00:00+00:00", "2026-01-01T23:59:00+00:00")
            + ((data_version,) if data_version is not None else ()),
        )
        db.execute(
            "INSERT INTO import_batches VALUES(?,?,?,?,?,?,?,?)",
            ("b1", "EURUSD", "/tmp/source.csv", "schema-v1", "published", 100, "{}", "2026-01-02T00:00:00+00:00"),
        )
        # A closed legacy trade with its full fill ledger, in the pre-cost
        # contract shape (no cost totals, no chart anchors, no exit metadata):
        # the v6 accumulator backfill and the v7 metadata backfill must be able
        # to reconstruct every field from it exactly.
        trade_payload = {
            "id": "t1", "session_id": "s1", "direction": "long",
            "initial_quantity": 1.0, "remaining_quantity": 0.0,
            "entry_time": "2026-01-01T00:01:00+00:00", "entry_price": 10.0,
            "stop_price": 9.9, "target_price": 10.2, "initial_risk": 10.0,
            "realized_pnl": 5.0, "status": "closed",
        }
        fill_payloads = [
            {"id": "f1", "trade_id": "t1", "session_id": "s1",
             "timestamp": "2026-01-01T00:01:00+00:00", "price": 10.0,
             "quantity": 1.0, "reason": "entry", "pnl": 0.0},
            {"id": "f2", "trade_id": "t1", "session_id": "s1",
             "timestamp": "2026-01-01T00:30:00+00:00", "price": 10.1,
             "quantity": 1.0, "reason": "manual", "pnl": 5.0},
        ]
        # The legacy snapshot embedded the complete history; the normalized
        # tables mirror it.
        db.execute(
            "INSERT INTO replay_sessions(id,state_json,updated_at"
            + (",revision" if revision is not None else "")
            + ") VALUES(?,?,?"
            + (",?" if revision is not None else "")
            + ")",
            ("s1", json.dumps({
                "id": "s1", "symbol": "EURUSD",
                "start": "2026-01-01T00:00:00+00:00",
                "end": "2026-01-01T23:59:00+00:00",
                "profile": "utc_aligned",
                "trades": [trade_payload],
                "fills": fill_payloads,
            }), "2026-01-02T00:00:00+00:00")
            + ((revision,) if revision is not None else ()),
        )
        # A customized built-in profile row plus a custom one: seeding must not
        # clobber either, and must only add the missing built-ins.
        db.executemany(
            "INSERT INTO timeframe_profiles VALUES(?,?,?,?)",
            [("utc_aligned", "America/Los_Angeles", "09:30", 1), ("my_profile", "Europe/London", "08:00", 1)],
        )
        db.execute("INSERT INTO session_indicators VALUES(?,?)", ("s1", "ema20"))
        db.execute(
            "INSERT INTO orders VALUES(?,?,?,?,?,?)",
            ("o1", "s1", "t1", "market_entry", '{"qty": 1}', "2026-01-02T00:00:00+00:00"),
        )
        db.execute(
            "INSERT INTO trades VALUES(?,?,?,?)",
            ("t1", "s1", json.dumps(trade_payload), "2026-01-02T00:00:00+00:00"),
        )
        for fill in fill_payloads:
            db.execute(
                "INSERT INTO fills VALUES(?,?,?,?,?)",
                (fill["id"], "s1", "t1", json.dumps(fill), "2026-01-02T00:00:00+00:00"),
            )
        db.execute(
            "INSERT INTO replay_events(id,session_id,event_type,payload_json,created_at) VALUES(?,?,?,?,?)",
            (1, "s1", "session_started", "{}", "2026-01-02T00:00:00+00:00"),
        )


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "migrate.sqlite3"


def test_fresh_database_reaches_current_version(db_path):
    with sqlite3.connect(db_path) as db:
        migrate(db)
        assert read_schema_version(db) == CURRENT_SCHEMA_VERSION
        assert EXPECTED_TABLES <= _tables(db)
        assert "data_version" in _columns(db, "symbols")
        assert "revision" in _columns(db, "replay_sessions")
        assert EXPECTED_SESSION_INDEXES <= _indexes(db)
        profiles = db.execute(
            "SELECT id,timezone,session_anchor,implemented FROM timeframe_profiles ORDER BY id"
        ).fetchall()
        assert [tuple(row) for row in profiles] == [
            ("custom_session_anchor", "configurable", None, 0),
            ("new_york_close", "America/New_York", "17:00", 1),
            ("utc_aligned", "UTC", "00:00", 1),
        ]


def test_oldest_legacy_schema_upgrades_without_data_loss(db_path):
    _build_legacy(db_path)
    with sqlite3.connect(db_path) as db:
        assert read_schema_version(db) is None
        migrate(db)
        assert read_schema_version(db) == CURRENT_SCHEMA_VERSION
        assert "data_version" in _columns(db, "symbols")
        assert "revision" in _columns(db, "replay_sessions")
        assert EXPECTED_SESSION_INDEXES <= _indexes(db)
        # Every pre-existing row survived; the additive columns got defaults.
        assert tuple(db.execute("SELECT symbol,data_version FROM symbols").fetchone()) == ("EURUSD", None)
        assert tuple(db.execute("SELECT id,revision FROM replay_sessions").fetchone()) == ("s1", 1)
        for table, expected in (
            ("import_batches", 1), ("session_indicators", 1), ("orders", 1),
            ("trades", 1), ("fills", 2), ("replay_events", 1),
        ):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == expected
        # Seeding is INSERT OR IGNORE: the customized built-in and the custom
        # profile survive untouched, and only the missing built-ins are added.
        assert tuple(db.execute("SELECT timezone FROM timeframe_profiles WHERE id='utc_aligned'").fetchone())[0] \
            == "America/Los_Angeles"
        assert tuple(db.execute("SELECT timezone FROM timeframe_profiles WHERE id='my_profile'").fetchone())[0] \
            == "Europe/London"
        assert db.execute("SELECT COUNT(*) FROM timeframe_profiles").fetchone()[0] == 4


def test_partial_legacy_schema_is_completed(db_path):
    # A pre-versioning database holding only some tables (the shape older
    # fragments leave behind) must get every missing table plus the additive
    # columns, with existing rows preserved and revision backfilled.
    with sqlite3.connect(db_path) as db:
        db.executescript(
            """
            CREATE TABLE replay_sessions (
              id TEXT PRIMARY KEY, state_json TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            """
        )
        db.execute(
            "INSERT INTO replay_sessions VALUES(?,?,?)",
            ("s1", json.dumps({
                "id": "s1", "symbol": "EURUSD",
                "start": "2026-01-01T00:00:00+00:00",
                "end": "2026-01-01T23:59:00+00:00",
                "profile": "utc_aligned",
            }), "2026-01-02T00:00:00+00:00"),
        )
    with sqlite3.connect(db_path) as db:
        assert read_schema_version(db) is None
        migrate(db)
        assert read_schema_version(db) == CURRENT_SCHEMA_VERSION
        assert EXPECTED_TABLES <= _tables(db)
        assert "data_version" in _columns(db, "symbols")
        assert "revision" in _columns(db, "replay_sessions")
        assert tuple(db.execute("SELECT id,revision FROM replay_sessions").fetchone()) == ("s1", 1)
        assert db.execute("SELECT COUNT(*) FROM timeframe_profiles").fetchone()[0] == 3


def test_current_unversioned_database_is_baselined(db_path):
    _build_legacy(db_path, data_version="v9", revision=5)
    with sqlite3.connect(db_path) as db:
        assert read_schema_version(db) is None
        migrate(db)
        assert read_schema_version(db) == CURRENT_SCHEMA_VERSION
        # Baseline detected from columns, not age: both values preserved.
        assert tuple(db.execute("SELECT symbol,data_version FROM symbols").fetchone()) == ("EURUSD", "v9")
        assert tuple(db.execute("SELECT id,revision FROM replay_sessions").fetchone()) == ("s1", 5)
        # The v4 index migration still ran.
        assert EXPECTED_SESSION_INDEXES <= _indexes(db)
        assert db.execute("SELECT COUNT(*) FROM replay_events").fetchone()[0] == 1


def test_migrate_is_idempotent_and_rerunnable(db_path):
    with sqlite3.connect(db_path) as db:
        for _ in range(3):
            migrate(db)
        assert read_schema_version(db) == CURRENT_SCHEMA_VERSION
        assert db.execute("SELECT COUNT(*) FROM schema_meta").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM timeframe_profiles").fetchone()[0] == 3
        assert db.execute("SELECT COUNT(*) FROM symbols").fetchone()[0] == 0
        assert len(_indexes(db) & EXPECTED_SESSION_INDEXES) == 4


def test_migrate_resumes_from_recorded_version_after_interruption(db_path):
    # A database already carrying version 2 (data_version present, no revision
    # column, no metadata table) resumes from where it stopped.
    _build_legacy(db_path, data_version="v1")
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        _set_version(db, 2)
        migrate(db)
        assert read_schema_version(db) == CURRENT_SCHEMA_VERSION
        assert "revision" in _columns(db, "replay_sessions")
        assert tuple(db.execute("SELECT id,revision FROM replay_sessions").fetchone()) == ("s1", 1)
        assert tuple(db.execute("SELECT symbol,data_version FROM symbols").fetchone()) == ("EURUSD", "v1")


def test_failed_migration_rolls_back_atomically(db_path, monkeypatch):
    _build_legacy(db_path)
    real_revision_migration = migrations._MIGRATIONS[3]

    def failing_revision_migration(connection):
        real_revision_migration(connection)  # the ALTER runs...
        raise RuntimeError("boom")  # ...then the seam fails

    monkeypatch.setitem(migrations._MIGRATIONS, 3, failing_revision_migration)
    with sqlite3.connect(db_path) as db:
        with pytest.raises(RuntimeError, match="boom"):
            migrate(db)
        # Migration 2 committed; migration 3 rolled back wholesale: neither the
        # column nor the version bump leaked, and no row was lost.
        assert read_schema_version(db) == 2
        assert "data_version" in _columns(db, "symbols")
        assert "revision" not in _columns(db, "replay_sessions")
        assert db.execute("SELECT COUNT(*) FROM replay_sessions").fetchone()[0] == 1
    monkeypatch.setitem(migrations._MIGRATIONS, 3, real_revision_migration)

    # A later run (seam healed) completes the upgrade from the recorded version.
    with sqlite3.connect(db_path) as db:
        migrate(db)
        assert read_schema_version(db) == CURRENT_SCHEMA_VERSION
        assert "revision" in _columns(db, "replay_sessions")
        assert tuple(db.execute("SELECT id,revision FROM replay_sessions").fetchone()) == ("s1", 1)
        assert db.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 2


def test_newer_recorded_version_is_rejected_before_touching_schema(db_path):
    with sqlite3.connect(db_path) as db:
        migrate(db)  # establishes schema_meta at CURRENT_SCHEMA_VERSION
        _set_version(db, CURRENT_SCHEMA_VERSION + 1)
        with pytest.raises(SchemaVersionError, match="newer"):
            migrate(db)
        # The rejected call changed nothing: version, tables, and data intact.
        assert read_schema_version(db) == CURRENT_SCHEMA_VERSION + 1
        assert EXPECTED_TABLES <= _tables(db)
        assert db.execute("SELECT COUNT(*) FROM timeframe_profiles").fetchone()[0] == 3


def test_malformed_recorded_version_is_rejected(db_path):
    with sqlite3.connect(db_path) as db:
        migrate(db)  # establishes schema_meta at CURRENT_SCHEMA_VERSION
        for bad in ("abc", "4.5", "-7"):
            _set_version(db, bad)
            with pytest.raises(SchemaVersionError, match="schema version"):
                migrate(db)
            # The rejected call changed nothing: the malformed value is still
            # stored verbatim and the schema is untouched.
            stored = db.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()[0]
            assert stored == str(bad)
            assert db.execute("SELECT COUNT(*) FROM symbols").fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM timeframe_profiles").fetchone()[0] == 3
            # The public reader reports the same actionable failure.
            with pytest.raises(SchemaVersionError, match="schema version"):
                read_schema_version(db)
        # Healing the metadata lets migration proceed normally.
        _set_version(db, CURRENT_SCHEMA_VERSION - 1)
        migrate(db)
        assert read_schema_version(db) == CURRENT_SCHEMA_VERSION


def test_repository_initialize_delegates_and_preserves_legacy_data(db_path, monkeypatch):
    monkeypatch.setattr(config, "RAW_ROOT", db_path.parent / "raw")
    monkeypatch.setattr(config, "OHLCV_ROOT", db_path.parent / "ohlcv")
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(repository, "DB_PATH", db_path)
    _build_legacy(db_path)

    repository.initialize()
    with sqlite3.connect(db_path) as db:
        assert read_schema_version(db) == CURRENT_SCHEMA_VERSION
        assert "revision" in _columns(db, "replay_sessions")
        assert tuple(db.execute("SELECT id,revision FROM replay_sessions").fetchone()) == ("s1", 1)
        assert db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1

    repository.initialize()  # idempotent: no errors, no duplicate rows
    with sqlite3.connect(db_path) as db:
        assert read_schema_version(db) == CURRENT_SCHEMA_VERSION
        assert db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM timeframe_profiles").fetchone()[0] == 4


# --- v6/v7 strict backfill semantics -----------------------------------------


def _legacy_session_history(closed_count: int, fills_per_trade: int, open_count: int):
    """Deterministic legacy trade/fill payloads in the pre-cost contract shape
    (no cost components, no chart anchors, no exit metadata): one entry plus
    partial exits plus a final exit per trade, with mixed winning/losing
    outcomes so every R aggregate is exercised."""
    base = "2026-01-01T00:00:00+00:00"
    from datetime import timedelta

    t0 = datetime.fromisoformat(base)
    trades: list[dict] = []
    fills: list[dict] = []
    for index in range(closed_count + open_count):
        trade_id = f"t{index}"
        open_ = index >= closed_count
        entry = t0 + timedelta(minutes=index * (fills_per_trade + 1))
        losing = index % 3 == 0
        final_pnl = -6.0 if losing else 0.5
        trades.append({
            "id": trade_id, "session_id": "s1", "direction": "long" if index % 2 == 0 else "short",
            "initial_quantity": 1.0, "remaining_quantity": 1.0 if open_ else 0.0,
            "entry_time": entry.isoformat(), "entry_price": 10.0,
            "stop_price": 9.5, "target_price": 11.0, "initial_risk": 0.5,
            "realized_pnl": 4.0 + final_pnl if not open_ else 0.0,
            "status": "open" if open_ else "closed",
        })
        for fill_index in range(fills_per_trade if not open_ else 1):
            is_entry = fill_index == 0
            is_final = fill_index == fills_per_trade - 1 and not open_
            if is_entry:
                pnl = 0.0
            elif is_final:
                pnl = final_pnl
            else:
                pnl = 1.0
            fills.append({
                "id": f"f{index}-{fill_index}", "trade_id": trade_id, "session_id": "s1",
                "timestamp": (entry + timedelta(minutes=fill_index)).isoformat(),
                "price": 10.0, "quantity": 1.0 if is_entry else 0.25,
                "reason": "entry" if is_entry else ("manual" if not is_final else "target"),
                "pnl": pnl,
            })
    return trades, fills


def _build_pre_v6_db(db_path, closed_count: int, fills_per_trade: int,
                     open_count: int) -> tuple[list[dict], list[dict]]:
    """A real pre-v6 database: current tables (v6/v7 are data-only migrations)
    stamped at version 5, holding legacy-shaped ledger rows and a snapshot with
    the complete embedded history."""
    trades, fills = _legacy_session_history(closed_count, fills_per_trade, open_count)
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as db:
        migrate(db)
        _set_version(db, 5)
        db.execute(
            "INSERT INTO replay_sessions(id,state_json,updated_at) VALUES(?,?,?)",
            ("s1", json.dumps({
                "id": "s1", "symbol": "EURUSD",
                "start": "2026-01-01T00:00:00+00:00",
                "end": "2026-02-01T00:00:00+00:00",
                "profile": "utc_aligned",
                "trades": trades, "fills": fills,
            }), "2026-01-02T00:00:00+00:00"),
        )
        for trade in trades:
            db.execute(
                "INSERT INTO trades(id,session_id,status,trade_json,updated_at) "
                "VALUES(?,?,?,?,?)",
                (trade["id"], "s1", trade["status"], json.dumps(trade),
                 "2026-01-02T00:00:00+00:00"),
            )
        for fill in fills:
            db.execute(
                "INSERT INTO fills(id,session_id,trade_id,fill_json,created_at) VALUES(?,?,?,?,?)",
                (fill["id"], "s1", fill["trade_id"], json.dumps(fill), "2026-01-02T00:00:00+00:00"),
            )
    return trades, fills


def test_v6_backfill_failure_keeps_version_5(db_path):
    """A session that should be backfilled but cannot be reconstructed exactly
    fails migration v6: the whole v6 transaction rolls back, the schema version
    stays at 5, and the snapshot is left untouched (no false accumulator)."""
    with sqlite3.connect(db_path) as db:
        migrate(db)
        _set_version(db, 5)
        db.execute(
            "INSERT INTO replay_sessions(id,state_json,updated_at) VALUES(?,?,?)",
            ("s1", json.dumps({
                "id": "s1", "symbol": "EURUSD",
                "start": "2026-01-01T00:00:00+00:00",
                "end": "2026-01-01T23:59:00+00:00",
                "profile": "utc_aligned",
            }), "2026-01-02T00:00:00+00:00"),
        )
        db.execute(
            "INSERT INTO trades(id,session_id,trade_json,updated_at) VALUES(?,?,?,?)",
            ("t1", "s1", '{"id": "t1"}', "2026-01-02T00:00:00+00:00"),
        )
    with sqlite3.connect(db_path) as db:
        with pytest.raises(Exception):
            migrate(db)
        assert read_schema_version(db) == 5
        value = json.loads(db.execute("SELECT state_json FROM replay_sessions").fetchone()[0])
        assert "accumulator" not in value


def test_pre_v6_backup_restores_with_exact_accumulator(tmp_path, monkeypatch):
    """The row_factory regression: a pre-v6 database whose history exceeds every
    bounded cap is backed up and restored through the maintenance path, whose
    candidate connection is a plain ``sqlite3.connect()`` (rows are tuples, not
    mappings). The v6 backfill must reconstruct the accumulator exactly from
    the normalized ledger, drop the redundant embedded history, and the
    restored session's statistics must equal the full-history reference."""
    from app.domain import ReplayState
    from app.maintenance import backup_database, restore_database
    from app.stats import calculate_stats, calculate_stats_from_history

    closed_count, fills_per_trade, open_count = 210, 6, 2
    live = tmp_path / "sessions" / "price_replay.sqlite3"
    monkeypatch.setattr(config, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(config, "RAW_ROOT", tmp_path / "raw")
    monkeypatch.setattr(config, "OHLCV_ROOT", tmp_path / "ohlcv")
    monkeypatch.setattr(config, "DB_PATH", live)
    monkeypatch.setattr(repository, "DB_PATH", live)

    _, fills = _build_pre_v6_db(live, closed_count, fills_per_trade, open_count)
    assert closed_count > repository.RECENT_CLOSED_TRADES_LIMIT
    assert len(fills) > repository.RECENT_FILLS_LIMIT

    backup = tmp_path / "pre-v6-backup.sqlite3"
    backup_database(backup)
    restore_database(backup)

    with sqlite3.connect(live) as db:
        assert read_schema_version(db) == CURRENT_SCHEMA_VERSION
        value = json.loads(
            db.execute("SELECT state_json FROM replay_sessions WHERE id='s1'").fetchone()[0]
        )
        # The redundant embedded history is gone; the backfilled aggregates
        # and exact totals are present.
        assert "trades" not in value and "fills" not in value
        assert "accumulator" in value
        assert value["accumulator"]["trades_opened"] == closed_count + open_count
        assert value["accumulator"]["trades_completed"] == closed_count
        assert value["closed_trades_total"] == closed_count
        assert value["fills_total"] == len(fills)

    # Bounded hydration: the working set is capped while the ledger stays whole.
    state = repository.load_session("s1")
    assert state is not None
    assert len(state.fills) == repository.RECENT_FILLS_LIMIT
    assert len([trade for trade in state.trades if trade.status == "closed"]) \
        == repository.RECENT_CLOSED_TRADES_LIMIT
    assert len([trade for trade in state.trades if trade.status == "open"]) == open_count

    # Accumulator-based statistics equal the full-history reference computed
    # over every ledger row (the bounded working set alone cannot reproduce
    # them, so a missing accumulator cannot accidentally pass).
    reference = ReplayState(
        id="s1", symbol="EURUSD",
        start=datetime.fromisoformat("2026-01-01T00:00:00+00:00"),
        end=datetime.fromisoformat("2026-02-01T00:00:00+00:00"),
        profile="utc_aligned",
    )
    reference.trades = repository.all_trades("s1")
    with sqlite3.connect(live) as db:
        rows = db.execute(
            "SELECT fill_json FROM fills WHERE session_id='s1' ORDER BY rowid"
        ).fetchall()
    reference.fills = [repository._parse_fill(json.loads(item[0])) for item in rows]
    assert calculate_stats(state) == calculate_stats_from_history(reference)


def test_v7_database_gains_fill_anchor_index(db_path):
    """A v7 database is upgraded to v8: the anchor_time column is backfilled
    from each row's effective chart anchor (the source candle's open for
    modern rows, the recorded timestamp for legacy rows) and the
    (session_id, trade_id, anchor_time) index is created and used.
    Re-running the migration is a byte-for-byte no-op."""
    with sqlite3.connect(db_path) as db:
        migrate(db)
        modern = {
            "id": "f1", "session_id": "s1", "trade_id": "t1",
            "timestamp": "2026-01-02T17:01:00+00:00",
            "source_candle_time": "2026-01-02T17:00:00+00:00",
            "price": 1.1, "quantity": 1.0,
        }
        legacy = {
            "id": "f2", "session_id": "s1", "trade_id": "t1",
            "timestamp": "2026-01-02T17:02:00+00:00",
            "price": 1.1, "quantity": 1.0,
        }
        db.execute(
            "INSERT INTO fills(id,session_id,trade_id,fill_json,created_at) VALUES(?,?,?,?,?)",
            ("f1", "s1", "t1", json.dumps(modern), "2026-01-02T00:00:00+00:00"))
        db.execute(
            "INSERT INTO fills(id,session_id,trade_id,fill_json,created_at) VALUES(?,?,?,?,?)",
            ("f2", "s1", "t1", json.dumps(legacy), "2026-01-02T00:00:00+00:00"))
        # Roll back to a v7 shape: no anchor column, no v8 index.
        db.execute("DROP INDEX ix_fills_session_trade_anchor")
        db.execute("ALTER TABLE fills DROP COLUMN anchor_time")
        _set_version(db, 7)

    with sqlite3.connect(db_path) as db:
        migrate(db)
        assert read_schema_version(db) == CURRENT_SCHEMA_VERSION
        assert "anchor_time" in _columns(db, "fills")
        assert "ix_fills_session_trade_anchor" in _indexes(db)
        anchors = dict(db.execute("SELECT id, anchor_time FROM fills").fetchall())
        assert anchors == {
            "f1": "2026-01-02T17:00:00+00:00",  # the source candle's open
            "f2": "2026-01-02T17:02:00+00:00",  # the legacy row's timestamp
        }
        # The windowed fill read is served by the new index, not a scan.
        plan = db.execute(
            "EXPLAIN QUERY PLAN SELECT fill_json FROM fills "
            "WHERE session_id='s1' AND trade_id='t1' "
            "AND anchor_time >= ? AND anchor_time <= ?",
            ("2026-01-02T17:00:00+00:00", "2026-01-02T17:02:00+00:00"),
        ).fetchone()
        assert "ix_fills_session_trade_anchor" in plan[-1]
        # Idempotent: a second run leaves the backfill byte-for-byte intact.
        migrate(db)
        assert dict(db.execute("SELECT id, anchor_time FROM fills").fetchall()) == anchors
