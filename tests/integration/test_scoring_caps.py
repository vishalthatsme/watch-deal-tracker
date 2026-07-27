from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from watch_tracker.database.models import Base, Valuation
from watch_tracker.database.repository import Repository
from watch_tracker.database.session import create_sqlite_engine, make_session_factory
from watch_tracker.domain import Confidence, ListingCandidate, ListingStatus
from watch_tracker.services.scoring import ScoringService


def _listing_candidate(source_id: str, risk_flags: list[str]) -> ListingCandidate:
    return ListingCandidate(
        source="fixture",
        source_listing_id=source_id,
        canonical_url=f"https://example.invalid/score/{source_id}",
        title="Synthetic scoring fixture",
        original_posted_at_utc=datetime(2026, 7, 25, tzinfo=UTC),
        date_evidence="synthetic fixture",
        date_confidence=Confidence.HIGH,
        current_status=ListingStatus.ACTIVE,
        status_evidence="synthetic active",
        brand="Patek Philippe",
        reference_number="6119G",
        asking_price_original=Decimal("10000"),
        currency="USD",
        estimated_all_in_original=Decimal("10000"),
        estimated_all_in_usd=Decimal("10000"),
        condition="Excellent",
        box_included=True,
        papers_included=True,
        seller_reputation_evidence="40 Transactions",
        transaction_protection="Synthetic escrow fixture",
        risk_flags=risk_flags,
        raw_payload={"parser_version": "fixture-1"},
    )


def test_authenticity_and_provisional_caps(settings) -> None:
    engine = create_sqlite_engine(settings.paths.database)
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    now = datetime(2026, 7, 25, 7, tzinfo=UTC)
    with factory() as session:
        repository = Repository(session)
        repository.create_run("run-score", now, now - timedelta(hours=48), now, False)
        listing = repository.upsert_candidate(
            _listing_candidate("score-001", ["unresolved authenticity concern"]),
            "run-score",
            now,
            "fixture",
        ).listing
        valuation = Valuation(
            listing_id=listing.id,
            run_id="run-score",
            calculated_at_utc=now,
            method_version="1.0",
            fair_value_low_usd=Decimal("15000"),
            fair_value_mid_usd=Decimal("16000"),
            fair_value_high_usd=Decimal("17000"),
            discount_to_fair_value_pct=37.5,
            comparable_count=3,
            completed_sale_comparable_count=3,
            confidence=Confidence.HIGH.value,
            assumptions=[],
        )
        session.add(valuation)
        session.flush()
        score = ScoringService(settings, session).score(listing, valuation, "run-score", now)
        assert score.total_score == 4.0
        assert score.cap_reason is not None

        provisional_listing = repository.upsert_candidate(
            _listing_candidate("score-002", []),
            "run-score",
            now,
            "fixture",
        ).listing
        provisional = Valuation(
            listing_id=provisional_listing.id,
            run_id="run-score",
            calculated_at_utc=now,
            method_version="provisional-test",
            fair_value_low_usd=Decimal("10000"),
            fair_value_mid_usd=Decimal("10000"),
            fair_value_high_usd=Decimal("10000"),
            discount_to_fair_value_pct=0,
            comparable_count=0,
            completed_sale_comparable_count=0,
            confidence=Confidence.LOW.value,
            assumptions=["synthetic provisional fixture"],
        )
        session.add(provisional)
        session.flush()
        provisional_score = ScoringService(settings, session).score(
            provisional_listing, provisional, "run-score", now
        )
        assert provisional_score.total_score == 5.0
        assert provisional_score.confidence == Confidence.LOW.value
        assert provisional_score.risk_level == "Moderate"
        assert provisional_score.recommended_action == "verify"

        complete_candidate = _listing_candidate("score-003", [])
        complete_candidate.authenticity_notes = "Serial and movement photos supplied"
        complete_listing = repository.upsert_candidate(
            complete_candidate,
            "run-score",
            now,
            "fixture",
        ).listing
        complete_valuation = Valuation(
            listing_id=complete_listing.id,
            run_id="run-score",
            calculated_at_utc=now,
            method_version="complete-test",
            fair_value_low_usd=Decimal("14000"),
            fair_value_mid_usd=Decimal("16000"),
            fair_value_high_usd=Decimal("17000"),
            discount_to_fair_value_pct=37.5,
            comparable_count=3,
            completed_sale_comparable_count=3,
            confidence=Confidence.HIGH.value,
            assumptions=[],
        )
        session.add(complete_valuation)
        session.flush()
        complete_score = ScoringService(settings, session).score(
            complete_listing,
            complete_valuation,
            "run-score",
            now,
        )
        assert complete_score.risk_level == "Low"
        assert complete_score.recommended_action == "pursue"
