from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select

from watch_tracker.database.models import (
    Base,
    Comparable,
    DealScore,
    DuplicateGroup,
    ErasureEvent,
    Listing,
    ListingObservation,
    ListingPriceHistory,
    ListingStatusHistory,
    Valuation,
)
from watch_tracker.database.repository import Repository
from watch_tracker.database.session import create_sqlite_engine, make_session_factory
from watch_tracker.domain import Confidence, ListingCandidate, ListingStatus


def _candidate(status: ListingStatus = ListingStatus.ACTIVE) -> ListingCandidate:
    return ListingCandidate(
        source="fixture",
        source_listing_id="fixture-001",
        canonical_url="https://example.invalid/listing/1",
        title="Synthetic JLC listing",
        original_posted_at_utc=datetime(2026, 7, 25, tzinfo=UTC),
        date_evidence="synthetic fixture",
        date_confidence=Confidence.HIGH,
        current_status=status,
        status_evidence=f"fixture {status.value}",
        brand="Jaeger-LeCoultre",
        reference_number="Q4018420",
        asking_price_original=Decimal("6250"),
        currency="USD",
        asking_price_usd=Decimal("6250"),
        stated_shipping_cost=Decimal("50"),
        estimated_all_in_original=Decimal("6300"),
        estimated_all_in_usd=Decimal("6300"),
        sold_price_original=None,
        raw_payload={"parser_version": "fixture-1"},
    )


def test_idempotent_upsert_and_status_history(settings) -> None:
    engine = create_sqlite_engine(settings.paths.database)
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    now = datetime(2026, 7, 25, 7, tzinfo=UTC)
    with factory() as session:
        repository = Repository(session)
        repository.create_run("run-1", now, now - timedelta(hours=48), now, False)
        first = repository.upsert_candidate(_candidate(), "run-1", now, "fixture")
        second = repository.upsert_candidate(_candidate(), "run-1", now, "fixture")
        session.commit()
        assert first.created is True
        assert second.created is False
        assert session.scalar(select(func.count(Listing.id))) == 1
        assert session.scalar(select(func.count(ListingPriceHistory.id))) == 2
        assert session.scalar(select(func.count(ListingStatusHistory.id))) == 1
        history = session.scalar(
            select(ListingPriceHistory).where(ListingPriceHistory.price_type == "asking")
        )
        assert history is not None
        assert history.amount_usd == Decimal("6250")
        listing = session.scalar(select(Listing))
        assert listing is not None
        assert listing.estimated_all_in_usd == Decimal("6300")

    with factory() as session:
        repository = Repository(session)
        later = now + timedelta(days=1)
        repository.create_run("run-2", later, later - timedelta(hours=48), later, False)
        sold = _candidate(ListingStatus.SOLD)
        repository.upsert_candidate(sold, "run-2", later, "fixture")
        session.commit()
        listing = session.scalar(select(Listing))
        assert listing is not None
        assert listing.is_sold is True
        assert listing.sold_price_original is None
        assert session.scalar(select(func.count(ListingStatusHistory.id))) == 2


def test_sparse_refresh_preserves_known_listing_fields(settings) -> None:
    engine = create_sqlite_engine(settings.paths.database)
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    now = datetime(2026, 7, 25, 7, tzinfo=UTC)
    initial = _candidate()
    initial.seller_name = "synthetic_seller"
    with factory() as session:
        repository = Repository(session)
        repository.create_run("run-1", now, now - timedelta(hours=48), now, False)
        repository.upsert_candidate(initial, "run-1", now, "fixture")
        session.commit()

    later = now + timedelta(days=1)
    sparse = ListingCandidate(
        source="fixture",
        source_listing_id="fixture-001",
        canonical_url="https://example.invalid/listing/1",
        title="Synthetic status-only refresh",
        original_posted_at_utc=None,
        date_evidence=None,
        date_confidence=Confidence.LOW,
        current_status=ListingStatus.PENDING,
        status_evidence="synthetic pending marker",
        raw_payload={"parser_version": "fixture-status-1"},
    )
    with factory() as session:
        repository = Repository(session)
        repository.create_run("run-2", later, later - timedelta(hours=48), later, False)
        repository.upsert_candidate(sparse, "run-2", later, "fixture")
        session.commit()
        listing = session.scalar(select(Listing))
        assert listing is not None
        assert listing.current_status == ListingStatus.PENDING.value
        assert listing.reference_number == "Q4018420"
        assert listing.seller_name == "synthetic_seller"
        assert listing.latest_asking_price_original == Decimal("6250")
        assert listing.original_posted_at_utc is not None


def test_deleted_reddit_content_is_purged(settings) -> None:
    engine = create_sqlite_engine(settings.paths.database)
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    now = datetime(2026, 7, 25, 7, tzinfo=UTC)
    candidate = _candidate()
    candidate.source = "reddit"
    candidate.source_listing_id = "t3_fixture"
    candidate.raw_payload["source_ad_id"] = "t3_fixture"
    candidate.seller_name = "synthetic_user"
    with factory() as session:
        repository = Repository(session)
        repository.create_run("run-1", now, now - timedelta(hours=48), now, False)
        repository.upsert_candidate(candidate, "run-1", now, "fixture")
        repository.purge_deleted_reddit_ad("t3_fixture", "run-1", now)
        session.commit()
        listing = session.scalar(select(Listing))
        assert listing is not None
        assert listing.current_status == ListingStatus.REMOVED.value
        assert listing.seller_name is None
        assert listing.latest_asking_price_original is None
        assert listing.title == "[purged deleted Reddit content]"
        assert listing.original_posted_at_utc is None
        assert listing.is_sold is False
        event = session.scalar(select(ErasureEvent))
        assert event is not None
        assert event.status == "Pending"
        assert event.event_type == "reddit_content_deletion"


def test_refresh_scope_includes_only_recent_sold_records_missing_sale_price(settings) -> None:
    engine = create_sqlite_engine(settings.paths.database)
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    now = datetime(2026, 7, 25, 7, tzinfo=UTC)
    active = _candidate()
    active.source = "chrono24"
    active.source_listing_id = "active"
    active.canonical_url = "https://example.invalid/listing/active"
    recent_sold = _candidate(ListingStatus.SOLD)
    recent_sold.source = "chrono24"
    recent_sold.source_listing_id = "recent-sold"
    recent_sold.canonical_url = "https://example.invalid/listing/recent-sold"
    old_sold = _candidate(ListingStatus.SOLD)
    old_sold.source = "chrono24"
    old_sold.source_listing_id = "old-sold"
    old_sold.canonical_url = "https://example.invalid/listing/old-sold"

    with factory() as session:
        repository = Repository(session)
        repository.create_run("run-refresh", now, now - timedelta(hours=48), now, False)
        repository.upsert_candidate(active, "run-refresh", now, "fixture")
        repository.upsert_candidate(recent_sold, "run-refresh", now, "fixture")
        repository.upsert_candidate(
            old_sold,
            "run-refresh",
            now - timedelta(days=30),
            "fixture",
        )
        session.commit()

        refreshable = repository.listings_for_refresh(
            "chrono24",
            sold_recheck_cutoff=now - timedelta(days=14),
        )

    assert {listing.source_listing_id for listing in refreshable} == {
        "active",
        "recent-sold",
    }


def test_deleted_evidence_invalidates_entire_derived_valuation_run(settings) -> None:
    engine = create_sqlite_engine(settings.paths.database)
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    now = datetime(2026, 7, 25, 7, tzinfo=UTC)
    target_candidate = _candidate()
    target_candidate.source_listing_id = "target"
    target_candidate.canonical_url = "https://example.invalid/listing/target"
    evidence_candidate = _candidate()
    evidence_candidate.source = "reddit"
    evidence_candidate.source_listing_id = "t3_evidence#watch-jlc"
    evidence_candidate.canonical_url = (
        "https://www.reddit.com/r/Watchexchange/comments/evidence/example#watch-jlc"
    )
    evidence_candidate.raw_payload["source_ad_id"] = "t3_evidence"

    with factory() as session:
        repository = Repository(session)
        repository.create_run("run-lineage", now, now - timedelta(hours=48), now, False)
        target = repository.upsert_candidate(
            target_candidate, "run-lineage", now, "fixture"
        ).listing
        evidence = repository.upsert_candidate(
            evidence_candidate, "run-lineage", now, "fixture"
        ).listing
        valuation = Valuation(
            listing_id=target.id,
            run_id="run-lineage",
            calculated_at_utc=now,
            method_version="fixture-v1",
            input_fingerprint="a" * 64,
            fair_value_low_usd=Decimal("6000"),
            fair_value_mid_usd=Decimal("6250"),
            fair_value_high_usd=Decimal("6500"),
            discount_to_fair_value_pct=0.0,
            comparable_count=2,
            completed_sale_comparable_count=1,
            confidence="Medium",
            assumptions=[],
        )
        session.add(valuation)
        session.flush()
        session.add(
            DealScore(
                listing_id=target.id,
                valuation_id=valuation.id,
                run_id="run-lineage",
                calculated_at_utc=now,
                method_version="fixture-v1",
                price_points=4,
                condition_points=2,
                safety_points=1,
                liquidity_points=1,
                pre_cap_score=8,
                total_score=8,
                cap_reason=None,
                confidence="Medium",
                risk_level="Moderate",
                rationale="fixture",
                recommended_action="Verify",
                suggested_opening_offer_usd=None,
            )
        )
        for source_url, evidence_listing_id in (
            (evidence.canonical_url, evidence.id),
            ("https://example.invalid/independent", None),
        ):
            session.add(
                Comparable(
                    listing_id=target.id,
                    evidence_listing_id=evidence_listing_id,
                    run_id="run-lineage",
                    source="fixture",
                    source_url=source_url,
                    observed_at_utc=now,
                    transaction_date=None,
                    reference_number="Q4018420",
                    condition="Good",
                    box_included=True,
                    papers_included=True,
                    price_type="asking",
                    price_original=Decimal("6250"),
                    currency="USD",
                    price_usd=Decimal("6250"),
                    relevance_weight=0.6,
                    evidence="fixture",
                )
            )
        session.flush()

        assert repository.purge_deleted_reddit_ad("t3_evidence", "run-lineage", now) == 1
        session.commit()

        assert session.scalar(select(func.count(Comparable.id))) == 0
        assert session.scalar(select(func.count(Valuation.id))) == 0
        assert session.scalar(select(func.count(DealScore.id))) == 0
        assert session.get(Listing, target.id) is not None
        assert repository.has_pending_erasures() is True


def test_author_erasure_removes_history_and_reopens_completed_outbox(settings) -> None:
    engine = create_sqlite_engine(settings.paths.database)
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    now = datetime(2026, 7, 25, 7, tzinfo=UTC)
    source_ad_id = "t3_author"
    author = "SecretSeller42"

    initial = _candidate()
    initial.source = "reddit"
    initial.source_listing_id = f"{source_ad_id}#watch-jlc"
    initial.canonical_url = (
        "https://www.reddit.com/r/Watchexchange/comments/author/example#watch-jlc"
    )
    initial.raw_payload["source_ad_id"] = source_ad_id
    initial.seller_name = author
    initial.seller_location = "Private location"
    initial.seller_reputation_evidence = f"{author} has 12 transactions"
    initial.title = f"Watch offered by {author}"
    initial.description_summary = f"Contact {author} for details"

    deleted = _candidate()
    deleted.source = "reddit"
    deleted.source_listing_id = initial.source_listing_id
    deleted.canonical_url = initial.canonical_url
    deleted.raw_payload.update(
        source_ad_id=source_ad_id,
        author_deleted=True,
        full_snapshot=True,
    )
    deleted.title = f"Old post from {author.upper()}"
    deleted.description_summary = f"Message {author} directly"
    deleted.status_evidence = f"{author} account is gone"

    with factory() as session:
        repository = Repository(session)
        repository.create_run("run-author-1", now, now - timedelta(hours=48), now, False)
        repository.upsert_candidate(initial, "run-author-1", now, "fixture")
        session.commit()

        later = now + timedelta(hours=1)
        repository.create_run("run-author-2", later, later - timedelta(hours=48), later, False)
        result = repository.upsert_candidate(deleted, "run-author-2", later, "fixture")
        session.commit()

        assert result.erasure_events == 1
        listing = result.listing
        assert listing.seller_name is None
        assert listing.seller_location is None
        assert listing.title == "[Reddit listing; author deleted]"
        observations = list(
            session.scalars(
                select(ListingObservation).where(ListingObservation.listing_id == listing.id)
            )
        )
        serialized = str(
            [
                listing.title,
                listing.description_summary,
                listing.status_evidence,
                *[
                    (observation.parsed_fields, observation.evidence_excerpt)
                    for observation in observations
                ],
            ]
        ).casefold()
        assert author.casefold() not in serialized
        assert len(observations) == 1

        event = repository.pending_erasure_events()[0]
        repository.mark_erasure_events_complete([event.id], later)
        session.commit()
        assert repository.has_pending_erasures() is False

        reappeared_at = later + timedelta(hours=1)
        repository.create_run(
            "run-author-3",
            reappeared_at,
            reappeared_at - timedelta(hours=48),
            reappeared_at,
            False,
        )
        repository.upsert_candidate(initial, "run-author-3", reappeared_at, "fixture")
        erased_again_at = reappeared_at + timedelta(hours=1)
        repository.create_run(
            "run-author-4",
            erased_again_at,
            erased_again_at - timedelta(hours=48),
            erased_again_at,
            False,
        )
        reopened = repository.upsert_candidate(deleted, "run-author-4", erased_again_at, "fixture")
        session.commit()

        assert reopened.erasure_events == 1
        event = session.scalar(select(ErasureEvent))
        assert event is not None
        assert event.status == "Pending"
        assert event.detected_run_id == "run-author-4"
        assert event.physical_scrub_completed_at_utc is None
        assert event.artifacts_regenerated_at_utc is None

        repository.mark_erasure_events_failed(
            [event.id],
            RuntimeError(f"failure involving {author}\nsecret details"),
        )
        session.commit()
        assert event.status == "Pending"
        assert author.casefold() not in (event.last_error or "").casefold()


def test_reconcile_missing_children_preserves_current_and_terminal_offers(settings) -> None:
    engine = create_sqlite_engine(settings.paths.database)
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    now = datetime(2026, 7, 25, 7, tzinfo=UTC)

    def child(identifier: str, status: ListingStatus) -> ListingCandidate:
        candidate = _candidate(status)
        candidate.source = "reddit"
        candidate.source_listing_id = f"t3_multi#{identifier}"
        candidate.canonical_url = (
            f"https://www.reddit.com/r/Watchexchange/comments/multi/example#{identifier}"
        )
        candidate.raw_payload["source_ad_id"] = "t3_multi"
        return candidate

    with factory() as session:
        repository = Repository(session)
        repository.create_run("run-children-1", now, now - timedelta(hours=48), now, False)
        for candidate in (
            child("current", ListingStatus.ACTIVE),
            child("missing", ListingStatus.ACTIVE),
            child("sold", ListingStatus.SOLD),
        ):
            repository.upsert_candidate(candidate, "run-children-1", now, "fixture")
        later = now + timedelta(hours=1)
        repository.create_run("run-children-2", later, later - timedelta(hours=48), later, False)

        changed = repository.reconcile_missing_source_children(
            "reddit",
            {"t3_multi"},
            {"t3_multi#current"},
            "run-children-2",
            later,
        )
        session.commit()

        statuses = {
            listing.source_listing_id: listing.current_status
            for listing in session.scalars(select(Listing))
        }
        assert changed == 1
        assert statuses["t3_multi#current"] == ListingStatus.ACTIVE.value
        assert statuses["t3_multi#missing"] == ListingStatus.UNAVAILABLE.value
        assert statuses["t3_multi#sold"] == ListingStatus.SOLD.value


def test_conservative_cross_source_duplicate_grouping(settings) -> None:
    engine = create_sqlite_engine(settings.paths.database)
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    now = datetime(2026, 7, 25, 7, tzinfo=UTC)

    reddit = _candidate()
    reddit.source = "reddit"
    reddit.source_listing_id = "t3_crosspost#watch-jlc"
    reddit.canonical_url = (
        "https://www.reddit.com/r/Watchexchange/comments/crosspost/example#watch-jlc"
    )
    reddit.raw_payload["source_ad_id"] = "t3_crosspost"
    reddit.seller_name = "  Same Seller  "
    reddit.seller_type = "private"

    chrono = _candidate()
    chrono.source = "chrono24"
    chrono.source_listing_id = "chrono-crosspost"
    chrono.canonical_url = "https://www.chrono24.com/watch/chrono-crosspost"
    chrono.seller_name = "same seller"
    chrono.seller_type = "individual"

    dealer = _candidate()
    dealer.source = "chrono24"
    dealer.source_listing_id = "chrono-dealer"
    dealer.canonical_url = "https://www.chrono24.com/watch/chrono-dealer"
    dealer.seller_name = "Same Seller"
    dealer.seller_type = "dealer"

    with factory() as session:
        repository = Repository(session)
        repository.create_run("run-crosspost", now, now - timedelta(hours=48), now, False)
        first = repository.upsert_candidate(reddit, "run-crosspost", now, "fixture").listing
        second = repository.upsert_candidate(
            chrono,
            "run-crosspost",
            now + timedelta(hours=1),
            "fixture",
        ).listing
        third = repository.upsert_candidate(
            dealer,
            "run-crosspost",
            now + timedelta(hours=2),
            "fixture",
        ).listing
        session.commit()

        assert first.duplicate_group_id is not None
        assert second.duplicate_group_id == first.duplicate_group_id
        assert third.duplicate_group_id is None
        assert session.scalar(select(func.count(DuplicateGroup.duplicate_group_id))) == 1

        later = now + timedelta(days=100)
        repository.create_run(
            "run-crosspost-later",
            later,
            later - timedelta(hours=48),
            later,
            False,
        )
        later_reddit = _candidate()
        later_reddit.source = "reddit"
        later_reddit.source_listing_id = "t3_later#watch-jlc"
        later_reddit.canonical_url = (
            "https://www.reddit.com/r/Watchexchange/comments/later/example#watch-jlc"
        )
        later_reddit.raw_payload["source_ad_id"] = "t3_later"
        later_reddit.seller_name = "Same Seller"
        later_reddit.seller_type = "private"
        later_reddit.original_posted_at_utc = later
        later_chrono = _candidate()
        later_chrono.source = "chrono24"
        later_chrono.source_listing_id = "chrono-later"
        later_chrono.canonical_url = "https://www.chrono24.com/watch/chrono-later"
        later_chrono.seller_name = "Same Seller"
        later_chrono.seller_type = "individual"
        later_chrono.original_posted_at_utc = later + timedelta(hours=1)

        later_first = repository.upsert_candidate(
            later_reddit,
            "run-crosspost-later",
            later,
            "fixture",
        ).listing
        later_second = repository.upsert_candidate(
            later_chrono,
            "run-crosspost-later",
            later + timedelta(hours=1),
            "fixture",
        ).listing
        session.commit()

        assert later_first.duplicate_group_id == later_second.duplicate_group_id
        assert later_first.duplicate_group_id != first.duplicate_group_id
        assert session.scalar(select(func.count(DuplicateGroup.duplicate_group_id))) == 2
