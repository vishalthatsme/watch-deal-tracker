from __future__ import annotations

import os
import sqlite3
from pathlib import Path


def _checkpoint_truncate(connection: sqlite3.Connection) -> None:
    result = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    if result is None:
        raise RuntimeError("SQLite did not return a WAL checkpoint result")
    busy = int(result[0])
    if busy:
        raise RuntimeError(
            "SQLite WAL checkpoint is busy; close all database readers before secure erasure"
        )


def secure_erase_database(database_path: Path) -> None:
    """Physically remove deleted SQLite content after callers dispose all engines.

    The caller must hold the application lock and close every ORM session/engine
    first. A truncating checkpoint is required before and after ``VACUUM`` so
    deleted content cannot remain in the WAL. Managed sidecars are removed only
    after the database connection closes successfully.
    """

    if not database_path.exists() or not database_path.is_file():
        raise FileNotFoundError(f"Cannot securely erase missing database: {database_path}")

    database_uri = database_path.resolve().as_uri()
    connection = sqlite3.connect(
        f"{database_uri}?mode=rw",
        uri=True,
        timeout=30,
        isolation_level=None,
    )
    try:
        connection.execute("PRAGMA busy_timeout=30000")
        secure_delete = connection.execute("PRAGMA secure_delete=ON").fetchone()
        if secure_delete is None or int(secure_delete[0]) != 1:
            raise RuntimeError("SQLite secure_delete could not be enabled")
        _checkpoint_truncate(connection)
        connection.execute("VACUUM")
        _checkpoint_truncate(connection)
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or str(integrity[0]).casefold() != "ok":
            message = str(integrity[0]) if integrity else "no result"
            raise RuntimeError(f"Post-erasure SQLite integrity check failed: {message}")
    finally:
        connection.close()

    for suffix in ("-journal", "-wal", "-shm"):
        Path(f"{database_path}{suffix}").unlink(missing_ok=True)
    os.chmod(database_path, 0o600)
