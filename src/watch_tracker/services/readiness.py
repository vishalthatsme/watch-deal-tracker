from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from watch_tracker.config import Settings


def _secret_digest(value: str | None) -> str | None:
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _file_state(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        stat = path.stat()
    except FileNotFoundError:
        return {"path": str(path), "exists": False}
    return {
        "path": str(path),
        "exists": True,
        "size": stat.st_size,
        "modified_ns": stat.st_mtime_ns,
    }


def source_readiness_fingerprint(settings: Settings) -> str:
    """Hash non-content readiness state so a same-day permission change retries."""
    reddit = settings.sources.reddit
    chrono24 = settings.sources.chrono24
    payload = {
        "version": 1,
        "config": _file_state(settings.config_path),
        "secrets_file": _file_state(settings.config_path.parent / "secrets.env"),
        "reddit": {
            "enabled": reddit.enabled,
            "community": reddit.community,
            "access_approved": reddit.access_approved,
            "deletion_contract_verified": getattr(reddit, "deletion_contract_verified", False),
            "client_id_digest": _secret_digest(reddit.client_id),
            "client_secret_digest": _secret_digest(reddit.client_secret),
            "username_digest": _secret_digest(reddit.username),
        },
        "chrono24": {
            "enabled": chrono24.enabled,
            "access_authorized": chrono24.access_authorized,
            "authorized_feed": _file_state(chrono24.authorized_feed_path),
        },
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
