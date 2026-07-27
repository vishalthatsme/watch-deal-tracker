from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from watch_tracker.config import Settings
from watch_tracker.database.models import Comparable, Listing, Valuation
from watch_tracker.domain import Confidence


def _weighted_percentile(
    values: list[tuple[Decimal, Decimal]],
    fraction: Decimal,
) -> Decimal:
    ordered = sorted(values, key=lambda item: item[0])
    total_weight = sum((weight for _, weight in ordered), Decimal("0"))
    target = total_weight * fraction
    cumulative = Decimal("0")
    for value, weight in ordered:
        cumulative += weight
        if cumulative >= target:
            return value
    return ordered[-1][0]


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class ValuationService:
    """Evidence-preserving exact-match valuation using collected records."""

    def __init__(self, settings: Settings, session: Session) -> None:
        self.settings = settings
        self.session = session

    def _matches(self, listing: Listing, calculated_at: datetime) -> list[Listing]:
        statement = select(Listing).where(
            Listing.id != listing.id,
            Listing.brand == listing.brand,
            or_(
                Listing.sold_price_usd.is_not(None),
                Listing.estimated_all_in_usd.is_not(None),
            ),
        )
        if listing.duplicate_group_id:
            statement = statement.where(
                or_(
                    Listing.duplicate_group_id.is_(None),
                    Listing.duplicate_group_id != listing.duplicate_group_id,
                )
            )
        if listing.reference_number:
            statement = statement.where(Listing.reference_number == listing.reference_number)
        elif listing.model:
            statement = statement.where(Listing.model == listing.model)
        else:
            return []
        cutoff = calculated_at - timedelta(days=365)
        matches = list(
            self.session.scalars(statement.order_by(Listing.last_seen_at_utc.desc()).limit(50))
        )
        return [
            match
            for match in matches
            if match.sold_price_usd is not None or _aware(match.last_seen_at_utc) >= cutoff
        ]

    @staticmethod
    def _dedupe_key(
        match: Listing,
        observed_price: Decimal,
        currency: str,
    ) -> tuple[str, ...]:
        if match.duplicate_group_id:
            return ("duplicate_group", match.duplicate_group_id)
        seller = (match.seller_name or "").strip().casefold()
        watch_key = (match.reference_number or match.model or "").strip().casefold()
        if seller and watch_key:
            return (
                "seller_watch_price",
                seller,
                watch_key,
                str(observed_price.normalize()),
                currency.upper(),
            )
        return ("listing", match.listing_uid)

    def value(self, listing: Listing, run_id: str, calculated_at: datetime) -> Valuation:
        matches = self._matches(listing, calculated_at)
        evidence_values: list[tuple[Decimal, Decimal]] = []
        fingerprint_items: list[dict[str, str | None]] = []
        completed = 0
        ignored_unverified_sales = 0
        evidence_candidates: list[tuple[Listing, Decimal, Decimal, str, Decimal, str]] = []
        for match in matches:
            if (
                match.sold_price_usd is not None
                and match.sold_price_confidence == Confidence.HIGH.value
            ):
                observed_price = match.sold_price_usd
                adjusted_price = observed_price
                price_type = "completed_sale"
                weight = Decimal("1.0")
            elif match.estimated_all_in_usd is not None:
                if match.sold_price_usd is not None:
                    ignored_unverified_sales += 1
                observed_price = match.estimated_all_in_usd
                adjusted_price = (observed_price * Decimal("0.92")).quantize(Decimal("0.01"))
                price_type = "asking"
                weight = Decimal("0.6")
            else:
                if match.sold_price_usd is not None:
                    ignored_unverified_sales += 1
                continue
            currency = (
                match.sold_price_currency
                if price_type == "completed_sale"
                else match.asking_price_currency
            ) or "USD"
            evidence_candidates.append(
                (match, observed_price, adjusted_price, price_type, weight, currency)
            )

        seen_evidence: set[tuple[str, ...]] = set()
        # Prefer a verified completed sale over an asking record when exact
        # cross-post evidence resolves to the same conservative dedupe key.
        evidence_candidates.sort(key=lambda item: item[3] != "completed_sale")
        for (
            match,
            observed_price,
            adjusted_price,
            price_type,
            weight,
            currency,
        ) in evidence_candidates:
            evidence_key = self._dedupe_key(match, observed_price, currency)
            if evidence_key in seen_evidence:
                continue
            seen_evidence.add(evidence_key)
            evidence_values.append((adjusted_price, weight))
            if price_type == "completed_sale":
                completed += 1
            fingerprint_items.append(
                {
                    "listing_uid": match.listing_uid,
                    "price_type": price_type,
                    "observed_price": str(observed_price),
                    "adjusted_price": str(adjusted_price),
                    "currency": (
                        match.sold_price_currency
                        if price_type == "completed_sale"
                        else match.asking_price_currency
                    ),
                }
            )
            self.session.add(
                Comparable(
                    listing_id=listing.id,
                    evidence_listing_id=match.id,
                    run_id=run_id,
                    source=match.source,
                    source_url=match.canonical_url,
                    observed_at_utc=calculated_at,
                    transaction_date=match.sold_at_utc,
                    reference_number=match.reference_number,
                    condition=match.condition,
                    box_included=match.box_included,
                    papers_included=match.papers_included,
                    price_type=price_type,
                    price_original=(
                        match.sold_price_original
                        if price_type == "completed_sale"
                        else match.latest_asking_price_original
                    )
                    or observed_price,
                    currency=currency,
                    price_usd=observed_price,
                    relevance_weight=float(weight),
                    evidence=(
                        match.sold_price_evidence
                        if price_type == "completed_sale"
                        else "Current ask adjusted by 8%; not a completed transaction"
                    ),
                )
            )

        assumptions: list[str] = []
        if ignored_unverified_sales:
            assumptions.append(
                f"Ignored {ignored_unverified_sales} sold-price record(s) without "
                "high-confidence explicit sale evidence"
            )
        low: Decimal | None
        mid: Decimal | None
        high: Decimal | None
        effective_weight = sum((weight for _, weight in evidence_values), Decimal("0"))
        if evidence_values:
            low = _weighted_percentile(evidence_values, Decimal("0.25"))
            mid = _weighted_percentile(evidence_values, Decimal("0.50"))
            high = _weighted_percentile(evidence_values, Decimal("0.75"))
            if completed < len(evidence_values):
                assumptions.append(
                    "Asking comparables are labeled, weighted 0.6, and adjusted down 8%"
                )
            if completed >= 3:
                confidence = Confidence.HIGH
            elif completed >= 1 and len(evidence_values) >= 3 and effective_weight >= 2:
                confidence = Confidence.MEDIUM
            else:
                confidence = Confidence.LOW
                assumptions.append("Completed-sale evidence is insufficient for ranking")
        else:
            low = mid = high = None
            confidence = Confidence.LOW
            assumptions.append(
                "Insufficient independent comparable evidence; no fair value was estimated"
            )

        discount: float | None = None
        asking = listing.estimated_all_in_usd
        if asking is not None and mid is not None and mid > 0:
            discount = float(((mid - asking) / mid) * 100)

        fingerprint_payload = {
            "method": self.settings.scoring.valuation_version,
            "listing_price": str(asking) if asking is not None else None,
            "evidence": sorted(fingerprint_items, key=lambda item: item["listing_uid"] or ""),
        }
        fingerprint = hashlib.sha256(
            json.dumps(fingerprint_payload, sort_keys=True).encode("utf-8")
        ).hexdigest()
        valuation = Valuation(
            listing_id=listing.id,
            run_id=run_id,
            calculated_at_utc=calculated_at,
            method_version=self.settings.scoring.valuation_version,
            input_fingerprint=fingerprint,
            fair_value_low_usd=low,
            fair_value_mid_usd=mid,
            fair_value_high_usd=high,
            discount_to_fair_value_pct=discount,
            comparable_count=len(evidence_values),
            completed_sale_comparable_count=completed,
            confidence=confidence.value,
            assumptions=assumptions,
        )
        self.session.add(valuation)
        self.session.flush()
        return valuation
