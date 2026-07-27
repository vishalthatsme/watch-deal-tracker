from __future__ import annotations

import os
import re
import sqlite3
import tempfile
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

_BACKUP_ARTIFACT_PATTERN = re.compile(
    r"(?:watch_market_[A-Za-z0-9_-]+_\d{8}T\d{6}Z\.sqlite"
    r"|\.watch-market-backup-[A-Za-z0-9_-]+\.sqlite\.tmp)"
    r"(?:-(?:wal|shm|journal))?"
)
_TEMPORARY_BACKUP_PATTERN = re.compile(
    r"\.watch-market-backup-[A-Za-z0-9_-]+\.sqlite\.tmp(?:-(?:wal|shm|journal))?"
)


def _sidecar_paths(database_path: Path) -> tuple[Path, Path, Path]:
    return (
        Path(f"{database_path}-wal"),
        Path(f"{database_path}-shm"),
        Path(f"{database_path}-journal"),
    )


def _remove_sidecars(database_path: Path) -> None:
    for sidecar in _sidecar_paths(database_path):
        sidecar.unlink(missing_ok=True)


def _remove_orphan_temporary_artifacts(backup_directory: Path) -> None:
    for artifact in backup_directory.iterdir():
        if _TEMPORARY_BACKUP_PATTERN.fullmatch(artifact.name) and (
            artifact.is_file() or artifact.is_symlink()
        ):
            artifact.unlink()


def purge_backup_artifacts(backup_directory: Path) -> list[Path]:
    """Remove only Watch Tracker backup artifacts directly inside a backup directory."""
    if not backup_directory.exists():
        return []
    if not backup_directory.is_dir():
        raise NotADirectoryError(backup_directory)

    removed: list[Path] = []
    for artifact in sorted(backup_directory.iterdir()):
        if not _BACKUP_ARTIFACT_PATTERN.fullmatch(artifact.name):
            continue
        if not (artifact.is_file() or artifact.is_symlink()):
            continue
        artifact.unlink()
        removed.append(artifact)
    return removed


def integrity_check(database_path: Path) -> tuple[bool, str]:
    if not database_path.exists():
        return False, f"Database does not exist: {database_path}"
    database_uri = database_path.resolve().as_uri()
    connection = sqlite3.connect(f"{database_uri}?mode=ro&immutable=1", uri=True)
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()
        message = str(result[0]) if result else "No integrity-check result"
        return message.casefold() == "ok", message
    finally:
        connection.close()


def create_backup(
    database_path: Path,
    backup_directory: Path,
    retain: int = 30,
    label: str = "daily",
) -> Path:
    if not database_path.exists():
        raise FileNotFoundError(f"Cannot back up missing database: {database_path}")
    if retain < 1:
        raise ValueError("Backup retention must be at least one")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", label):
        raise ValueError("Backup label may contain only letters, numbers, underscores, and dashes")
    backup_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    _remove_orphan_temporary_artifacts(backup_directory)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = backup_directory / f"watch_market_{label}_{timestamp}.sqlite"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".watch-market-backup-", suffix=".sqlite.tmp", dir=backup_directory
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        source_connection = sqlite3.connect(database_path)
        backup_connection = sqlite3.connect(temporary)
        try:
            source_connection.backup(backup_connection)
            backup_connection.commit()
        finally:
            backup_connection.close()
            source_connection.close()
        healthy, message = integrity_check(temporary)
        if not healthy:
            raise RuntimeError(f"Backup integrity check failed: {message}")
        _remove_sidecars(temporary)
        os.chmod(temporary, 0o600)
        _remove_sidecars(destination)
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        _remove_sidecars(temporary)

    backups = sorted(
        backup_directory.glob(f"watch_market_{label}_*.sqlite"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for expired in backups[retain:]:
        expired.unlink()
        _remove_sidecars(expired)
    with suppress(FileNotFoundError):
        _remove_sidecars(destination)
        os.chmod(destination, 0o600)
    return destination
