from __future__ import annotations

from pathlib import Path

from sqlalchemy import text

from watch_tracker.database.secure_erasure import secure_erase_database
from watch_tracker.database.session import create_sqlite_engine


def _managed_database_bytes(database_path: Path) -> bytes:
    content = bytearray()
    for suffix in ("", "-journal", "-wal", "-shm"):
        artifact = Path(f"{database_path}{suffix}")
        if artifact.exists():
            content.extend(artifact.read_bytes())
    return bytes(content)


def test_engine_enables_secure_delete_and_hides_parameters(tmp_path: Path) -> None:
    database = tmp_path / "database" / "secure.sqlite"
    engine = create_sqlite_engine(database)
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA secure_delete").scalar_one() == 1
    assert engine.hide_parameters is True
    assert database.stat().st_mode & 0o777 == 0o600


def test_secure_erase_removes_deleted_sentinel_from_database_and_sidecars(
    tmp_path: Path,
) -> None:
    database = tmp_path / "database" / "secure.sqlite"
    engine = create_sqlite_engine(database)
    sentinel = "ERASURE-SENTINEL-9f2014f7-" * 256
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA secure_delete=OFF")
        connection.execute(text("CREATE TABLE secret (value TEXT NOT NULL)"))
        connection.execute(
            text("INSERT INTO secret (value) VALUES (:value)"),
            {"value": sentinel},
        )
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA secure_delete=OFF")
        connection.execute(text("DELETE FROM secret"))
    engine.dispose()

    secure_erase_database(database)

    assert sentinel.encode() not in _managed_database_bytes(database)
    assert not Path(f"{database}-journal").exists()
    assert not Path(f"{database}-wal").exists()
    assert not Path(f"{database}-shm").exists()
