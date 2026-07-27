from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from watch_tracker.config import Settings
from watch_tracker.domain import Confidence, ListingCandidate, ListingStatus
from watch_tracker.services.identity import identify_brands
from watch_tracker.sources.base import RefreshOutcome, SourceAccessError, SourceAdapter

PARSER_VERSION = "chrono24-authorized-feed-1.0"
LOGGER = logging.getLogger(__name__)


def _datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Authorized feed timestamps must include a UTC offset")
    return parsed.astimezone(UTC)


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    parsed = Decimal(str(value))
    if not parsed.is_finite() or parsed < 0:
        raise ValueError("Authorized feed monetary values must be finite and non-negative")
    return parsed


def _currency(value: Any) -> str | None:
    if value in (None, ""):
        return None
    parsed = str(value).strip().upper()
    if re.fullmatch(r"[A-Z]{3}", parsed) is None:
        raise ValueError(f"Invalid ISO-style currency code: {value!r}")
    return parsed


def _status(value: str | None) -> ListingStatus:
    if not value:
        return ListingStatus.UNKNOWN
    for status in ListingStatus:
        if status.value.casefold() == value.casefold():
            return status
    return ListingStatus.UNKNOWN


class Chrono24AuthorizedFeedSource(SourceAdapter):
    """Reads a feed that the user has obtained permission or a license to use.

    It deliberately contains no Chrono24 HTTP scraping implementation.
    """

    name = "chrono24"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.config = settings.sources.chrono24

    def _feed_path(self) -> Path:
        if not self.config.enabled:
            raise SourceAccessError(
                self.name,
                "written_permission_required",
                "Chrono24 collection is disabled until written permission "
                "or a licensed feed exists",
            )
        if not self.config.access_authorized:
            raise SourceAccessError(
                self.name,
                "written_permission_required",
                "Set access_authorized only after obtaining Chrono24 permission",
            )
        if not self.config.authorized_feed_path:
            raise SourceAccessError(
                self.name,
                "feed_missing",
                "No authorized Chrono24 feed path is configured",
            )
        if not self.config.authorized_feed_path.exists():
            raise SourceAccessError(
                self.name,
                "feed_missing",
                f"Authorized feed does not exist: {self.config.authorized_feed_path}",
            )
        return self.config.authorized_feed_path

    def _records(self) -> list[dict[str, Any]]:
        path = self._feed_path()
        text = path.read_text(encoding="utf-8")
        if path.suffix.casefold() == ".jsonl":
            records: list[dict[str, Any]] = []
            for line_number, line in enumerate(text.splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                    if not isinstance(record, dict):
                        raise TypeError("Each authorized JSONL record must be an object")
                except Exception as error:
                    LOGGER.exception(
                        "authorized feed JSONL line rejected",
                        extra={"stage": "feed_json", "record_id": f"line-{line_number}"},
                    )
                    self.record_failure("feed_json", error, f"line-{line_number}")
                    continue
                records.append(record)
            return records
        payload = json.loads(text)
        if not isinstance(payload, list):
            raise ValueError("Authorized Chrono24 JSON feed must be a list or JSONL")
        return payload

    def _parse(self, record: dict[str, Any]) -> ListingCandidate | None:
        source_listing_id = str(record.get("source_listing_id") or "").strip()
        canonical_url = str(record.get("canonical_url") or "").strip()
        title = str(record.get("title") or "")
        if not source_listing_id:
            raise ValueError("Authorized feed record has no source_listing_id")
        if not canonical_url:
            raise ValueError("Authorized feed record has no canonical_url")
        if not title:
            raise ValueError("Authorized feed record has no title")
        brand_matches = identify_brands(
            f"{title} {record.get('brand', '')}", self.settings.target_brands
        )
        if not brand_matches:
            return None
        posted = _datetime(record.get("original_posted_at_utc"))
        if self.config.require_verified_posted_date and posted is None:
            return None
        current_status = _status(record.get("status"))
        sold_price = _decimal(record.get("sold_price_original"))
        return ListingCandidate(
            source=self.name,
            source_listing_id=source_listing_id,
            canonical_url=canonical_url,
            title=title,
            original_posted_at_utc=posted,
            date_evidence=record.get("date_evidence") or "Authorized feed timestamp",
            date_confidence=Confidence(record.get("date_confidence", "High")),
            current_status=current_status,
            status_evidence=record.get("status_evidence"),
            brand=brand_matches[0],
            model=record.get("model"),
            reference_number=record.get("reference_number"),
            approximate_year=record.get("approximate_year"),
            condition=record.get("condition"),
            condition_notes=record.get("condition_notes"),
            box_included=record.get("box_included"),
            papers_included=record.get("papers_included"),
            seller_name=record.get("seller_name"),
            seller_type=record.get("seller_type"),
            seller_location=record.get("seller_location"),
            seller_reputation_evidence=record.get("seller_reputation_evidence"),
            transaction_protection=record.get("transaction_protection"),
            asking_price_original=_decimal(record.get("asking_price_original")),
            currency=_currency(record.get("currency")),
            stated_shipping_cost=_decimal(record.get("stated_shipping_cost")),
            estimated_all_in_original=_decimal(record.get("estimated_all_in_original")),
            description_summary=record.get("description_summary"),
            risk_flags=list(record.get("risk_flags") or []),
            missing_information=list(record.get("missing_information") or []),
            questions_to_ask_seller=list(record.get("questions_to_ask_seller") or []),
            sold_at_utc=_datetime(record.get("sold_at_utc")),
            sold_price_original=sold_price,
            sold_price_currency=_currency(record.get("sold_price_currency")),
            sold_price_evidence=record.get("sold_price_evidence"),
            sold_price_confidence=(
                Confidence(record["sold_price_confidence"])
                if record.get("sold_price_confidence")
                else None
            ),
            raw_payload={
                "parser_version": PARSER_VERSION,
                "authorized_feed": True,
                "full_snapshot": True,
            },
        )

    def _candidates(self, stage: str) -> list[ListingCandidate]:
        candidates: list[ListingCandidate] = []
        for index, record in enumerate(self._records()):
            record_id = f"record-{index}"
            try:
                if not isinstance(record, dict):
                    raise TypeError("Each authorized JSON record must be an object")
                record_id = str(record.get("source_listing_id") or record_id)
                candidate = self._parse(record)
            except Exception as error:
                LOGGER.exception(
                    "authorized feed record rejected",
                    extra={"stage": stage, "record_id": record_id},
                )
                self.record_failure(stage, error, record_id)
                continue
            if candidate is not None:
                candidates.append(candidate)
        return candidates

    def discover(self, window_start: datetime, window_end: datetime) -> list[ListingCandidate]:
        candidates = self._candidates("discover_parse")
        return [
            candidate
            for candidate in candidates
            if candidate.original_posted_at_utc
            and window_start <= candidate.original_posted_at_utc <= window_end
        ]

    def refresh(self, source_ad_ids: list[str]) -> RefreshOutcome:
        wanted = set(source_ad_ids)
        candidates = [
            candidate
            for candidate in self._candidates("refresh_parse")
            if candidate.source_listing_id in wanted
        ]
        return RefreshOutcome(candidates=candidates, deleted_source_ad_ids=set())
