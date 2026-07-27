from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from watch_tracker.config import ApplicationSettings, RedditSettings, load_settings


def test_application_schedule_is_fixed_to_midnight() -> None:
    with pytest.raises(ValidationError, match="fixed to daily midnight"):
        ApplicationSettings(
            timezone="America/Los_Angeles",
            schedule_hour=1,
            schedule_minute=0,
            user_agent="test",
        )


def test_application_timezone_must_be_an_iana_zone() -> None:
    with pytest.raises(ValidationError, match="Unknown IANA timezone"):
        ApplicationSettings(
            timezone="Definitely/Not_A_Zone",
            schedule_hour=0,
            schedule_minute=0,
            user_agent="test",
        )


def test_source_enabled_flags_can_be_set_in_secure_environment(monkeypatch) -> None:
    root = Path(__file__).resolve().parents[2]
    monkeypatch.setenv("WATCH_TRACKER_REDDIT_ENABLED", "false")
    monkeypatch.setenv("WATCH_TRACKER_REDDIT_DELETION_CONTRACT_VERIFIED", "true")
    monkeypatch.setenv("WATCH_TRACKER_CHRONO24_ENABLED", "true")

    configured = load_settings(root / "config/default.yaml")

    assert configured.sources.reddit.enabled is False
    assert configured.sources.reddit.deletion_contract_verified is True
    assert configured.sources.chrono24.enabled is True


def test_reddit_request_budget_defaults_to_100_and_must_be_positive() -> None:
    assert RedditSettings().max_requests_per_run == 100

    with pytest.raises(ValidationError):
        RedditSettings(max_requests_per_run=0)
