from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.orm import Session

from watch_tracker import __version__
from watch_tracker.database.models import (
    CollectionError,
    Comparable,
    DealScore,
    DuplicateGroup,
    ErasureEvent,
    Listing,
    ListingAlias,
    ListingObservation,
    ListingPriceHistory,
    ListingStatusHistory,
    Run,
    SourceRun,
    Valuation,
)
from watch_tracker.domain import ListingCandidate, ListingStatus, RunStatus
from watch_tracker.services.identity import canonicalize_url, content_hash, make_listing_uid


def utc_now() -> datetime:
    return datetime.now(UTC)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "value"):
        return value.value
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    return value


def candidate_payload(candidate: ListingCandidate) -> dict[str, Any]:
    payload = asdict(candidate)
    payload.pop("raw_payload", None)
    return _jsonable(payload)


@dataclass(slots=True)
class UpsertResult:
    listing: Listing
    created: bool = False
    updated: bool = False
    duplicate_prevented: bool = False
    sold_transition: bool = False
    price_changed: bool = False
    erasure_events: int = 0


def _safe_stage(stage: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.:-]+", "-", stage).strip("-")
    return normalized[:64] or "operation"


def _safe_error_message(error: Exception, stage: str) -> str:
    if isinstance(error, json.JSONDecodeError):
        return (
            f"JSONDecodeError during {_safe_stage(stage)} at "
            f"line {error.lineno}, column {error.colno}; source content suppressed"
        )
    return (
        f"{type(error).__name__} during {_safe_stage(stage)}; "
        "source content and parameters suppressed"
    )


def _redact_author_identifier(value: Any, identifier: str | None) -> Any:
    if not identifier:
        return value
    if isinstance(value, str):
        return re.sub(
            re.escape(identifier),
            "[deleted author]",
            value,
            flags=re.IGNORECASE,
        )
    if isinstance(value, list):
        return [_redact_author_identifier(item, identifier) for item in value]
    if isinstance(value, dict):
        return {key: _redact_author_identifier(item, identifier) for key, item in value.items()}
    return value


class Repository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_run(
        self,
        run_id: str,
        started_at: datetime,
        window_start: datetime,
        window_end: datetime,
        dry_run: bool,
    ) -> Run:
        run = Run(
            run_id=run_id,
            started_at_utc=started_at,
            discovery_window_start_utc=window_start,
            discovery_window_end_utc=window_end,
            status=RunStatus.RUNNING.value,
            dry_run=dry_run,
            code_version=__version__,
            summary={},
        )
        self.session.add(run)
        self.session.flush()
        return run

    def finish_run(
        self,
        run_id: str,
        status: RunStatus,
        counts: dict[str, int],
        summary: dict[str, Any],
    ) -> None:
        self.session.execute(
            update(Run)
            .where(Run.run_id == run_id)
            .values(
                completed_at_utc=utc_now(),
                status=status.value,
                new_records=counts.get("new_records", 0),
                updated_records=counts.get("updated_records", 0),
                duplicate_records_prevented=counts.get("duplicate_records_prevented", 0),
                sold_status_changes=counts.get("sold_status_changes", 0),
                price_changes=counts.get("price_changes", 0),
                errors=counts.get("errors", 0),
                summary=_jsonable(summary),
            )
        )

    def create_source_run(self, run_id: str, source: str) -> SourceRun:
        source_run = SourceRun(
            run_id=run_id,
            source=source,
            started_at_utc=utc_now(),
            status="Running",
        )
        self.session.add(source_run)
        self.session.flush()
        return source_run

    def finish_source_run(
        self,
        source_run_id: int,
        status: str,
        discovered: int,
        refreshed: int,
        errors: int,
        message: str | None = None,
    ) -> None:
        self.session.execute(
            update(SourceRun)
            .where(SourceRun.id == source_run_id)
            .values(
                completed_at_utc=utc_now(),
                status=status,
                discovered_count=discovered,
                refreshed_count=refreshed,
                error_count=errors,
                message=message,
            )
        )

    def record_error(
        self,
        run_id: str,
        source: str | None,
        stage: str,
        error: Exception,
        listing_uid: str | None = None,
        recoverable: bool = True,
        retry_count: int = 0,
    ) -> None:
        safe_stage = _safe_stage(stage)
        self.session.add(
            CollectionError(
                run_id=run_id,
                source=source,
                listing_uid=listing_uid,
                stage=safe_stage,
                error_type=type(error).__name__,
                message=_safe_error_message(error, safe_stage),
                occurred_at_utc=utc_now(),
                retry_count=retry_count,
                recoverable=recoverable,
            )
        )

    def queue_erasure_event(
        self,
        *,
        source: str,
        source_object_id: str,
        event_type: str,
        run_id: str,
        detected_at: datetime,
        reopen_completed: bool = True,
    ) -> tuple[ErasureEvent, bool]:
        event = self.session.scalar(
            select(ErasureEvent).where(
                ErasureEvent.source == source,
                ErasureEvent.source_object_id == source_object_id,
                ErasureEvent.event_type == event_type,
            )
        )
        if event is not None:
            if event.status == "Complete" and reopen_completed:
                event.detected_run_id = run_id
                event.detected_at_utc = detected_at
                event.status = "Pending"
                event.logical_purge_completed_at_utc = detected_at
                event.physical_scrub_completed_at_utc = None
                event.artifacts_regenerated_at_utc = None
                event.last_error = None
                return event, True
            if event.status != "Complete":
                event.status = "Pending"
                event.logical_purge_completed_at_utc = detected_at
                event.last_error = None
            return event, False
        event = ErasureEvent(
            source=source,
            source_object_id=source_object_id,
            event_type=event_type,
            detected_run_id=run_id,
            detected_at_utc=detected_at,
            status="Pending",
            logical_purge_completed_at_utc=detected_at,
        )
        self.session.add(event)
        self.session.flush()
        return event, True

    def pending_erasure_events(self) -> list[ErasureEvent]:
        return list(
            self.session.scalars(
                select(ErasureEvent)
                .where(ErasureEvent.status != "Complete")
                .order_by(ErasureEvent.detected_at_utc, ErasureEvent.id)
            )
        )

    def has_pending_erasures(self) -> bool:
        return (
            self.session.scalar(
                select(ErasureEvent.id)
                .where(ErasureEvent.status != "Complete")
                .order_by(ErasureEvent.id)
                .limit(1)
            )
            is not None
        )

    def mark_erasure_events_complete(
        self,
        event_ids: list[int],
        completed_at: datetime,
    ) -> None:
        if not event_ids:
            return
        self.session.execute(
            update(ErasureEvent)
            .where(
                ErasureEvent.id.in_(event_ids),
                ErasureEvent.status != "Complete",
            )
            .values(
                status="Complete",
                physical_scrub_completed_at_utc=completed_at,
                artifacts_regenerated_at_utc=completed_at,
                last_error=None,
            )
        )

    def mark_erasure_events_failed(
        self,
        event_ids: list[int],
        error: Exception | str,
    ) -> None:
        if not event_ids:
            return
        error_type = type(error).__name__ if isinstance(error, Exception) else "Error"
        safe_message = (
            f"{error_type} during erasure-processing; source content and parameters suppressed"
        )
        self.session.execute(
            update(ErasureEvent)
            .where(
                ErasureEvent.id.in_(event_ids),
                ErasureEvent.status != "Complete",
            )
            .values(status="Pending", last_error=safe_message)
        )

    def _resolve_listing(
        self,
        source: str,
        source_listing_id: str | None,
        canonical_url: str,
    ) -> Listing | None:
        if source_listing_id:
            listing = self.session.scalar(
                select(Listing).where(
                    Listing.source == source,
                    Listing.source_listing_id == source_listing_id,
                )
            )
            if listing:
                return listing
            alias = self.session.scalar(
                select(ListingAlias).where(
                    ListingAlias.source == source,
                    ListingAlias.alias_type == "source_listing_id",
                    ListingAlias.alias_value == source_listing_id,
                )
            )
            if alias:
                return self.session.get(Listing, alias.listing_id)
            # A source-provided identifier is authoritative. Multi-item parent
            # posts may legitimately give several offers the same canonical
            # parent URL, so URL fallback here would collapse distinct children.
            return None
        return self.session.scalar(
            select(Listing).where(
                Listing.source == source,
                Listing.canonical_url == canonical_url,
            )
        )

    def _assign_conservative_duplicate_group(
        self,
        listing: Listing,
        observed_at: datetime,
    ) -> None:
        if listing.duplicate_group_id:
            return
        seller_type = (listing.seller_type or "").strip().casefold()
        if seller_type not in {"private", "individual", "collector"}:
            return
        seller = " ".join((listing.seller_name or "").split()).casefold()
        brand = (listing.brand or "").strip().casefold()
        watch_key = (listing.reference_number or listing.model or "").strip().casefold()
        currency = (listing.asking_price_currency or "").upper()
        price = listing.latest_asking_price_original
        if not all((seller, brand, watch_key, currency)) or price is None:
            return

        statement = select(Listing).where(
            Listing.id != listing.id,
            Listing.source != listing.source,
            Listing.brand == listing.brand,
            Listing.latest_asking_price_original == price,
            Listing.asking_price_currency == currency,
            Listing.seller_name.is_not(None),
        )
        if listing.reference_number:
            statement = statement.where(Listing.reference_number == listing.reference_number)
        else:
            statement = statement.where(
                Listing.reference_number.is_(None),
                Listing.model == listing.model,
            )
        listing_time = listing.original_posted_at_utc or listing.first_seen_at_utc
        listing_time = (
            listing_time.replace(tzinfo=UTC)
            if listing_time.tzinfo is None
            else listing_time.astimezone(UTC)
        )
        matches: list[Listing] = []
        for match in self.session.scalars(statement):
            if (match.seller_type or "").strip().casefold() not in {
                "private",
                "individual",
                "collector",
            }:
                continue
            if " ".join((match.seller_name or "").split()).casefold() != seller:
                continue
            match_time = match.original_posted_at_utc or match.first_seen_at_utc
            match_time = (
                match_time.replace(tzinfo=UTC)
                if match_time.tzinfo is None
                else match_time.astimezone(UTC)
            )
            if abs(listing_time - match_time).total_seconds() <= 72 * 60 * 60:
                matches.append(match)
        if not matches:
            return

        existing_groups = {
            match.duplicate_group_id for match in matches if match.duplicate_group_id
        }
        if len(existing_groups) > 1:
            return
        group_id = next(iter(existing_groups), None)
        if group_id is None:
            # Listing identities make separate sale cohorts distinct even when
            # the same private seller later advertises the same reference at
            # the same price.
            cohort = "|".join(
                sorted([listing.listing_uid, *(match.listing_uid for match in matches)])
            )
            group_id = f"crosspost:{content_hash(cohort)[:54]}"
        group = self.session.get(DuplicateGroup, group_id)
        if group is None:
            group = DuplicateGroup(
                duplicate_group_id=group_id,
                confidence="High",
                rationale=(
                    "Exact non-dealer seller, watch, and asking-price match "
                    "across sources within 72 hours"
                ),
                created_at_utc=observed_at,
            )
            self.session.add(group)
        listing.duplicate_group_id = group_id
        for match in matches:
            match.duplicate_group_id = group_id

    def upsert_candidate(
        self,
        candidate: ListingCandidate,
        run_id: str,
        observed_at: datetime,
        fetch_method: str,
    ) -> UpsertResult:
        canonical_url = canonicalize_url(candidate.source, candidate.canonical_url)
        listing = self._resolve_listing(
            candidate.source, candidate.source_listing_id, canonical_url
        )
        created = listing is None
        source_ad_id = str(
            candidate.raw_payload.get("source_ad_id")
            or (candidate.source_listing_id or "").split("#", 1)[0]
            or candidate.source_listing_id
            or ""
        )
        author_erasure_requested = bool(
            candidate.source == "reddit" and candidate.raw_payload.get("author_deleted")
        )
        stored_author_identifier = listing.seller_name if listing is not None else None
        had_stored_author_data = bool(
            listing is not None
            and (
                listing.seller_name or listing.seller_location or listing.seller_reputation_evidence
            )
        )
        erasure_events = 0
        if listing is None:
            listing_uid = make_listing_uid(
                candidate.source,
                candidate.source_listing_id,
                canonical_url,
            )
            listing = Listing(
                listing_uid=listing_uid,
                source=candidate.source,
                source_listing_id=candidate.source_listing_id,
                source_ad_id=source_ad_id or None,
                canonical_url=canonical_url,
                first_seen_at_utc=observed_at,
                last_seen_at_utc=observed_at,
                last_checked_at_utc=observed_at,
                status_checked_at_utc=observed_at,
                date_confidence=candidate.date_confidence.value,
                current_status=candidate.current_status.value,
                is_sold=candidate.current_status == ListingStatus.SOLD,
                title=candidate.title,
            )
            self.session.add(listing)
            self.session.flush()
        else:
            listing.last_seen_at_utc = observed_at
            listing.last_checked_at_utc = observed_at
            if candidate.source_listing_id and not listing.source_listing_id:
                self.session.add(
                    ListingAlias(
                        listing_id=listing.id,
                        source=candidate.source,
                        alias_type="source_listing_id",
                        alias_value=candidate.source_listing_id,
                        created_at_utc=observed_at,
                    )
                )

        old_status = listing.current_status
        old_price = listing.latest_asking_price_original
        old_currency = listing.asking_price_currency
        old_all_in = listing.estimated_all_in_original
        old_sold_price = listing.sold_price_original
        old_sold_currency = listing.sold_price_currency

        always_observed = {
            "source_listing_id": listing.source_listing_id or candidate.source_listing_id,
            "source_ad_id": listing.source_ad_id or source_ad_id or None,
            "canonical_url": canonical_url,
            "status_checked_at_utc": observed_at,
        }
        for field, value in always_observed.items():
            setattr(listing, field, value)

        incoming_status = candidate.current_status.value
        if not created and candidate.current_status == ListingStatus.UNKNOWN:
            incoming_status = old_status
        listing.current_status = incoming_status
        if candidate.status_evidence is not None:
            listing.status_evidence = candidate.status_evidence
        if candidate.title:
            listing.title = candidate.title
        if candidate.original_posted_at_utc is not None:
            listing.original_posted_at_utc = candidate.original_posted_at_utc
            listing.date_evidence = candidate.date_evidence
            listing.date_confidence = candidate.date_confidence.value

        observed_values = {
            "brand": candidate.brand,
            "model": candidate.model,
            "reference_number": candidate.reference_number,
            "approximate_year": candidate.approximate_year,
            "case_material": candidate.case_material,
            "case_size_mm": candidate.case_size_mm,
            "dial_description": candidate.dial_description,
            "movement_or_caliber": candidate.movement_or_caliber,
            "condition": candidate.condition,
            "condition_notes": candidate.condition_notes,
            "box_included": candidate.box_included,
            "papers_included": candidate.papers_included,
            "service_history": candidate.service_history,
            "seller_name": candidate.seller_name,
            "seller_type": candidate.seller_type,
            "seller_location": candidate.seller_location,
            "seller_reputation_evidence": candidate.seller_reputation_evidence,
            "transaction_protection": candidate.transaction_protection,
            "return_policy": candidate.return_policy,
            "latest_asking_price_original": candidate.asking_price_original,
            "asking_price_currency": candidate.currency,
            "latest_asking_price_usd": candidate.asking_price_usd,
            "stated_shipping_cost": candidate.stated_shipping_cost,
            "estimated_all_in_original": candidate.estimated_all_in_original,
            "estimated_all_in_usd": candidate.estimated_all_in_usd,
            "negotiable": candidate.negotiable,
            "description_summary": candidate.description_summary,
            "authenticity_notes": candidate.authenticity_notes,
            "image_quality_notes": candidate.image_quality_notes,
        }
        for field, value in observed_values.items():
            if value is not None:
                setattr(listing, field, value)

        if created or candidate.raw_payload.get("full_snapshot"):
            listing.complications = candidate.complications
            listing.accessories = candidate.accessories
            listing.risk_flags = candidate.risk_flags
            listing.missing_information = candidate.missing_information
            listing.questions_to_ask_seller = candidate.questions_to_ask_seller
        if author_erasure_requested:
            listing.seller_name = None
            listing.seller_type = None
            listing.seller_location = None
            listing.seller_reputation_evidence = None
            listing.title = "[Reddit listing; author deleted]"
            listing.status_evidence = "Reddit author deletion observed; author data purged"
            for field in (
                "condition_notes",
                "service_history",
                "description_summary",
                "authenticity_notes",
                "image_quality_notes",
            ):
                setattr(listing, field, None)
            for field in (
                "date_evidence",
                "sold_price_evidence",
                "dial_description",
                "model",
                "movement_or_caliber",
                "condition",
                "return_policy",
                "transaction_protection",
            ):
                setattr(
                    listing,
                    field,
                    _redact_author_identifier(
                        getattr(listing, field),
                        stored_author_identifier,
                    ),
                )
            for field in (
                "complications",
                "accessories",
                "risk_flags",
                "missing_information",
                "questions_to_ask_seller",
            ):
                setattr(
                    listing,
                    field,
                    _redact_author_identifier(
                        getattr(listing, field),
                        stored_author_identifier,
                    ),
                )
            if had_stored_author_data and source_ad_id:
                self.session.execute(
                    delete(ListingObservation).where(ListingObservation.listing_id == listing.id)
                )
                self.session.execute(
                    update(ListingStatusHistory)
                    .where(ListingStatusHistory.listing_id == listing.id)
                    .values(evidence=None)
                )
                self.session.execute(
                    update(ListingPriceHistory)
                    .where(ListingPriceHistory.listing_id == listing.id)
                    .values(evidence=None)
                )
                self.session.execute(
                    update(Comparable)
                    .where(
                        or_(
                            Comparable.evidence_listing_id == listing.id,
                            Comparable.source_url == listing.canonical_url,
                        )
                    )
                    .values(evidence=None)
                )
                self.session.execute(delete(DealScore).where(DealScore.listing_id == listing.id))
                self.session.execute(
                    delete(CollectionError).where(
                        CollectionError.listing_uid == listing.listing_uid
                    )
                )
                _, queued = self.queue_erasure_event(
                    source="reddit",
                    source_object_id=source_ad_id,
                    event_type="reddit_author_erasure",
                    run_id=run_id,
                    detected_at=observed_at,
                    reopen_completed=True,
                )
                erasure_events += int(queued)

        if (
            listing.initial_asking_price_original is None
            and candidate.asking_price_original is not None
        ):
            listing.initial_asking_price_original = candidate.asking_price_original
            listing.initial_asking_price_usd = candidate.asking_price_usd
        elif (
            listing.initial_asking_price_usd is None
            and candidate.asking_price_original == listing.initial_asking_price_original
            and candidate.asking_price_usd is not None
        ):
            listing.initial_asking_price_usd = candidate.asking_price_usd

        if incoming_status == ListingStatus.SOLD.value:
            listing.is_sold = True
            if listing.first_observed_sold_at_utc is None:
                listing.first_observed_sold_at_utc = observed_at
            listing.sold_at_utc = candidate.sold_at_utc or listing.sold_at_utc
            if candidate.sold_price_original is not None:
                listing.sold_price_original = candidate.sold_price_original
                listing.sold_price_currency = candidate.sold_price_currency
                listing.sold_price_usd = candidate.sold_price_usd
                listing.sold_price_evidence = candidate.sold_price_evidence
                listing.sold_price_confidence = (
                    candidate.sold_price_confidence.value
                    if candidate.sold_price_confidence
                    else None
                )
        if author_erasure_requested:
            listing.sold_price_evidence = _redact_author_identifier(
                listing.sold_price_evidence,
                stored_author_identifier,
            )

        status_changed = created or old_status != incoming_status
        sold_transition = (
            not created
            and old_status != ListingStatus.SOLD.value
            and incoming_status == ListingStatus.SOLD.value
        )
        asking_price_changed = (
            not created
            and candidate.asking_price_original is not None
            and (old_price != candidate.asking_price_original or old_currency != candidate.currency)
        )
        all_in_changed = (
            not created
            and candidate.estimated_all_in_original is not None
            and (
                old_all_in != candidate.estimated_all_in_original
                or old_currency != candidate.currency
            )
        )
        price_changed = asking_price_changed or all_in_changed
        price_observed = created and candidate.asking_price_original is not None
        sold_price_changed = candidate.sold_price_original is not None and (
            old_sold_price != candidate.sold_price_original
            or old_sold_currency != candidate.sold_price_currency
        )

        if status_changed:
            self.session.add(
                ListingStatusHistory(
                    listing_id=listing.id,
                    run_id=run_id,
                    observed_at_utc=observed_at,
                    old_status=None if created else old_status,
                    new_status=incoming_status,
                    evidence=(
                        listing.status_evidence
                        if author_erasure_requested
                        else candidate.status_evidence
                    ),
                )
            )
        if price_changed or price_observed:
            listing.price_last_changed_at_utc = observed_at
        if asking_price_changed or price_observed:
            self.session.add(
                ListingPriceHistory(
                    listing_id=listing.id,
                    run_id=run_id,
                    observed_at_utc=observed_at,
                    price_type="asking",
                    amount_original=candidate.asking_price_original,
                    currency=candidate.currency or "UNK",
                    amount_usd=candidate.asking_price_usd,
                    evidence=candidate.canonical_url,
                )
            )
        all_in_observed = (
            candidate.estimated_all_in_original is not None
            and candidate.estimated_all_in_original != candidate.asking_price_original
            and (created or all_in_changed)
        )
        if all_in_observed:
            self.session.add(
                ListingPriceHistory(
                    listing_id=listing.id,
                    run_id=run_id,
                    observed_at_utc=observed_at,
                    price_type="all_in",
                    amount_original=candidate.estimated_all_in_original,
                    currency=candidate.currency or "UNK",
                    amount_usd=candidate.estimated_all_in_usd,
                    evidence="Asking price plus disclosed shipping/all-in costs",
                )
            )
        if sold_price_changed:
            self.session.add(
                ListingPriceHistory(
                    listing_id=listing.id,
                    run_id=run_id,
                    observed_at_utc=observed_at,
                    price_type="confirmed_sale",
                    amount_original=candidate.sold_price_original,
                    currency=candidate.sold_price_currency or candidate.currency or "UNK",
                    amount_usd=candidate.sold_price_usd,
                    evidence=(
                        listing.sold_price_evidence
                        if author_erasure_requested
                        else candidate.sold_price_evidence
                    ),
                )
            )

        self._assign_conservative_duplicate_group(listing, observed_at)
        payload = candidate_payload(candidate)
        if author_erasure_requested:
            for field in (
                "seller_name",
                "seller_type",
                "seller_location",
                "seller_reputation_evidence",
            ):
                payload[field] = None
            payload.update(
                {
                    "title": "[Reddit listing; author deleted]",
                    "status_evidence": ("Reddit author deletion observed; author data purged"),
                    "condition_notes": None,
                    "service_history": None,
                    "description_summary": None,
                    "authenticity_notes": None,
                    "image_quality_notes": None,
                }
            )
            payload = _redact_author_identifier(payload, stored_author_identifier)
        serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        observation_hash = content_hash(serialized)
        existing_observation = self.session.scalar(
            select(ListingObservation.id).where(
                ListingObservation.run_id == run_id,
                ListingObservation.listing_id == listing.id,
                ListingObservation.content_hash == observation_hash,
            )
        )
        if existing_observation is None:
            self.session.add(
                ListingObservation(
                    listing_id=listing.id,
                    run_id=run_id,
                    observed_at_utc=observed_at,
                    source_url=canonical_url,
                    content_hash=observation_hash,
                    parser_version=str(candidate.raw_payload.get("parser_version", "unknown")),
                    fetch_method=fetch_method,
                    retrieval_outcome="success",
                    parsed_fields=payload,
                    evidence_excerpt=(
                        listing.status_evidence
                        if author_erasure_requested
                        else candidate.status_evidence
                    ),
                )
            )

        return UpsertResult(
            listing=listing,
            created=created,
            updated=not created and (status_changed or price_changed or sold_price_changed),
            duplicate_prevented=not created,
            sold_transition=sold_transition,
            price_changed=price_changed,
            erasure_events=erasure_events,
        )

    def listings_for_refresh(
        self,
        source: str,
        include_all: bool = False,
        sold_recheck_cutoff: datetime | None = None,
    ) -> list[Listing]:
        statement = select(Listing).where(Listing.source == source)
        refreshable = Listing.current_status.in_(
            [
                ListingStatus.ACTIVE.value,
                ListingStatus.PENDING.value,
                ListingStatus.RESERVED.value,
                ListingStatus.UNKNOWN.value,
                ListingStatus.UNAVAILABLE.value,
            ]
        )
        if include_all:
            statement = statement.where(Listing.current_status != ListingStatus.REMOVED.value)
        elif sold_recheck_cutoff is not None:
            statement = statement.where(
                or_(
                    refreshable,
                    and_(
                        Listing.current_status == ListingStatus.SOLD.value,
                        Listing.sold_price_original.is_(None),
                        Listing.first_observed_sold_at_utc.is_not(None),
                        Listing.first_observed_sold_at_utc >= sold_recheck_cutoff,
                    ),
                )
            )
        else:
            statement = statement.where(refreshable)
        return list(self.session.scalars(statement.order_by(Listing.id)))

    def reconcile_missing_source_children(
        self,
        source: str,
        returned_source_ad_ids: set[str],
        current_source_listing_ids: set[str],
        run_id: str,
        observed_at: datetime,
    ) -> int:
        if not returned_source_ad_ids:
            return 0
        missing_identifier = Listing.source_listing_id.is_(None)
        if current_source_listing_ids:
            missing_identifier = or_(
                missing_identifier,
                Listing.source_listing_id.not_in(current_source_listing_ids),
            )
        candidates = list(
            self.session.scalars(
                select(Listing).where(
                    Listing.source == source,
                    Listing.source_ad_id.in_(returned_source_ad_ids),
                    missing_identifier,
                    Listing.current_status.in_(
                        [
                            ListingStatus.ACTIVE.value,
                            ListingStatus.PENDING.value,
                            ListingStatus.RESERVED.value,
                            ListingStatus.UNKNOWN.value,
                        ]
                    ),
                )
            )
        )
        for listing in candidates:
            old_status = listing.current_status
            listing.current_status = ListingStatus.UNAVAILABLE.value
            listing.is_sold = False
            listing.status_evidence = (
                "Authorized source refresh returned the parent record without "
                "this previously stored child offer"
            )
            listing.status_checked_at_utc = observed_at
            listing.last_checked_at_utc = observed_at
            self.session.add(
                ListingStatusHistory(
                    listing_id=listing.id,
                    run_id=run_id,
                    observed_at_utc=observed_at,
                    old_status=old_status,
                    new_status=ListingStatus.UNAVAILABLE.value,
                    evidence=listing.status_evidence,
                )
            )
        return len(candidates)

    def _invalidate_listing_evidence(
        self,
        listing_id: int,
        legacy_source_url: str,
    ) -> None:
        evidence_filter = or_(
            Comparable.listing_id == listing_id,
            Comparable.evidence_listing_id == listing_id,
            Comparable.source_url == legacy_source_url,
        )
        affected_pairs = {
            (target_listing_id, affected_run_id)
            for target_listing_id, affected_run_id in self.session.execute(
                select(Comparable.listing_id, Comparable.run_id).where(evidence_filter)
            )
        }
        affected_pairs.update(
            self.session.execute(
                select(Valuation.listing_id, Valuation.run_id).where(
                    Valuation.listing_id == listing_id
                )
            )
        )
        for target_listing_id, affected_run_id in affected_pairs:
            valuation_ids = list(
                self.session.scalars(
                    select(Valuation.id).where(
                        Valuation.listing_id == target_listing_id,
                        Valuation.run_id == affected_run_id,
                    )
                )
            )
            score_filter = and_(
                DealScore.listing_id == target_listing_id,
                DealScore.run_id == affected_run_id,
            )
            if valuation_ids:
                score_filter = or_(
                    score_filter,
                    DealScore.valuation_id.in_(valuation_ids),
                )
            self.session.execute(delete(DealScore).where(score_filter))
            self.session.execute(
                delete(Comparable).where(
                    Comparable.listing_id == target_listing_id,
                    Comparable.run_id == affected_run_id,
                )
            )
            self.session.execute(
                delete(Valuation).where(
                    Valuation.listing_id == target_listing_id,
                    Valuation.run_id == affected_run_id,
                )
            )
        self.session.execute(
            delete(Comparable).where(
                or_(
                    Comparable.listing_id == listing_id,
                    Comparable.evidence_listing_id == listing_id,
                    Comparable.source_url == legacy_source_url,
                )
            )
        )
        self.session.execute(delete(DealScore).where(DealScore.listing_id == listing_id))
        self.session.execute(delete(Valuation).where(Valuation.listing_id == listing_id))

    def purge_deleted_reddit_ad(
        self,
        source_ad_id: str,
        run_id: str,
        observed_at: datetime,
    ) -> int:
        listings = list(
            self.session.scalars(
                select(Listing).where(
                    Listing.source == "reddit",
                    Listing.source_ad_id == source_ad_id,
                )
            )
        )
        if not listings:
            return 0
        contains_sensitive_state = any(
            not (
                listing.current_status == ListingStatus.REMOVED.value
                and listing.title == "[purged deleted Reddit content]"
                and listing.latest_asking_price_original is None
                and listing.seller_name is None
            )
            for listing in listings
        )
        self.queue_erasure_event(
            source="reddit",
            source_object_id=source_ad_id,
            event_type="reddit_content_deletion",
            run_id=run_id,
            detected_at=observed_at,
            reopen_completed=contains_sensitive_state,
        )
        purged_count = 0
        for listing in listings:
            old_status = listing.current_status
            original_url = listing.canonical_url
            already_purged = (
                old_status == ListingStatus.REMOVED.value
                and listing.title == "[purged deleted Reddit content]"
                and listing.latest_asking_price_original is None
                and listing.seller_name is None
            )
            if not already_purged:
                purged_count += 1
            listing.current_status = ListingStatus.REMOVED.value
            listing.is_sold = False
            listing.status_evidence = "Reddit content deletion/removal observed; content purged"
            listing.status_checked_at_utc = observed_at
            listing.last_checked_at_utc = observed_at
            listing.title = "[purged deleted Reddit content]"
            listing.canonical_url = f"reddit-purged://{source_ad_id}/{listing.id}"
            listing.source_listing_id = f"{source_ad_id}#purged:{listing.id}"
            listing.duplicate_group_id = None
            listing.original_posted_at_utc = None
            listing.date_evidence = None
            listing.date_confidence = "Low"
            listing.sold_at_utc = None
            listing.first_observed_sold_at_utc = None
            listing.price_last_changed_at_utc = None
            for attribute in (
                "brand",
                "model",
                "reference_number",
                "case_material",
                "dial_description",
                "movement_or_caliber",
                "condition",
                "condition_notes",
                "service_history",
                "seller_name",
                "seller_type",
                "seller_location",
                "seller_reputation_evidence",
                "transaction_protection",
                "return_policy",
                "asking_price_currency",
                "sold_price_currency",
                "sold_price_evidence",
                "sold_price_confidence",
                "description_summary",
                "authenticity_notes",
                "image_quality_notes",
            ):
                setattr(listing, attribute, None)
            for attribute in (
                "approximate_year",
                "case_size_mm",
                "initial_asking_price_original",
                "latest_asking_price_original",
                "initial_asking_price_usd",
                "latest_asking_price_usd",
                "stated_shipping_cost",
                "estimated_all_in_original",
                "estimated_all_in_usd",
                "sold_price_original",
                "sold_price_usd",
            ):
                setattr(listing, attribute, None)
            for attribute in (
                "box_included",
                "papers_included",
                "negotiable",
            ):
                setattr(listing, attribute, None)
            listing.complications = []
            listing.accessories = []
            listing.risk_flags = []
            listing.missing_information = []
            listing.questions_to_ask_seller = []
            self._invalidate_listing_evidence(listing.id, original_url)
            self.session.execute(
                delete(ListingObservation).where(ListingObservation.listing_id == listing.id)
            )
            self.session.execute(
                delete(ListingPriceHistory).where(ListingPriceHistory.listing_id == listing.id)
            )
            self.session.execute(delete(ListingAlias).where(ListingAlias.listing_id == listing.id))
            self.session.execute(
                delete(CollectionError).where(CollectionError.listing_uid == listing.listing_uid)
            )
            self.session.execute(
                delete(ListingStatusHistory).where(ListingStatusHistory.listing_id == listing.id)
            )
            self.session.add(
                ListingStatusHistory(
                    listing_id=listing.id,
                    run_id=run_id,
                    observed_at_utc=observed_at,
                    old_status=old_status,
                    new_status=ListingStatus.REMOVED.value,
                    evidence=None,
                )
            )
        return purged_count

    def latest_successful_run(self) -> Run | None:
        return self.session.scalar(
            select(Run)
            .where(Run.status == RunStatus.SUCCESS.value)
            .order_by(Run.completed_at_utc.desc())
            .limit(1)
        )

    def counts(self) -> dict[str, int]:
        return {
            "listings": self.session.scalar(select(func.count(Listing.id))) or 0,
            "active": self.session.scalar(
                select(func.count(Listing.id)).where(
                    Listing.current_status == ListingStatus.ACTIVE.value
                )
            )
            or 0,
            "sold": self.session.scalar(
                select(func.count(Listing.id)).where(Listing.is_sold.is_(True))
            )
            or 0,
        }
