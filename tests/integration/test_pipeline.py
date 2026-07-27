from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from watch_tracker.database.models import (
    Base,
    CollectionError,
    DealScore,
    ErasureEvent,
    Listing,
    Run,
    Valuation,
)
from watch_tracker.database.session import create_sqlite_engine, make_session_factory
from watch_tracker.domain import Confidence, ListingCandidate, ListingStatus
from watch_tracker.pipeline import DailyPipeline
from watch_tracker.services.exports import ExportService
from watch_tracker.sources.base import RefreshOutcome, SourceAccessError, SourceAdapter


class FixtureSource(SourceAdapter):
    name = "fixture"

    def __init__(self) -> None:
        self.candidate = ListingCandidate(
            source="fixture",
            source_listing_id="fixture-001",
            canonical_url="https://example.invalid/listing/1",
            title="Synthetic Patek listing",
            original_posted_at_utc=datetime(2026, 7, 24, tzinfo=UTC),
            date_evidence="synthetic fixture",
            date_confidence=Confidence.HIGH,
            current_status=ListingStatus.ACTIVE,
            status_evidence="synthetic active",
            brand="Patek Philippe",
            reference_number="6119G",
            asking_price_original=Decimal("25000"),
            currency="USD",
            raw_payload={"source_ad_id": "fixture-001", "parser_version": "fixture-1"},
        )

    def discover(self, window_start, window_end):
        if window_start <= self.candidate.original_posted_at_utc <= window_end:
            return [self.candidate]
        return []

    def refresh(self, source_ad_ids):
        return RefreshOutcome([self.candidate] if source_ad_ids else [], set())


def test_two_runs_create_one_listing_and_exports(settings) -> None:
    engine = create_sqlite_engine(settings.paths.database)
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    source = FixtureSource()
    pipeline = DailyPipeline(settings, factory, [source])
    as_of = datetime(2026, 7, 25, 7, tzinfo=UTC)
    first = pipeline.run(as_of=as_of)
    second = pipeline.run(as_of=as_of)
    assert first.status == "Success"
    assert second.status == "Success"
    with factory() as session:
        assert session.scalar(select(func.count(Listing.id))) == 1
        assert session.scalar(select(func.count(Valuation.id))) == 1
        assert session.scalar(select(func.count(DealScore.id))) == 2
        valuation = session.scalar(select(Valuation))
        assert valuation is not None
        assert valuation.fair_value_mid_usd is None
        assert valuation.comparable_count == 0
        valuation_ids = set(session.scalars(select(DealScore.valuation_id)))
        assert len(valuation_ids) == 1
        assert None not in valuation_ids
    assert (settings.paths.exports / "watch_listings_latest.csv").exists()
    assert (settings.paths.exports / "watch_active_deals.csv").exists()


class MixedQualitySource(FixtureSource):
    def discover(self, window_start, window_end):
        invalid = ListingCandidate(
            source="fixture",
            source_listing_id="fixture-invalid",
            canonical_url="https://example.invalid/listing/invalid",
            title=None,  # type: ignore[arg-type]
            original_posted_at_utc=self.candidate.original_posted_at_utc,
            date_evidence="synthetic malformed fixture",
            date_confidence=Confidence.HIGH,
            current_status=ListingStatus.ACTIVE,
            brand="Patek Philippe",
            reference_number="BAD",
            raw_payload={"parser_version": "fixture-1"},
        )
        return [self.candidate, invalid]


def test_malformed_record_isolated_and_run_finishes_partial(settings) -> None:
    engine = create_sqlite_engine(settings.paths.database)
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)

    result = DailyPipeline(settings, factory, [MixedQualitySource()]).run(
        as_of=datetime(2026, 7, 25, 7, tzinfo=UTC),
        refresh=False,
    )

    assert result.status == "Partial"
    assert result.counts["errors"] == 1
    with factory() as session:
        assert session.scalar(select(func.count(Listing.id))) == 1
        assert session.scalar(select(func.count(CollectionError.id))) == 1
        run = session.get(Run, result.run_id)
        assert run is not None
        assert run.status == "Partial"
        assert run.completed_at_utc is not None


class OutageSource(FixtureSource):
    def discover(self, window_start, window_end):
        raise RuntimeError("synthetic upstream outage")


class PermissionSource(FixtureSource):
    def discover(self, window_start, window_end):
        raise SourceAccessError("fixture", "approval_required", "synthetic permission gate")


@pytest.mark.parametrize(
    ("source", "expected_status"),
    [(OutageSource(), "Failed"), (PermissionSource(), "Partial")],
)
def test_incomplete_collection_preserves_last_known_good_exports(
    settings,
    source,
    expected_status,
) -> None:
    engine = create_sqlite_engine(settings.paths.database)
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    latest = settings.paths.exports / "watch_listings_latest.csv"
    latest.write_text("last-known-good\n", encoding="utf-8")

    result = DailyPipeline(settings, factory, [source]).run(
        as_of=datetime(2026, 7, 25, 7, tzinfo=UTC)
    )

    assert result.status == expected_status
    assert latest.read_text(encoding="utf-8") == "last-known-good\n"
    assert not result.exports
    assert any("last known good" in message for message in result.messages)


def test_collection_with_no_enabled_sources_fails_without_publishing(settings) -> None:
    engine = create_sqlite_engine(settings.paths.database)
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)

    result = DailyPipeline(settings, factory, []).run(as_of=datetime(2026, 7, 25, 7, tzinfo=UTC))

    assert result.status == "Failed"
    assert result.counts["errors"] == 1
    assert not result.exports


def test_single_source_diagnostic_does_not_replace_full_exports(settings) -> None:
    engine = create_sqlite_engine(settings.paths.database)
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    latest = settings.paths.exports / "watch_listings_latest.csv"
    latest.write_text("full-market-snapshot\n", encoding="utf-8")

    result = DailyPipeline(settings, factory, [FixtureSource()]).run(
        as_of=datetime(2026, 7, 25, 7, tzinfo=UTC),
        source_filter="fixture",
    )

    assert result.status == "Success"
    assert not result.exports
    assert latest.read_text(encoding="utf-8") == "full-market-snapshot\n"


class ParseDiagnosticSource(FixtureSource):
    def discover(self, window_start, window_end):
        self.record_failure(
            "discover_parse",
            ValueError("synthetic rejected source record"),
            "fixture-rejected",
        )
        return [self.candidate]


def test_adapter_parse_failure_is_recorded_without_losing_valid_records(settings) -> None:
    engine = create_sqlite_engine(settings.paths.database)
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)

    result = DailyPipeline(settings, factory, [ParseDiagnosticSource()]).run(
        as_of=datetime(2026, 7, 25, 7, tzinfo=UTC),
        refresh=False,
    )

    assert result.status == "Partial"
    assert result.source_statuses == {"fixture": "Partial"}
    with factory() as session:
        assert session.scalar(select(func.count(Listing.id))) == 1
        error = session.scalar(select(CollectionError))
        assert error is not None
        assert error.stage == "discover_parse"


def test_fatal_export_error_finalizes_run_as_failed(settings, monkeypatch) -> None:
    engine = create_sqlite_engine(settings.paths.database)
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)

    def fail_export(*args, **kwargs):
        raise RuntimeError("synthetic export failure")

    monkeypatch.setattr(ExportService, "export", fail_export)
    result = DailyPipeline(settings, factory, [FixtureSource()]).run(
        as_of=datetime(2026, 7, 25, 7, tzinfo=UTC)
    )

    assert result.status == "Failed"
    with factory() as session:
        run = session.get(Run, result.run_id)
        assert run is not None
        assert run.status == "Failed"
        assert run.completed_at_utc is not None
        assert session.scalar(select(func.count(CollectionError.id))) == 1


class RedditDeletionSource(SourceAdapter):
    name = "reddit"

    def __init__(self) -> None:
        self.candidate = ListingCandidate(
            source="reddit",
            source_listing_id="t3_sensitive#item:patek-philippe",
            canonical_url=(
                "https://www.reddit.com/r/Watchexchange/comments/sensitive/#watch-patek-philippe"
            ),
            title="Sensitive seller-authored listing text",
            original_posted_at_utc=datetime(2026, 7, 24, tzinfo=UTC),
            date_evidence="Reddit created_utc fixture",
            date_confidence=Confidence.HIGH,
            current_status=ListingStatus.ACTIVE,
            status_evidence="visible fixture",
            brand="Patek Philippe",
            reference_number="6119G",
            seller_name="sensitive_seller",
            asking_price_original=Decimal("25000"),
            currency="USD",
            raw_payload={
                "source_ad_id": "t3_sensitive",
                "parser_version": "fixture-1",
                "full_snapshot": True,
            },
        )

    def discover(self, window_start, window_end):
        return [self.candidate]

    def refresh(self, source_ad_ids):
        assert source_ad_ids == ["t3_sensitive"]
        return RefreshOutcome([], {"t3_sensitive"})


class EmptyRedditSource(SourceAdapter):
    name = "reddit"

    def discover(self, window_start, window_end):
        return []

    def refresh(self, source_ad_ids):
        return RefreshOutcome([], set())


def test_reddit_erasure_resets_artifacts_and_exports_tombstone(settings) -> None:
    engine = create_sqlite_engine(settings.paths.database)
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)

    result = DailyPipeline(settings, factory, [RedditDeletionSource()]).run(
        as_of=datetime(2026, 7, 25, 7, tzinfo=UTC)
    )

    assert result.status == "Success"
    assert result.counts["compliance_purges"] == 1
    backups = list(settings.paths.backups.glob("watch_market_*.sqlite"))
    assert len(backups) == 1
    assert "post_erasure" in backups[0].name
    exported = (settings.paths.exports / "watch_listings_latest.csv").read_text(encoding="utf-8")
    assert "sensitive_seller" not in exported
    assert "Sensitive seller-authored" not in exported
    assert "[purged deleted Reddit content]" in exported
    with factory() as session:
        listing = session.scalar(select(Listing))
        assert listing is not None
        assert listing.brand is None
        assert listing.seller_name is None
        assert listing.latest_asking_price_original is None
        assert listing.current_status == ListingStatus.REMOVED.value


def test_pending_erasure_resumes_after_artifact_regeneration_failure(
    settings,
    monkeypatch,
) -> None:
    engine = create_sqlite_engine(settings.paths.database)
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    original_export = ExportService.export

    def fail_sanitized_export(self, generated_at, *, include_analysis=True):
        if not include_analysis:
            raise RuntimeError("synthetic sanitized-export interruption")
        return original_export(self, generated_at, include_analysis=include_analysis)

    monkeypatch.setattr(ExportService, "export", fail_sanitized_export)
    failed = DailyPipeline(settings, factory, [RedditDeletionSource()]).run(
        as_of=datetime(2026, 7, 25, 7, tzinfo=UTC)
    )

    assert failed.status == "Failed"
    with factory() as session:
        event = session.scalar(select(ErasureEvent))
        assert event is not None
        assert event.status == "Pending"
        assert event.last_error

    monkeypatch.setattr(ExportService, "export", original_export)
    resumed = DailyPipeline(settings, factory, [EmptyRedditSource()]).run(
        as_of=datetime(2026, 7, 25, 8, tzinfo=UTC)
    )

    assert resumed.status == "Success"
    with factory() as session:
        event = session.scalar(select(ErasureEvent))
        assert event is not None
        assert event.status == "Complete"
        assert event.physical_scrub_completed_at_utc is not None
        assert event.artifacts_regenerated_at_utc is not None
    exported = (settings.paths.exports / "watch_listings_latest.csv").read_text(encoding="utf-8")
    assert "sensitive_seller" not in exported


def test_unknown_source_filter_is_rejected(settings) -> None:
    engine = create_sqlite_engine(settings.paths.database)
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with pytest.raises(ValueError, match="Unknown source"):
        DailyPipeline(settings, factory, [FixtureSource()]).run(source_filter="missing")
