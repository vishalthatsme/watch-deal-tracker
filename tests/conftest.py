from __future__ import annotations

from pathlib import Path

import pytest

from watch_tracker.config import Settings, load_settings


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    root = Path(__file__).resolve().parents[1]
    configured = load_settings(root / "config/default.yaml")
    configured.paths.database = tmp_path / "data/database/watch_market.sqlite"
    configured.paths.exports = tmp_path / "data/exports"
    configured.paths.backups = tmp_path / "data/backups"
    configured.paths.evidence = tmp_path / "data/evidence"
    configured.paths.logs = tmp_path / "logs"
    configured.paths.lock = tmp_path / "data/database/watch_tracker.lock"
    configured.ensure_directories()
    return configured
