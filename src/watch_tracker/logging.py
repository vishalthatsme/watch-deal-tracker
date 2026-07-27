from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any


class JsonFormatter(logging.Formatter):
    _standard = {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(
            {
                key: value
                for key, value in record.__dict__.items()
                if key not in self._standard and not key.startswith("_")
            }
        )
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class PrivateTimedRotatingFileHandler(TimedRotatingFileHandler):
    """Keep both the initial and post-rollover log files owner-readable only."""

    def _open(self):  # type: ignore[no-untyped-def]
        stream = super()._open()
        os.chmod(self.baseFilename, 0o600)
        return stream


def configure_logging(
    log_directory: Path,
    level: str = "INFO",
    retention_days: int = 30,
) -> Path:
    log_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(log_directory, 0o700)
    log_path = log_directory / "watch_tracker.jsonl"
    formatter = JsonFormatter()

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level.upper())

    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    root.addHandler(stream)

    file_handler = PrivateTimedRotatingFileHandler(
        log_path,
        when="midnight",
        backupCount=retention_days,
        encoding="utf-8",
        utc=True,
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)
    return log_path
