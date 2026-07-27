from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from watch_tracker.sources.base import SourceAccessError
from watch_tracker.sources.chrono24 import Chrono24AuthorizedFeedSource


def test_chrono24_source_fails_closed_without_permission(settings) -> None:
    source = Chrono24AuthorizedFeedSource(settings)
    with pytest.raises(SourceAccessError, match="written permission"):
        source.discover(
            datetime(2026, 7, 23, tzinfo=UTC),
            datetime(2026, 7, 25, tzinfo=UTC),
        )


def test_authorized_feed_requires_verified_date(settings, tmp_path: Path) -> None:
    feed = tmp_path / "authorized.jsonl"
    records = [
        {
            "source_listing_id": "id9990001",
            "canonical_url": "https://example.invalid/authorized/id9990001",
            "title": "Synthetic Breguet Classique 5177",
            "brand": "Breguet",
            "original_posted_at_utc": "2026-07-24T12:00:00Z",
            "date_evidence": "synthetic licensed feed",
            "status": "Active",
            "asking_price_original": "14500",
            "currency": "USD",
        },
        {
            "source_listing_id": "id9990002",
            "canonical_url": "https://example.invalid/authorized/id9990002",
            "title": "Synthetic Patek 6119G without date",
            "brand": "Patek Philippe",
            "status": "Active",
            "asking_price_original": "25000",
            "currency": "USD",
        },
        {
            "source_listing_id": "id9990003",
            "canonical_url": "https://example.invalid/authorized/id9990003",
            "title": "Synthetic malformed Breguet record",
            "brand": "Breguet",
            "original_posted_at_utc": "2026-07-24T12:00:00",
            "status": "Active",
            "asking_price_original": "NaN",
            "currency": "US dollars",
        },
    ]
    feed.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
    settings.sources.chrono24.enabled = True
    settings.sources.chrono24.access_authorized = True
    settings.sources.chrono24.authorized_feed_path = feed
    source = Chrono24AuthorizedFeedSource(settings)
    candidates = source.discover(
        datetime(2026, 7, 23, tzinfo=UTC),
        datetime(2026, 7, 25, tzinfo=UTC),
    )
    assert [candidate.source_listing_id for candidate in candidates] == ["id9990001"]
    failures = source.drain_record_failures()
    assert len(failures) == 1
    assert failures[0].record_id == "id9990003"
