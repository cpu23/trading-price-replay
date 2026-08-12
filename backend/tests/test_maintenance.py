import os
import sqlite3
from pathlib import Path

import pytest

from app import config, repository
from app.maintenance import (
    CURRENT_SCHEMA_VERSION,
    MaintenanceError,
    backup_database,
    main,
    restore_database,
)
from app.migrations import read_schema_version

_SYMBOL_COLUMNS = (
    "symbol, asset_class, pnl_currency, price_precision, contract_multiplier, "
    "default_profile, first_timestamp, last_timestamp, data_version"
)


@pytest.fixture()
def db_env(tmp_path, monkeypatch):
    """Point the app at an isolated database and initialize it."""
    live = tmp_path / "sessions" / "price_replay.sqlite3"
    monkeypatch.setattr(config, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(config, "RAW_ROOT", tmp_path / "raw")
    monkeypatch.setattr(config, "OHLCV_ROOT", tmp_path / "ohlcv")
    monkeypatch.setattr(config, "DB_PATH", live)
    monkeypatch.setattr(repository, "DB_PATH", live)
    repository.initialize()
    return live


def _insert_symbol(db_path: Path, symbol: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            f"INSERT INTO symbols({_SYMBOL_COLUMNS}) "
            "VALUES (?, 'forex', 'USD', 5, 1.0, 'utc_aligned', "
            "'2026-01-02T00:00:00Z', '2026-01-02T23:59:00Z', NULL)",
            (symbol,),
        )
        conn.commit()
    finally:
        conn.close()


def _copy_db(source: Path, destination: Path) -> None:
    """Copy a database file with the online backup API (folds WAL frames)."""
    src = sqlite3.connect(source)
    try:
        dst = sqlite3.connect(destination)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _symbols(db_path: Path) -> list[str]:
    conn = sqlite3.connect(db_path)
    try:
        return [row[0] for row in conn.execute("SELECT symbol FROM symbols ORDER BY symbol")]
    finally:
        conn.close()


def test_backup_includes_committed_wal_content(db_env, tmp_path):
    # Commit a row through an open WAL connection and keep it open: the commit
    # lives in the -wal sidecar, uncheckpointed, at backup time.
    writer = sqlite3.connect(db_env)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute(
            f"INSERT INTO symbols({_SYMBOL_COLUMNS}) "
            "VALUES ('EURUSD', 'forex', 'USD', 5, 1.0, 'utc_aligned', "
            "'2026-01-02T00:00:00Z', '2026-01-02T23:59:00Z', NULL)"
        )
        writer.commit()
        assert Path(f"{db_env}-wal").is_file()

        destination = tmp_path / "wal_backup.sqlite3"
        assert backup_database(destination) == destination

        # The online backup captured the committed-but-uncheckpointed row.
        with sqlite3.connect(destination) as check:
            assert check.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            row = check.execute("SELECT symbol FROM symbols WHERE symbol='EURUSD'").fetchone()
            assert row is not None and row[0] == "EURUSD"
    finally:
        writer.close()


def test_backup_refuses_to_overwrite_existing_destination(db_env, tmp_path):
    destination = tmp_path / "existing.sqlite3"
    destination.write_bytes(b"precious bytes")
    with pytest.raises(FileExistsError):
        backup_database(destination)
    assert destination.read_bytes() == b"precious bytes"  # untouched on refusal

    assert backup_database(destination, overwrite=True) == destination
    with sqlite3.connect(destination) as check:
        assert check.execute("PRAGMA quick_check").fetchone()[0] == "ok"


def test_backup_refuses_to_replace_live_database(db_env):
    before = db_env.read_bytes()
    with pytest.raises(MaintenanceError, match="must differ"):
        backup_database(db_env, overwrite=True)
    assert db_env.read_bytes() == before


def test_restore_rejects_corrupt_source_and_preserves_live(db_env, tmp_path):
    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(b"definitely not a sqlite database")
    before = db_env.read_bytes()

    with pytest.raises(MaintenanceError, match="not a readable SQLite database"):
        restore_database(corrupt)

    assert db_env.read_bytes() == before  # live data byte-identical
    assert not list(db_env.parent.glob(".*restore-tmp*"))  # no temp litter


def test_restore_rejects_newer_schema_source(db_env, tmp_path):
    newer = tmp_path / "newer.sqlite3"
    _copy_db(db_env, newer)
    conn = sqlite3.connect(newer)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('schema_version', ?)",
            (str(CURRENT_SCHEMA_VERSION + 1),),
        )
        conn.commit()
    finally:
        conn.close()
    before = db_env.read_bytes()

    with pytest.raises(MaintenanceError, match="newer"):
        restore_database(newer)

    assert db_env.read_bytes() == before  # rejected before anything touched live
    assert not list(db_env.parent.glob(".*restore-tmp*"))


def test_restore_upgrades_older_unversioned_backup(db_env, tmp_path):
    # A legacy backup: full current tables plus data, but no version metadata.
    legacy = tmp_path / "legacy.sqlite3"
    _copy_db(db_env, legacy)
    conn = sqlite3.connect(legacy)
    try:
        conn.execute(
            f"INSERT INTO symbols({_SYMBOL_COLUMNS}) "
            "VALUES ('EURUSD', 'forex', 'USD', 5, 1.0, 'utc_aligned', "
            "'2026-01-02T00:00:00Z', '2026-01-02T23:59:00Z', NULL)"
        )
        conn.execute("DROP TABLE schema_meta")
        conn.commit()
    finally:
        conn.close()

    safety = restore_database(legacy)

    assert safety is not None and safety.is_file()
    repository.initialize()  # restored DB must be initializable and loadable
    with repository.connect() as db:
        assert read_schema_version(db) == CURRENT_SCHEMA_VERSION  # upgraded in place
        row = db.execute("SELECT symbol FROM symbols WHERE symbol='EURUSD'").fetchone()
        assert row is not None  # data survived the upgrade


def test_restore_installs_backup_and_creates_validated_safety_backup(db_env, tmp_path):
    _insert_symbol(db_env, "EURUSD")
    source = tmp_path / "snapshot.sqlite3"
    backup_database(source)
    _insert_symbol(db_env, "JPYUSD")  # live moves on after the snapshot

    safety = restore_database(source)

    assert safety is not None and safety.is_file()
    assert safety.parent == db_env.parent  # automatic sibling of the live DB
    assert safety.name.startswith(f"{db_env.name}.safety-")
    assert _symbols(db_env) == ["EURUSD"]  # snapshot content restored
    with sqlite3.connect(safety) as check:
        assert check.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert [row[0] for row in check.execute("SELECT symbol FROM symbols ORDER BY symbol")] == [
            "EURUSD", "JPYUSD",
        ]  # safety backup holds the pre-restore live state
    repository.initialize()  # restored DB passes app initialization
    assert repository.load_session("nope") is None  # and normal load operations


def test_restore_honors_explicit_safety_backup_and_refuses_overwrite(db_env, tmp_path):
    _insert_symbol(db_env, "EURUSD")
    source = tmp_path / "snapshot.sqlite3"
    backup_database(source)
    custom = tmp_path / "custom-safety.sqlite3"

    assert restore_database(source, pre_restore_backup=custom) == custom
    assert custom.is_file()

    before = db_env.read_bytes()
    with pytest.raises(FileExistsError):
        restore_database(source, pre_restore_backup=custom)
    assert db_env.read_bytes() == before  # failed safety backup leaves live intact
    assert not list(db_env.parent.glob(".*restore-tmp*"))


def test_restore_rejects_invalid_version_metadata_and_preserves_live(db_env, tmp_path):
    damaged = tmp_path / "damaged.sqlite3"
    _copy_db(db_env, damaged)
    conn = sqlite3.connect(damaged)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('schema_version', 'not-a-number')"
        )
        conn.commit()
    finally:
        conn.close()
    before = db_env.read_bytes()

    with pytest.raises(MaintenanceError, match="invalid schema version metadata"):
        restore_database(damaged)

    assert db_env.read_bytes() == before  # rejected before anything touched live
    assert not list(db_env.parent.glob(".*restore-tmp*"))


def test_restore_rejects_unrelated_sqlite_source_and_preserves_live(db_env, tmp_path):
    unrelated = tmp_path / "unrelated.sqlite3"
    conn = sqlite3.connect(unrelated)
    try:
        conn.execute("CREATE TABLE customers(id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
        conn.execute("INSERT INTO customers(name) VALUES ('Acme')")
        conn.commit()
    finally:
        conn.close()
    before = db_env.read_bytes()

    with pytest.raises(MaintenanceError, match="not a price replay database"):
        restore_database(unrelated)

    assert db_env.read_bytes() == before  # rejected before anything touched live
    assert not list(db_env.parent.glob(".*restore-tmp*"))


def test_restore_source_never_opens_read_write(db_env, tmp_path, monkeypatch):
    """Restore sources are only ever opened via the mode=ro URI; the read-write
    fallback is reserved for the known live database."""
    _insert_symbol(db_env, "EURUSD")
    source = tmp_path / "snapshot.sqlite3"
    backup_database(source)

    calls: list[tuple[str, bool]] = []
    real_connect = sqlite3.connect

    def recording_connect(database, *args, **kwargs):
        calls.append((database, kwargs.get("uri", False)))
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", recording_connect)
    restore_database(source)

    # The user's backup file was opened read-only (URI form), never by Path
    # (the plain read-write form).
    ro_uri_calls = [db for db, uri in calls if uri and "mode=ro" in str(db)]
    assert any(source.name in str(db) for db in ro_uri_calls)
    assert not any(db is source for db, _ in calls)


def test_restore_install_failure_preserves_wal_backed_live(db_env, tmp_path, monkeypatch):
    """If the final install rename fails, a committed-but-uncheckpointed row in
    the live database's WAL must survive (checkpointed into the main file before
    the sidecars were removed), and the safety backup must already exist."""
    writer = sqlite3.connect(db_env)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute(
            f"INSERT INTO symbols({_SYMBOL_COLUMNS}) "
            "VALUES ('EURUSD', 'forex', 'USD', 5, 1.0, 'utc_aligned', "
            "'2026-01-02T00:00:00Z', '2026-01-02T23:59:00Z', NULL)"
        )
        writer.commit()
        assert Path(f"{db_env}-wal").is_file()  # frames are WAL-only at this point
        source = tmp_path / "snapshot.sqlite3"
        backup_database(source)

        real_replace = os.replace

        def failing_install_replace(src, dst, *args, **kwargs):
            # Only the final install rename (onto the live DB) fails; the
            # safety backup's own rename must still succeed.
            if Path(dst) == db_env:
                raise OSError("injected install failure")
            return real_replace(src, dst, *args, **kwargs)

        monkeypatch.setattr("app.maintenance.os.replace", failing_install_replace)
        with pytest.raises(OSError, match="injected"):
            restore_database(source)
    finally:
        writer.close()

    # The committed row is still readable from the live database: the checkpoint
    # folded it into the main file before the sidecars were removed.
    with sqlite3.connect(db_env) as check:
        assert check.execute("SELECT symbol FROM symbols WHERE symbol='EURUSD'").fetchone()
    # And the validated safety backup of the pre-restore live state exists.
    safety = max(db_env.parent.glob(f"{db_env.name}.safety-*"))
    with sqlite3.connect(safety) as check:
        assert check.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert check.execute("SELECT symbol FROM symbols WHERE symbol='EURUSD'").fetchone()


def test_restore_aborts_when_live_database_is_busy(db_env, tmp_path):
    """A checkpoint that cannot complete (another connection is reading the live
    database) aborts the restore before any sidecar is deleted or any file is
    replaced; the live database stays logically complete."""
    _insert_symbol(db_env, "EURUSD")
    source = tmp_path / "snapshot.sqlite3"
    backup_database(source)

    blocker = sqlite3.connect(db_env)
    try:
        blocker.execute("BEGIN")
        blocker.execute("SELECT count(*) FROM symbols").fetchone()  # hold a read mark
        sidecars_before = {p.name for p in db_env.parent.iterdir() if p.name != db_env.name}
        with pytest.raises(MaintenanceError, match="busy"):
            restore_database(source)
        sidecars_after = {p.name for p in db_env.parent.iterdir() if p.name != db_env.name}
        assert sidecars_before <= sidecars_after  # nothing deleted
        assert not list(db_env.parent.glob(".*restore-tmp*"))  # no install happened
        # The live database is still valid and holds the committed row.
        with repository.connect() as db:
            assert db.execute("SELECT symbol FROM symbols WHERE symbol='EURUSD'").fetchone()
    finally:
        blocker.close()


def test_cli_backup_succeeds_and_restore_failure_exits_nonzero(db_env, tmp_path):
    destination = tmp_path / "cli.sqlite3"
    assert main(["backup", str(destination)]) == 0
    assert destination.is_file()

    safety = tmp_path / "cli-safety.sqlite3"
    assert main(["restore", str(destination), "--safety-backup", str(safety)]) == 0
    assert safety.is_file()
    assert _symbols(db_env) == []

    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(b"garbage")
    assert main(["restore", str(corrupt)]) == 1
