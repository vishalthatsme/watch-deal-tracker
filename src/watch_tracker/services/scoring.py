from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from watch_tracker.config import Settings
from watch_tracker.database.models import DealScore, Listing, Valuation
from watch_tracker.domain import Confidence

_LIQUIDITY = {
    "Patek Philippe": 1.0,
    "A. Lange & Söhne": 0.85,
    "Jaeger-LeCoultre": 0.8,
    "Breguet": 0.7,
    "Glashütte Original": 0.7,
}
_TRANSACTION_COUNT = re.compile(r"(?i)\b(\d{1,3}(?:,\d{3})*)\s+transactions?\b")


def _price_points(discount: float | None) -> float:
    if discount is None:
        return 2.5
    if discount >= 25:
        return 6.0
    if discount >= 15:
        return 5.0
    if discount >= 8:
        return 4.0
    if discount >= 2:
        return 3.0
    if discount >= -2:
        return 2.5
    if discount >= -10:
        return 1.5
    return 0.5


def _condition_points(listing: Listing) -> float:
    text = f"{listing.condition or ''} {listing.condition_notes or ''}".casefold()
    if any(term in text for term in ("unworn", "new", "excellent", "mint")):
        points = 1.1
    elif any(term in text for term in ("good", "very good")):
        points = 0.9
    elif any(term in text for term in ("fair", "worn")):
        points = 0.55
    elif any(term in text for term in ("poor", "damaged", "parts")):
        points = 0.2
    else:
        points = 0.6
    if listing.box_included is True:
        points += 0.15
    if listing.papers_included is True:
        points += 0.15
    if listing.service_history:
        points += 0.1
    return min(1.5, points)


def _safety_points(listing: Listing) -> float:
    points = 0.0
    if listing.transaction_protection:
        points += 0.4
    evidence = listing.seller_reputation_evidence or ""
    transactions = max(
        (int(match.group(1).replace(",", "")) for match in _TRANSACTION_COUNT.finditer(evidence)),
        default=0,
    )
    if transactions >= 20:
        points += 0.5
    elif transactions >= 5:
        points += 0.3
    elif transactions > 0:
        points += 0.15
    if listing.authenticity_notes:
        points += 0.4
    if listing.seller_type == "dealer":
        points += 0.1
    points -= min(0.8, len(listing.risk_flags or []) * 0.2)
    return max(0.0, min(1.5, points))


def _missing_safety_evidence(listing: Listing) -> list[str]:
    missing: list[str] = []
    if not listing.transaction_protection:
        missing.append("transaction protection")
    reputation = listing.seller_reputation_evidence or ""
    if _TRANSACTION_COUNT.search(reputation) is None:
        missing.append("verified transaction history")
    if not listing.authenticity_notes:
        missing.append("authenticity evidence")
    return missing


class ScoringService:
    def __init__(self, settings: Settings, session: Session) -> None:
        self.settings = settings
        self.session = session

    def score(
        self,
        listing: Listing,
        valuation: Valuation,
        run_id: str,
        calculated_at: datetime,
    ) -> DealScore:
        price = _price_points(valuation.discount_to_fair_value_pct)
        condition = _condition_points(listing)
        safety = _safety_points(listing)
        missing_safety = _missing_safety_evidence(listing)
        liquidity = _LIQUIDITY.get(listing.brand or "", 0.6)
        if not listing.reference_number:
            liquidity = max(0.25, liquidity - 0.2)
        pre_cap = round(price + condition + safety + liquidity, 1)
        total = max(1.0, min(10.0, pre_cap))
        cap_reason: str | None = None
        flags = " ".join(listing.risk_flags or []).casefold()
        if any(term in flags for term in ("scam", "stolen payment", "impersonation")):
            total = min(total, self.settings.scoring.scam_indicator_cap)
            cap_reason = "Strong scam indicator"
        elif any(term in flags for term in ("authenticity", "counterfeit", "provenance")):
            total = min(total, self.settings.scoring.strong_authenticity_risk_cap)
            cap_reason = "Significant unresolved authenticity or provenance concern"
        provisional = (
            valuation.comparable_count < 3
            or valuation.completed_sale_comparable_count < 1
            or valuation.confidence == Confidence.LOW.value
        )
        if provisional and total > 5.0:
            total = 5.0
            cap_reason = "Insufficient independent comparable evidence"
        if missing_safety:
            total = min(total, 5.5)
            safety_reason = "Missing safety evidence: " + ", ".join(missing_safety)
            cap_reason = f"{cap_reason}; {safety_reason}" if cap_reason else safety_reason
        confidence = (
            Confidence.LOW.value
            if provisional or missing_safety or valuation.confidence == Confidence.LOW.value
            else valuation.confidence
        )
        if cap_reason and total <= 2:
            risk = "Avoid"
        elif listing.risk_flags or len(missing_safety) >= 2:
            risk = "High"
        elif missing_safety or confidence == Confidence.LOW.value:
            risk = "Moderate"
        else:
            risk = "Low"
        if risk == "Avoid":
            action = "pass"
        elif missing_safety:
            action = "verify"
        elif total < 4:
            action = "pass"
        elif total >= 7:
            action = "pursue"
        elif total >= 5.5:
            action = "negotiate"
        else:
            action = "monitor"
        opening_offer: Decimal | None = None
        if action in {"pursue", "negotiate"} and listing.estimated_all_in_usd:
            opening_offer = (listing.estimated_all_in_usd * Decimal("0.92")).quantize(Decimal("1"))
        rationale = (
            f"Price {price:.1f}/6, condition/completeness {condition:.1f}/1.5, "
            f"transaction safety {safety:.1f}/1.5, liquidity {liquidity:.1f}/1. "
            f"Valuation confidence is {valuation.confidence.lower()} from "
            f"{valuation.comparable_count} comparable(s)."
        )
        if missing_safety:
            rationale += " Verify " + ", ".join(missing_safety) + " before transacting."
        score = DealScore(
            listing_id=listing.id,
            valuation_id=valuation.id,
            run_id=run_id,
            calculated_at_utc=calculated_at,
            method_version=self.settings.scoring.version,
            price_points=price,
            condition_points=condition,
            safety_points=safety,
            liquidity_points=liquidity,
            pre_cap_score=pre_cap,
            total_score=round(total, 1),
            cap_reason=cap_reason,
            confidence=confidence,
            risk_level=risk,
            rationale=rationale,
            recommended_action=action,
            suggested_opening_offer_usd=opening_offer,
        )
        self.session.add(score)
        self.session.flush()
        return score
