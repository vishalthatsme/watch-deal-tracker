from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select

from watch_tracker.database.models import Base, Comparable
from watch_tracker.database.repository import Repository
from watch_tracker.database.session import create_sqlite_engine, make_session_factory
from watch_tracker.domain import Confidence, ListingCandidate, ListingStatus
from watch_tracker.services.valuation import ValuationService


def _candidate(
    source_id: str,
    *,
    seller: str,
    sold_price: Decimal | None = None,
    sold_confidence: Confidence | None = None,
) -> ListingCandidate:
    return ListingCandidate(
        source="fixture",
        source_listing_id=source_id,
        canonical_url=f"https://example.invalid/valuation/{source_id}",
        title="Synthetic Patek 6119G",
        original_posted_at_utc=datetime(2026, 7, 24, tzinfo=UTC),
        date_evidence="synthetic",
        date_confidence=Confidence.HIGH,
        current_status=ListingStatus.SOLD if sold_price else ListingStatus.ACTIVE,
        status_evidence="synthetic",
        brand="Patek Philippe",
        reference_number="6119G",
        seller_name=seller,
        asking_price_original=None if sold_price else Decimal("10000"),
        currency=None if sold_price else "USD",
        asking_price_usd=None if sold_price else Decimal("10000"),
        estimated_all_in_original=None if sold_price else Decimal("10000"),
        estimated_all_in_usd=None if sold_price else Decimal("10000"),
        sold_price_original=sold_price,
        sold_price_currency="USD" if sold_price else None,
        sold_price_usd=sold_price,
        sold_price_evidence="explicit fixture sale" if sold_price else None,
        sold_price_confidence=sold_confidence,
        raw_payload={"parser_version": "fixture-1"},
    )


def test_valuation_dedupes_crossposts_and_rejects_unverified_sales(settings) -> None:
    engine = create_sqlite_engine(settings.paths.database)
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    now = datetime(2026, 7, 25, 7, tzinfo=UTC)

    with factory() as session:
        repository = Repository(session)
        repository.create_run(
            "run-valuation",
            now,
            now - timedelta(hours=48),
            now,
            False,
        )
        target = repository.upsert_candidate(
            _candidate("target", seller="buyer-facing-seller"),
            "run-valuation",
            now,
            "fixture",
        ).listing
        verified = repository.upsert_candidate(
            _candidate(
                "verified-a",
                seller="same-seller",
                sold_price=Decimal("12000"),
                sold_confidence=Confidence.HIGH,
            ),
            "run-valuation",
            now,
            "fixture",
        ).listing
        crosspost = repository.upsert_candidate(
            _candidate(
                "verified-crosspost",
                seller=" SAME-SELLER ",
                sold_price=Decimal("12000"),
                sold_confidence=Confidence.HIGH,
            ),
            "run-valuation",
            now,
            "fixture",
        )
        repository.upsert_candidate(
            _candidate(
                "unverified",
                seller="other-seller",
                sold_price=Decimal("9000"),
                sold_confidence=Confidence.MEDIUM,
            ),
            "run-valuation",
            now,
            "fixture",
        )

        valuation = ValuationService(settings, session).value(
            target,
            "run-valuation",
            now,
        )
        session.flush()
        comparable = session.scalar(select(Comparable))

        assert valuation.comparable_count == 1
        assert valuation.completed_sale_comparable_count == 1
        assert session.scalar(select(func.count(Comparable.id))) == 1
        assert comparable is not None
        assert comparable.evidence_listing_id in {verified.id, crosspost.listing.id}
        assert any(
            "without high-confidence explicit sale evidence" in assumption
            for assumption in valuation.assumptions
        )
