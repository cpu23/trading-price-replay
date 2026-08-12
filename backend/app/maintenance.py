"""Online backup and validated restore of the price replay SQLite database.

The live database lives at ``config.DB_PATH`` and is resolved from ``app.config``
at call time, so ``PRICE_REPLAY_DATA_ROOT`` overrides and test monkeypatching of
``config.DB_PATH`` take effect. The database runs in WAL mode, so both operations
work on committed-but-uncheckpointed content via the SQLite online backup API
(``sqlite3.Connection.backup``).

Public API
----------
``backup_database(destination, overwrite=False) -> Path``
    Copy the live database to ``destination``. The copy includes every committed
    transaction (WAL frames included) and is validated with ``PRAGMA quick_check``
    and a schema-readability probe before it becomes visible. An existing
    ``destination`` is refused unless ``overwrite=True``. The file is written to
    a temporary sibling and atomically renamed, so a failed backup never leaves a
    partial destination. Returns the resolved destination path.

    ``restore_database(source, pre_restore_backup=None) -> Path | None``
    Replace the live database with ``source``. The source is validated read-only
    and is never opened read-write; corrupt, newer-schema, and unrelated SQLite
    inputs are rejected before anything touches the live file. A temporary
    candidate next to the live database is then built from the source, validated,
    upgraded through ``app.migrations.migrate`` when the source is older or
    unversioned, and validated again. A validated safety backup of the live
    database is always created before the candidate is atomically installed (at
    ``pre_restore_backup`` when given, else an automatic timestamped sibling of
    the live database). The stopped live database is then WAL-checkpointed and
    its sidecars removed before the atomic rename, so a failed install can never
    cost committed data. On any failure the live database's committed data is
    fully preserved: validation failures leave it byte-for-byte untouched, and a
    checkpoint abort (busy or unreadable database) happens before anything is
    deleted or replaced. Returns the safety backup path, or ``None`` when no live
    database existed (a fresh install has nothing to protect).

CLI
---
``python -m app.maintenance backup DEST [--overwrite]``
``python -m app.maintenance restore SOURCE [--safety-backup DEST]``

The application MUST be stopped before restoring; the restore removes WAL/SHM
sidecars and atomically replaces the main database file while the app holds no
connections.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from . import config
from .migrations import (
    CURRENT_SCHEMA_VERSION,
    SchemaVersionError,
    migrate,
    read_schema_version,
)


class MaintenanceError(Exception):
    """Raised when a backup or restore cannot be validated or completed safely.

    A raised error guarantees that no unvalidated candidate was installed and
    that the live database's committed contents remain intact. Restore may
    checkpoint committed WAL frames before a final install attempt, but it
    deletes or replaces no live file until that checkpoint succeeds.
    """


def backup_database(destination, overwrite: bool = False) -> Path:
    """Create a validated online backup of the live database at ``destination``.

    Raises ``FileNotFoundError`` when no live database exists, ``FileExistsError``
    when ``destination`` exists and ``overwrite`` is false, and
    ``MaintenanceError`` when the produced copy fails validation. Returns the
    resolved destination path.
    """
    destination = _coerce_path(destination)
    live = _live_db_path()
    if destination == live:
        raise MaintenanceError("backup destination must differ from the live database path")
    if not live.is_file():
        raise FileNotFoundError(f"live database not found at {live}; nothing to back up")
    if destination.exists() and not overwrite:
        raise FileExistsError(
            f"backup destination already exists: {destination} (pass overwrite=True to replace it)"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.parent / f".{destination.name}.tmp-{uuid4().hex}"
    try:
        _backup_into(live, temp)
        _validate_db(temp, label="backup")
        _fsync_dir(destination.parent)
        os.replace(temp, destination)
        _fsync_dir(destination.parent)
    except BaseException:
        _cleanup(temp)
        raise
    return destination


def restore_database(source, pre_restore_backup=None) -> Path | None:
    """Atomically replace the live database with a validated, migrated copy of ``source``.

    Raises ``FileNotFoundError`` for a missing source, ``FileExistsError`` when a
    supplied ``pre_restore_backup`` path already exists, and ``MaintenanceError``
    for corrupt, newer-schema, or unrelated-SQLite sources or any failed
    validation step. The live database's committed data is fully preserved on
    every failure path: validation failures leave it byte-for-byte untouched,
    and a checkpoint abort precedes any deletion or replacement. Returns the
    safety backup path (``None`` if no live database existed to protect).
    """
    source = _coerce_path(source)
    live = _live_db_path()
    if not source.is_file():
        raise FileNotFoundError(f"restore source not found: {source}")
    if not live.is_file():
        # Fresh install: nothing to protect, but the candidate still needs a home.
        live.parent.mkdir(parents=True, exist_ok=True)

    # Stage 1: validate the source read-only, before anything touches live data.
    _validate_db(source, label="restore source")
    _check_source_schema(source)

    # Stage 2: build, validate, and migrate a temporary candidate beside the live
    # database (same filesystem, so the final install is a true atomic rename).
    candidate = live.parent / f".{live.name}.restore-tmp-{uuid4().hex}"
    try:
        _backup_into(source, candidate, strict=True)  # folds uncheckpointed WAL frames
        _validate_db(candidate, label="restore candidate")
        _migrate_candidate(candidate)
        _validate_db(candidate, label="migrated restore candidate")
        _fsync_file(candidate)

        # Stage 3: validated safety backup of the live database.
        safety = _safety_backup(live, pre_restore_backup)

        # Stage 4: checkpoint the stopped live DB so every committed WAL frame
        # lives in its main file, then clear stale sidecars and install
        # atomically. A checkpoint failure (busy/unreadable) aborts before
        # anything is deleted; an install failure leaves the old main file
        # logically complete.
        _checkpoint_live(live)
        _remove_sidecars(live)
        os.replace(candidate, live)
        _fsync_dir(live.parent)
    finally:
        _cleanup(candidate)
    return safety


def main(argv: list[str] | None = None) -> int:
    """Run the backup/restore CLI; returns a process exit code."""
    parser = argparse.ArgumentParser(
        prog="python -m app.maintenance",
        description="Back up and restore the price replay SQLite database.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    backup_parser = subparsers.add_parser(
        "backup", help="write a validated online backup of the live database to DEST"
    )
    backup_parser.add_argument("destination", metavar="DEST", type=Path,
                               help="backup file to create (refused if it already exists)")
    backup_parser.add_argument("--overwrite", action="store_true",
                               help="replace DEST if it already exists")

    restore_parser = subparsers.add_parser(
        "restore", help="restore the live database from a validated backup SOURCE"
    )
    restore_parser.add_argument("source", metavar="SOURCE", type=Path,
                                help="backup file to restore")
    restore_parser.add_argument(
        "--safety-backup", metavar="DEST", dest="safety_backup", type=Path,
        help="write the pre-restore safety backup here (default: timestamped sibling "
             "of the live database)",
    )
    restore_parser.epilog = (
        "WARNING: the application MUST be stopped before restoring. The restore removes\n"
        "the live database's WAL/SHM sidecars and atomically replaces the main file; a\n"
        "running app holds stale connections and could corrupt or overwrite the restored\n"
        "database. Backup can run while the app is live."
    )
    args = parser.parse_args(argv)

    try:
        if args.command == "backup":
            destination = backup_database(args.destination, overwrite=args.overwrite)
            print(f"backup written: {destination}")
        else:
            safety = restore_database(args.source, pre_restore_backup=args.safety_backup)
            print("restore complete")
            if safety is not None:
                print(f"safety backup of the previous database: {safety}")
            else:
                print("no previous live database existed; nothing to back up")
        return 0
    except (MaintenanceError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _coerce_path(value) -> Path:
    return Path(value).expanduser().resolve()


def _live_db_path() -> Path:
    """Resolve the live database path from app.config at call time."""
    return _coerce_path(config.DB_PATH)


def _backup_into(source: Path, destination: Path, *, strict: bool = False) -> None:
    """Copy ``source`` to ``destination`` via the SQLite online backup API.

    The API reads committed content including WAL frames still sitting in the
    source's ``-wal`` sidecar, which a plain file copy would silently drop. The
    source is opened read-only, so a restore never opens the user's backup file
    for writing; ``strict`` additionally forbids the read-write fallback used
    only for the known live database. The target connection is created and
    closed inside this call so no handle to the new file survives.
    """
    src = dst = None
    try:
        src = _readonly_connection(source, strict=strict)
        dst = sqlite3.connect(destination)
        src.backup(dst)
    except MaintenanceError:
        raise
    except sqlite3.Error as exc:
        raise MaintenanceError(f"could not copy SQLite database {source}: {exc}") from exc
    finally:
        if dst is not None:
            dst.close()
        if src is not None:
            src.close()
    _fsync_file(destination)


def _validate_db(path: Path, *, label: str) -> None:
    """Run PRAGMA quick_check and a schema-readability probe; raise on any failure.

    Validation opens strictly read-only: a user-supplied restore source must
    never be opened read-write, and every file validated here (including our own
    temporaries) is expected to be readable without write access.
    """
    conn = None
    try:
        conn = _readonly_connection(path, strict=True)
        row = conn.execute("PRAGMA quick_check").fetchone()
        if row is None or row[0] != "ok":
            detail = row[0] if row is not None else "no result"
            raise MaintenanceError(
                f"{label} failed SQLite integrity check (PRAGMA quick_check): {detail}"
            )
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except sqlite3.DatabaseError as exc:
        raise MaintenanceError(f"{label} is not a readable SQLite database: {exc}") from exc
    finally:
        if conn is not None:
            conn.close()


def _readonly_connection(path: Path, *, strict: bool = False) -> sqlite3.Connection:
    """Open ``path`` read-only.

    With ``strict`` (restore sources and every validation), a read-only open
    failure raises an actionable ``MaintenanceError`` instead of falling back to
    a read-write connection, which could checkpoint or otherwise touch a user's
    backup file. The read-write fallback is reserved for the known live
    database, where WAL sidecar access is ordinary operation.
    """
    conn = None
    try:
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        # Force the file to be opened and parsed so garbage files and WAL
        # -shm access problems surface here instead of mid-validation.
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return conn
    except sqlite3.OperationalError as exc:
        if conn is not None:
            conn.close()
        if strict:
            raise MaintenanceError(
                f"cannot open {path} read-only ({exc}); the restore source must be "
                "readable without write access — stop every program using it (or "
                "checkpoint it) and retry"
            ) from exc
        return sqlite3.connect(path)


_PRICE_REPLAY_TABLES = frozenset({
    "symbols", "import_batches", "replay_sessions", "timeframe_profiles",
    "session_indicators", "orders", "trades", "fills", "replay_events",
})


def _check_source_schema(path: Path) -> None:
    """Reject unsupported restore sources before anything touches live data.

    A supported source either carries readable schema version metadata
    (``schema_meta``/``schema_version``) or is a recognizable legacy database
    holding the full Price Replay base-table set. A recorded version newer than
    ``CURRENT_SCHEMA_VERSION`` and present-but-invalid metadata are rejected
    outright, and so is any other valid SQLite file, so ``migrate()`` can never
    silently turn an arbitrary database into what looks like a valid backup.
    """
    conn = _readonly_connection(path, strict=True)
    try:
        try:
            version = read_schema_version(conn)
        except SchemaVersionError as exc:
            raise MaintenanceError(
                f"restore source {path} has invalid schema version metadata ({exc}); "
                "refusing to restore a damaged or foreign database"
            ) from exc
        if version is not None:
            if version > CURRENT_SCHEMA_VERSION:
                raise MaintenanceError(
                    f"restore source has schema version {version}, which is newer than the "
                    f"supported version {CURRENT_SCHEMA_VERSION}; refusing to install a "
                    "database created by a newer application"
                )
            return  # recognizable versioned Price Replay database
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()
    if not _PRICE_REPLAY_TABLES.issubset(tables):
        raise MaintenanceError(
            f"restore source {path} is a valid SQLite database but not a price replay "
            "database (no schema version metadata and no full legacy table set); "
            "refusing to restore it"
        )


def _migrate_candidate(path: Path) -> None:
    """Upgrade the candidate in place and fold any migration WAL frames away.

    The connection is closed explicitly (a ``with`` block only commits), so the
    final close checkpoints and removes the candidate's WAL sidecars before the
    file is validated and atomically installed.
    """
    conn = None
    try:
        conn = sqlite3.connect(path)
        try:
            migrate(conn)
        except SchemaVersionError as exc:
            # Defense in depth: the source was already accepted by
            # _check_source_schema, so this only fires on races or foreign
            # metadata; never let a raw migrations error escape a restore.
            raise MaintenanceError(f"restore candidate rejected by schema checks: {exc}") from exc
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except MaintenanceError:
        raise
    except sqlite3.Error as exc:
        raise MaintenanceError(f"could not migrate restore candidate {path}: {exc}") from exc
    finally:
        if conn is not None:
            conn.close()


def _safety_backup(live: Path, pre_restore_backup) -> Path | None:
    """Create a validated safety backup of the live database.

    Returns ``None`` when no live database exists. Refuses to overwrite an
    existing ``pre_restore_backup`` path.
    """
    if not live.is_file():
        return None
    destination = (
        _coerce_path(pre_restore_backup)
        if pre_restore_backup is not None
        else _auto_safety_path(live)
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(
            f"safety backup destination already exists: {destination}; choose another path"
        )
    temp = destination.parent / f".{destination.name}.tmp-{uuid4().hex}"
    try:
        _backup_into(live, temp)
        _validate_db(temp, label="safety backup")
        _fsync_dir(destination.parent)
        os.replace(temp, destination)
        _fsync_dir(destination.parent)
    except BaseException:
        _cleanup(temp)
        raise
    return destination


def _auto_safety_path(live: Path) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%f")
    return live.parent / f"{live.name}.safety-{timestamp}"


def _checkpoint_live(live: Path) -> None:
    """Fold every committed WAL frame of the live database into its main file.

    Called during restore before the sidecars are removed, so deleting the
    ``-wal``/``-shm`` files can never lose committed transactions, even if the
    final install step fails afterwards. A busy checkpoint aborts before any
    sidecar deletion or replacement. It may still fold committed frames into
    the main file, which changes its bytes but not its logical contents.
    """
    conn = None
    try:
        conn = sqlite3.connect(live, timeout=1.0)  # fail fast instead of waiting on a running app
        row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        busy = row[0] if row is not None else 1
        if busy:
            raise MaintenanceError(
                f"live database {live} is busy; close every connection (stop the "
                "application) before restoring — no files were deleted or replaced"
            )
    except MaintenanceError:
        raise
    except sqlite3.Error as exc:
        raise MaintenanceError(
            f"could not checkpoint the live database {live}: {exc}; no files were "
            "deleted or replaced"
        ) from exc
    finally:
        if conn is not None:
            conn.close()


def _remove_sidecars(live: Path) -> None:
    """Drop the old database's WAL/SHM sidecars so stale frames can never replay
    into the freshly installed file. Safe only while the app is stopped; the
    safety backup already folded every committed frame out of them."""
    for sidecar in (Path(f"{live}-wal"), Path(f"{live}-shm")):
        sidecar.unlink(missing_ok=True)


def _fsync_file(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_DIRECTORY)
    except OSError:
        return  # directory fsync unsupported on this platform; best effort
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _cleanup(path: Path) -> None:
    path.unlink(missing_ok=True)
    for sidecar in (Path(f"{path}-wal"), Path(f"{path}-shm")):
        sidecar.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
