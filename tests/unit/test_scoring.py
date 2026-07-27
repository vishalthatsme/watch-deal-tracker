from types import SimpleNamespace

from watch_tracker.services.scoring import (
    _missing_safety_evidence,
    _price_points,
    _safety_points,
)


def test_price_score_boundaries() -> None:
    assert _price_points(25) == 6.0
    assert _price_points(15) == 5.0
    assert _price_points(8) == 4.0
    assert _price_points(2) == 3.0
    assert _price_points(-2) == 2.5
    assert _price_points(-10) == 1.5
    assert _price_points(-10.01) == 0.5
    assert _price_points(None) == 2.5


def test_price_range_in_flair_is_not_misread_as_transaction_history() -> None:
    listing = SimpleNamespace(
        transaction_protection=None,
        seller_reputation_evidence="$6000-$6999",
        authenticity_notes=None,
        seller_type="private",
        risk_flags=[],
    )

    assert _safety_points(listing) == 0.0
    assert _missing_safety_evidence(listing) == [
        "transaction protection",
        "verified transaction history",
        "authenticity evidence",
    ]


def test_strict_transaction_flair_contributes_only_documented_points() -> None:
    listing = SimpleNamespace(
        transaction_protection="escrow available",
        seller_reputation_evidence="1,204 Transactions",
        authenticity_notes="movement and serial photos provided",
        seller_type="private",
        risk_flags=[],
    )

    assert _safety_points(listing) == 1.3
    assert _missing_safety_evidence(listing) == []
