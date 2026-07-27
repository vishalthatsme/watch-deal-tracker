from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any


class ListingStatus(StrEnum):
    ACTIVE = "Active"
    PENDING = "Pending"
    RESERVED = "Reserved"
    SOLD = "Sold"
    WITHDRAWN = "Withdrawn"
    EXPIRED = "Expired"
    REMOVED = "Removed"
    UNAVAILABLE = "Unavailable"
    UNKNOWN = "Unknown"


class Confidence(StrEnum):
    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"


class RunStatus(StrEnum):
    RUNNING = "Running"
    SUCCESS = "Success"
    PARTIAL = "Partial"
    FAILED = "Failed"
    DRY_RUN = "DryRun"


TERMINAL_STATUSES = {
    ListingStatus.SOLD,
    ListingStatus.WITHDRAWN,
    ListingStatus.EXPIRED,
    ListingStatus.REMOVED,
}

REFRESHABLE_STATUSES = {
    ListingStatus.ACTIVE,
    ListingStatus.PENDING,
    ListingStatus.RESERVED,
    ListingStatus.UNAVAILABLE,
    ListingStatus.UNKNOWN,
}


@dataclass(slots=True)
class ListingCandidate:
    source: str
    source_listing_id: str | None
    canonical_url: str
    title: str
    original_posted_at_utc: datetime | None
    date_evidence: str | None
    date_confidence: Confidence
    current_status: ListingStatus
    status_evidence: str | None = None
    brand: str | None = None
    model: str | None = None
    reference_number: str | None = None
    approximate_year: int | None = None
    case_material: str | None = None
    case_size_mm: Decimal | None = None
    dial_description: str | None = None
    movement_or_caliber: str | None = None
    complications: list[str] = field(default_factory=list)
    condition: str | None = None
    condition_notes: str | None = None
    box_included: bool | None = None
    papers_included: bool | None = None
    accessories: list[str] = field(default_factory=list)
    service_history: str | None = None
    seller_name: str | None = None
    seller_type: str | None = None
    seller_location: str | None = None
    seller_reputation_evidence: str | None = None
    transaction_protection: str | None = None
    return_policy: str | None = None
    asking_price_original: Decimal | None = None
    currency: str | None = None
    asking_price_usd: Decimal | None = None
    stated_shipping_cost: Decimal | None = None
    estimated_all_in_original: Decimal | None = None
    estimated_all_in_usd: Decimal | None = None
    negotiable: bool | None = None
    description_summary: str | None = None
    authenticity_notes: str | None = None
    image_quality_notes: str | None = None
    risk_flags: list[str] = field(default_factory=list)
    missing_information: list[str] = field(default_factory=list)
    questions_to_ask_seller: list[str] = field(default_factory=list)
    sold_at_utc: datetime | None = None
    sold_price_original: Decimal | None = None
    sold_price_currency: str | None = None
    sold_price_usd: Decimal | None = None
    sold_price_evidence: str | None = None
    sold_price_confidence: Confidence | None = None
    raw_payload: dict[str, Any] = field(default_factory=dict)
