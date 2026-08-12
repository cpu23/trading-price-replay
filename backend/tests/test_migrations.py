"""Schema migration contracts: fresh installs, legacy baselining, and durability.

Every test builds its own database by hand (never through migrate) so the
upgrade path is exercised from the exact legacy shape being claimed, then
asserts observable state: recorded version, tables/columns/indexes, and that
every pre-existing row survived.
"""

import sqlite3

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
        db.execute(
            "INSERT INTO replay_sessions(id,state_json,updated_at"
            + (",revision" if revision is not None else "")
            + ") VALUES(?,?,?"
            + (",?" if revision is not None else "")
            + ")",
            ("s1", '{"symbol": "EURUSD"}', "2026-01-02T00:00:00+00:00")
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
        db.execute("INSERT INTO trades VALUES(?,?,?,?)", ("t1", "s1", '{"id": "t1"}', "2026-01-02T00:00:00+00:00"))
        db.execute("INSERT INTO fills VALUES(?,?,?,?,?)", ("f1", "s1", "t1", '{"id": "f1"}', "2026-01-02T00:00:00+00:00"))
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
            ("trades", 1), ("fills", 1), ("replay_events", 1),
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
            ("s1", '{"symbol": "EURUSD"}', "2026-01-02T00:00:00+00:00"),
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
        assert db.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1


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
