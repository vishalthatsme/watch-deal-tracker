from __future__ import annotations

import sqlite3
from pathlib import Path

from watch_tracker.database.backup import (
    create_backup,
    integrity_check,
    purge_backup_artifacts,
)
from watch_tracker.services.exports import _text, purge_export_artifacts


def _create_wal_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE fixture (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO fixture (value) VALUES ('retained')")
        connection.commit()
    finally:
        connection.close()


def _sidecars(path: Path) -> tuple[Path, Path, Path]:
    return Path(f"{path}-wal"), Path(f"{path}-shm"), Path(f"{path}-journal")


def test_integrity_check_is_read_only_and_does_not_create_sidecars(tmp_path: Path) -> None:
    database = tmp_path / "database with spaces.sqlite"
    _create_wal_database(database)
    for sidecar in _sidecars(database):
        sidecar.unlink(missing_ok=True)

    assert integrity_check(database) == (True, "ok")
    assert all(not sidecar.exists() for sidecar in _sidecars(database))


def test_create_backup_leaves_no_temporary_or_sqlite_sidecars(tmp_path: Path) -> None:
    database = tmp_path / "data" / "watch_market.sqlite"
    backup_directory = tmp_path / "backups"
    _create_wal_database(database)
    backup_directory.mkdir()
    for suffix in ("", "-wal", "-shm", "-journal"):
        (backup_directory / f".watch-market-backup-orphan.sqlite.tmp{suffix}").write_text(
            "stale temporary data",
            encoding="utf-8",
        )

    backup = create_backup(database, backup_directory, retain=2)

    assert integrity_check(backup) == (True, "ok")
    assert list(backup_directory.glob(".watch-market-backup-*")) == []
    assert list(backup_directory.glob("*-wal")) == []
    assert list(backup_directory.glob("*-shm")) == []
    assert list(backup_directory.glob("*-journal")) == []


def test_purge_backup_artifacts_removes_only_managed_direct_children(tmp_path: Path) -> None:
    backup_directory = tmp_path / "backups"
    backup_directory.mkdir()
    managed_names = {
        "watch_market_daily_20260725T010203Z.sqlite",
        "watch_market_daily_20260725T010203Z.sqlite-wal",
        "watch_market_daily_20260725T010203Z.sqlite-shm",
        "watch_market_daily_20260725T010203Z.sqlite-journal",
        ".watch-market-backup-deadbeef.sqlite.tmp",
        ".watch-market-backup-deadbeef.sqlite.tmp-wal",
        ".watch-market-backup-deadbeef.sqlite.tmp-journal",
    }
    for name in managed_names:
        (backup_directory / name).write_text("sensitive fixture", encoding="utf-8")
    unrelated = backup_directory / "keep.sqlite"
    unrelated.write_text("keep", encoding="utf-8")
    managed_directory = backup_directory / "watch_market_daily_20260725T020304Z.sqlite"
    managed_directory.mkdir()

    removed = purge_backup_artifacts(backup_directory)

    assert {path.name for path in removed} == managed_names
    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert managed_directory.is_dir()


def test_purge_export_artifacts_removes_only_managed_direct_children(tmp_path: Path) -> None:
    export_directory = tmp_path / "exports"
    export_directory.mkdir()
    managed_names = {
        "watch_listings_latest.csv",
        "watch_active_deals.csv",
        "watch_sales_history.csv",
        "watch_listings_2026-07-25.csv",
        "watch_deal_report_2026-07-25.md",
        ".watch_listings_latest.csv.deadbeef.tmp",
        ".watch_deal_report_2026-07-25.md.deadbeef.tmp",
    }
    for name in managed_names:
        (export_directory / name).write_text("sensitive fixture", encoding="utf-8")
    unrelated = export_directory / "watch_notes.csv"
    unrelated.write_text("keep", encoding="utf-8")
    managed_directory = export_directory / "watch_listings_2026-07-26.csv"
    managed_directory.mkdir()

    removed = purge_export_artifacts(export_directory)

    assert {path.name for path in removed} == managed_names
    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert managed_directory.is_dir()


def test_artifact_purges_are_idempotent_for_missing_directories(tmp_path: Path) -> None:
    assert purge_backup_artifacts(tmp_path / "missing-backups") == []
    assert purge_export_artifacts(tmp_path / "missing-exports") == []


def test_export_text_neutralizes_spreadsheet_formula_prefixes() -> None:
    assert _text('=HYPERLINK("https://malicious.invalid")').startswith("'=")
    assert _text("@SUM(1,2)").startswith("'@")
    assert _text("ordinary title") == "ordinary title"
