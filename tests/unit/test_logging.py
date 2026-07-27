from __future__ import annotations

import logging
import stat

from watch_tracker.logging import configure_logging


def _mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_structured_logs_remain_private_across_rollover(tmp_path) -> None:
    log_directory = tmp_path / "logs"
    log_directory.mkdir(mode=0o755)
    root = logging.getLogger()
    previous_handlers = list(root.handlers)
    previous_level = root.level

    try:
        log_path = configure_logging(log_directory, retention_days=2)
        assert _mode(log_directory) == 0o700
        assert _mode(log_path) == 0o600

        file_handler = next(
            handler
            for handler in root.handlers
            if getattr(handler, "baseFilename", None) == str(log_path)
        )
        file_handler.doRollover()

        assert all(_mode(artifact) == 0o600 for artifact in log_directory.iterdir())
    finally:
        for handler in list(root.handlers):
            if handler not in previous_handlers:
                handler.close()
        root.handlers[:] = previous_handlers
        root.setLevel(previous_level)
