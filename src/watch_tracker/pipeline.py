from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from filelock import FileLock, Timeout
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from watch_tracker.config import Settings
from watch_tracker.database.backup import create_backup, purge_backup_artifacts
from watch_tracker.database.models import Listing, Valuation
from watch_tracker.database.repository import Repository
from watch_tracker.database.secure_erasure import secure_erase_database
from watch_tracker.domain import ListingCandidate, ListingStatus, RunStatus
from watch_tracker.services.exports import ExportService, purge_export_artifacts
from watch_tracker.services.fx import CurrencyConverter
from watch_tracker.services.readiness import source_readiness_fingerprint
from watch_tracker.services.scoring import ScoringService
from watch_tracker.services.valuation import ValuationService
from watch_tracker.sources.base import SourceAccessError, SourceAdapter
from watch_tracker.sources.chrono24 import Chrono24AuthorizedFeedSource
from watch_tracker.sources.reddit import RedditOAuthSource

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class PipelineResult:
    run_id: str
    status: str
    window_start: datetime
    window_end: datetime
    counts: dict[str, int]
    source_statuses: dict[str, str]
    exports: dict[str, Path] = field(default_factory=dict)
    messages: list[str] = field(default_factory=list)


def build_sources(settings: Settings) -> list[SourceAdapter]:
    sources: list[SourceAdapter] = []
    if settings.sources.reddit.enabled:
        sources.append(RedditOAuthSource(settings))
    if settings.sources.chrono24.enabled:
        sources.append(Chrono24AuthorizedFeedSource(settings))
    return sources


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _prepare_candidate(
    candidate: ListingCandidate,
    converter: CurrencyConverter,
    observed_at: datetime,
) -> None:
    candidate.asking_price_usd = converter.convert(
        candidate.asking_price_original,
        candidate.currency,
        observed_at,
    )
    if candidate.estimated_all_in_original is None and candidate.asking_price_original is not None:
        shipping = candidate.stated_shipping_cost or 0
        candidate.estimated_all_in_original = candidate.asking_price_original + shipping
    candidate.estimated_all_in_usd = converter.convert(
        candidate.estimated_all_in_original,
        candidate.currency,
        observed_at,
    )
    candidate.sold_price_usd = converter.convert(
        candidate.sold_price_original,
        candidate.sold_price_currency,
        candidate.sold_at_utc or observed_at,
    )


class DailyPipeline:
    def __init__(
        self,
        settings: Settings,
        factory: sessionmaker,
        sources: list[SourceAdapter] | None = None,
    ) -> None:
        self.settings = settings
        self.factory = factory
        self.sources = build_sources(settings) if sources is None else sources

    def run(
        self,
        *,
        as_of: datetime | None = None,
        dry_run: bool = False,
        source_filter: str | None = None,
        discover: bool = True,
        refresh: bool = True,
        analyze: bool = True,
        export: bool = True,
        mode: str = "daily",
    ) -> PipelineResult:
        source_names = {source.name for source in self.sources}
        if source_filter and source_filter not in source_names:
            choices = ", ".join(sorted(source_names))
            raise ValueError(f"Unknown source {source_filter!r}; expected one of: {choices}")
        if source_filter and mode == "daily":
            mode = "source_diagnostic"
        if source_filter:
            # A one-source diagnostic is intentionally not an authoritative
            # market snapshot when other configured sources may be omitted.
            analyze = False
            export = False

        as_of = _as_utc(as_of or datetime.now(UTC))
        window_start = as_of - timedelta(hours=self.settings.application.discovery_window_hours)
        run_id = str(uuid.uuid4())
        counts = {
            "new_records": 0,
            "updated_records": 0,
            "duplicate_records_prevented": 0,
            "sold_status_changes": 0,
            "price_changes": 0,
            "compliance_purges": 0,
            "errors": 0,
        }
        result = PipelineResult(
            run_id=run_id,
            status=RunStatus.RUNNING.value,
            window_start=window_start,
            window_end=as_of,
            counts=counts,
            source_statuses={},
        )

        lock = FileLock(str(self.settings.paths.lock))
        try:
            lock.acquire(timeout=0)
            os.chmod(self.settings.paths.lock, 0o600)
        except Timeout as error:
            self._close_sources()
            raise RuntimeError("Another watch-tracker run already holds the lock") from error
        try:
            if dry_run:
                return self._dry_run(result, source_filter)
            return self._persistent_run(
                result,
                source_filter,
                discover=discover,
                refresh=refresh,
                analyze=analyze,
                export=export,
                mode=mode,
            )
        finally:
            lock.release()
            self._close_sources()

    def _close_sources(self) -> None:
        for source in self.sources:
            with suppress(Exception):
                source.close()

    def _dry_run(
        self,
        result: PipelineResult,
        source_filter: str | None,
    ) -> PipelineResult:
        for source in self.sources:
            if source_filter and source.name != source_filter:
                continue
            try:
                candidates = source.discover(result.window_start, result.window_end)
                result.counts["new_records"] += len(candidates)
                failures = source.drain_record_failures()
                for failure in failures:
                    result.counts["errors"] += 1
                    result.messages.append(
                        f"{source.name} {failure.record_id or 'unknown record'} "
                        f"{failure.stage}: {failure.error}"
                    )
                result.source_statuses[source.name] = (
                    "DryRunPartial" if failures else "DryRunSuccess"
                )
            except SourceAccessError as error:
                result.source_statuses[source.name] = "PermissionRequired"
                result.messages.append(f"{source.name}: {error}")
            except Exception as error:
                result.counts["errors"] += 1
                result.source_statuses[source.name] = "Failed"
                result.messages.append(f"{source.name}: {error}")
        result.status = RunStatus.DRY_RUN.value
        return result

    def _persistent_run(
        self,
        result: PipelineResult,
        source_filter: str | None,
        *,
        discover: bool,
        refresh: bool,
        analyze: bool,
        export: bool,
        mode: str,
    ) -> PipelineResult:
        with self.factory() as session:
            repository = Repository(session)
            repository.create_run(
                result.run_id,
                result.window_end,
                result.window_start,
                result.window_end,
                False,
            )
            session.commit()

        try:
            processed_pending_erasure = self._process_pending_erasures(result)
            if not processed_pending_erasure:
                create_backup(
                    self.settings.paths.database,
                    self.settings.paths.backups,
                    self.settings.retention.daily_backups,
                    label="daily",
                )
            return self._execute_persistent_run(
                result,
                source_filter,
                discover=discover,
                refresh=refresh,
                analyze=analyze,
                export=export,
                mode=mode,
            )
        except Exception as error:
            LOGGER.exception("pipeline failed", extra={"run_id": result.run_id})
            result.counts["errors"] += 1
            result.status = RunStatus.FAILED.value
            result.messages.append(f"pipeline: {error}")
            try:
                with self.factory() as session:
                    repository = Repository(session)
                    repository.record_error(
                        result.run_id,
                        None,
                        "pipeline",
                        error,
                        recoverable=False,
                    )
                    repository.finish_run(
                        result.run_id,
                        RunStatus.FAILED,
                        result.counts,
                        self._summary(result, mode),
                    )
                    session.commit()
            except Exception:
                LOGGER.exception(
                    "failed to finalize failed pipeline run",
                    extra={"run_id": result.run_id},
                )
                raise
            return result

    def _execute_persistent_run(
        self,
        result: PipelineResult,
        source_filter: str | None,
        *,
        discover: bool,
        refresh: bool,
        analyze: bool,
        export: bool,
        mode: str,
    ) -> PipelineResult:
        successful_sources = 0
        permission_sources = 0
        failed_sources = 0
        collection_requested = discover or refresh
        selected_sources = [
            source for source in self.sources if not source_filter or source.name == source_filter
        ]

        if collection_requested and not selected_sources:
            failed_sources = 1
            result.counts["errors"] += 1
            result.messages.append("No enabled source is available for collection")

        for source in selected_sources if collection_requested else []:
            with self.factory() as session:
                source_run = Repository(session).create_source_run(result.run_id, source.name)
                session.commit()
                source_run_id = source_run.id

            discovered_count = 0
            refreshed_count = 0
            source_errors = 0
            try:
                if discover:
                    candidates = source.discover(result.window_start, result.window_end)
                    source_errors += self._record_source_failures(source, result)
                    discovered_count, ingest_errors = self._ingest_candidates(
                        source,
                        candidates,
                        result,
                        stage="discover_record",
                    )
                    source_errors += ingest_errors

                if refresh:
                    with self.factory() as session:
                        sold_cutoff = result.window_end - timedelta(
                            days=self.settings.application.sold_price_recheck_days
                        )
                        existing = Repository(session).listings_for_refresh(
                            source.name,
                            include_all=source.name == "reddit",
                            sold_recheck_cutoff=(
                                sold_cutoff if source.name == "chrono24" else None
                            ),
                        )
                        source_ids = sorted(
                            {
                                listing.source_ad_id or listing.source_listing_id
                                for listing in existing
                                if listing.source_ad_id or listing.source_listing_id
                            }
                        )
                    refresh_outcome = source.refresh(source_ids) if source_ids else None
                    source_errors += self._record_source_failures(source, result)
                    if refresh_outcome:
                        refreshed_count, refresh_errors = self._ingest_candidates(
                            source,
                            refresh_outcome.candidates,
                            result,
                            stage="refresh_record",
                        )
                        source_errors += refresh_errors
                        purge_errors = self._apply_reddit_deletions(
                            source,
                            refresh_outcome.deleted_source_ad_ids,
                            result,
                        )
                        source_errors += purge_errors
                        if refresh_outcome.returned_source_ad_ids:
                            with self.factory() as session:
                                Repository(session).reconcile_missing_source_children(
                                    source.name,
                                    refresh_outcome.returned_source_ad_ids,
                                    refresh_outcome.current_source_listing_ids,
                                    result.run_id,
                                    result.window_end,
                                )
                                session.commit()

                source_status = "Partial" if source_errors else "Successful"
                with self.factory() as session:
                    Repository(session).finish_source_run(
                        source_run_id,
                        source_status,
                        discovered_count,
                        refreshed_count,
                        source_errors,
                    )
                    session.commit()
                successful_sources += 1
                result.source_statuses[source.name] = source_status
            except SourceAccessError as error:
                permission_sources += 1
                result.source_statuses[source.name] = "PermissionRequired"
                result.messages.append(f"{source.name}: {error}")
                with self.factory() as session:
                    Repository(session).finish_source_run(
                        source_run_id,
                        "PermissionRequired",
                        discovered_count,
                        refreshed_count,
                        source_errors,
                        str(error),
                    )
                    session.commit()
            except Exception as error:
                LOGGER.exception(
                    "source pipeline failed",
                    extra={"run_id": result.run_id, "source": source.name},
                )
                source_errors += self._record_source_failures(source, result)
                failed_sources += 1
                result.counts["errors"] += 1
                result.source_statuses[source.name] = "Failed"
                result.messages.append(f"{source.name}: {error}")
                with self.factory() as session:
                    repository = Repository(session)
                    repository.record_error(result.run_id, source.name, "source_pipeline", error)
                    repository.finish_source_run(
                        source_run_id,
                        "Failed",
                        discovered_count,
                        refreshed_count,
                        source_errors + 1,
                        str(error),
                    )
                    session.commit()

        self._process_pending_erasures(result)

        collection_healthy = not collection_requested or (
            bool(selected_sources)
            and not failed_sources
            and not permission_sources
            and all(
                result.source_statuses.get(source.name) == "Successful"
                for source in selected_sources
            )
        )
        if not collection_healthy:
            result.messages.append(
                "Collection was incomplete; analysis and normal exports were withheld "
                "to preserve the last known good report"
            )

        errors_before_analysis = result.counts["errors"]
        if analyze and collection_healthy:
            self._analyze(result)

        analysis_healthy = result.counts["errors"] == errors_before_analysis
        if export and collection_healthy and analysis_healthy:
            with self.factory() as session:
                result.exports = ExportService(self.settings, session).export(result.window_end)
        elif export and collection_healthy and not analysis_healthy:
            result.messages.append(
                "Analysis was incomplete; normal exports were withheld to preserve "
                "the last known good report"
            )

        if failed_sources and not successful_sources:
            status = RunStatus.FAILED
        elif result.counts["errors"] or failed_sources or permission_sources:
            status = RunStatus.PARTIAL
        else:
            status = RunStatus.SUCCESS
        result.status = status.value
        with self.factory() as session:
            Repository(session).finish_run(
                result.run_id,
                status,
                result.counts,
                self._summary(result, mode),
            )
            session.commit()
        return result

    def _process_pending_erasures(self, result: PipelineResult) -> bool:
        """Finish durable erasure events before any analysis or ordinary publication."""
        with self.factory() as session:
            pending = Repository(session).pending_erasure_events()
            event_ids = [event.id for event in pending]
        if not event_ids:
            return False

        removed_backups: list[Path] = []
        removed_exports: list[Path] = []
        try:
            removed_backups = purge_backup_artifacts(self.settings.paths.backups)
            removed_exports = purge_export_artifacts(self.settings.paths.exports)
            bind = self.factory.kw.get("bind")
            if bind is not None:
                bind.dispose()
            secure_erase_database(self.settings.paths.database)

            with self.factory() as session:
                result.exports = ExportService(self.settings, session).export(
                    result.window_end,
                    include_analysis=False,
                )
            create_backup(
                self.settings.paths.database,
                self.settings.paths.backups,
                self.settings.retention.daily_backups,
                label="post_erasure",
            )
            with self.factory() as session:
                Repository(session).mark_erasure_events_complete(
                    event_ids,
                    result.window_end,
                )
                session.commit()
            if bind is not None:
                bind.dispose()
            # Completion metadata itself is harmless, but checkpoint it so no
            # stale WAL remains and restored backups deterministically retry only
            # events that were pending when the backup was taken.
            secure_erase_database(self.settings.paths.database)
        except Exception as error:
            with self.factory() as session:
                Repository(session).mark_erasure_events_failed(
                    event_ids,
                    type(error).__name__,
                )
                session.commit()
            raise

        result.messages.append(
            f"Completed {len(event_ids)} durable erasure event(s); replaced "
            f"{len(removed_backups)} managed backup artifact(s) and "
            f"{len(removed_exports)} managed export artifact(s)"
        )
        return True

    def _ingest_candidates(
        self,
        source: SourceAdapter,
        candidates: Iterable[ListingCandidate],
        result: PipelineResult,
        *,
        stage: str,
    ) -> tuple[int, int]:
        processed = 0
        errors = 0
        fetch_method = "oauth_api" if source.name == "reddit" else "authorized_feed"
        with self.factory() as session:
            repository = Repository(session)
            converter = CurrencyConverter(self.settings, session)
            for candidate in candidates:
                try:
                    with session.begin_nested():
                        _prepare_candidate(candidate, converter, result.window_end)
                        outcome = repository.upsert_candidate(
                            candidate,
                            result.run_id,
                            result.window_end,
                            fetch_method=fetch_method,
                        )
                    self._count(result.counts, outcome)
                    processed += 1
                except Exception as error:
                    LOGGER.exception(
                        "listing record failed",
                        extra={
                            "run_id": result.run_id,
                            "source": source.name,
                            "source_listing_id": candidate.source_listing_id,
                        },
                    )
                    errors += 1
                    result.counts["errors"] += 1
                    result.messages.append(
                        f"{source.name} {candidate.source_listing_id or candidate.canonical_url}: "
                        f"{error}"
                    )
                    repository.record_error(
                        result.run_id,
                        source.name,
                        stage,
                        error,
                    )
            session.commit()
        return processed, errors

    def _apply_reddit_deletions(
        self,
        source: SourceAdapter,
        source_ad_ids: set[str],
        result: PipelineResult,
    ) -> int:
        if source.name != "reddit" or not source_ad_ids:
            return 0
        errors = 0
        purged_total = 0
        with self.factory() as session:
            repository = Repository(session)
            for source_ad_id in sorted(source_ad_ids):
                try:
                    with session.begin_nested():
                        purged = repository.purge_deleted_reddit_ad(
                            source_ad_id,
                            result.run_id,
                            result.window_end,
                        )
                    purged_total += purged
                except Exception as error:
                    LOGGER.exception(
                        "Reddit erasure failed",
                        extra={"run_id": result.run_id, "source_ad_id": source_ad_id},
                    )
                    errors += 1
                    result.counts["errors"] += 1
                    result.messages.append(f"reddit erasure {source_ad_id}: {error}")
                    repository.record_error(
                        result.run_id,
                        source.name,
                        "reddit_erasure",
                        error,
                        recoverable=False,
                    )
            session.commit()
        result.counts["compliance_purges"] += purged_total
        return errors

    def _record_source_failures(
        self,
        source: SourceAdapter,
        result: PipelineResult,
    ) -> int:
        failures = source.drain_record_failures()
        if not failures:
            return 0
        with self.factory() as session:
            repository = Repository(session)
            for failure in failures:
                result.counts["errors"] += 1
                record_label = failure.record_id or "unknown record"
                result.messages.append(
                    f"{source.name} {record_label} {failure.stage}: {failure.error}"
                )
                repository.record_error(
                    result.run_id,
                    source.name,
                    failure.stage,
                    failure.error,
                )
            session.commit()
        return len(failures)

    def _analyze(self, result: PipelineResult) -> None:
        with self.factory() as session:
            listings = list(
                session.scalars(
                    select(Listing).where(
                        Listing.current_status.in_(
                            [
                                ListingStatus.ACTIVE.value,
                                ListingStatus.PENDING.value,
                                ListingStatus.RESERVED.value,
                            ]
                        )
                    )
                )
            )
            repository = Repository(session)
            valuation_service = ValuationService(self.settings, session)
            scoring_service = ScoringService(self.settings, session)
            for listing in listings:
                try:
                    with session.begin_nested():
                        latest = session.scalar(
                            select(Valuation)
                            .where(Valuation.listing_id == listing.id)
                            .order_by(
                                Valuation.calculated_at_utc.desc(),
                                Valuation.id.desc(),
                            )
                            .limit(1)
                        )
                        valuation = (
                            valuation_service.value(
                                listing,
                                result.run_id,
                                result.window_end,
                            )
                            if self._valuation_needed(
                                session,
                                listing,
                                latest,
                                result.window_end,
                            )
                            else latest
                        )
                        assert valuation is not None
                        scoring_service.score(
                            listing,
                            valuation,
                            result.run_id,
                            result.window_end,
                        )
                except Exception as error:
                    LOGGER.exception(
                        "valuation or scoring failed",
                        extra={"run_id": result.run_id, "listing_uid": listing.listing_uid},
                    )
                    result.counts["errors"] += 1
                    result.messages.append(f"analysis {listing.listing_uid}: {error}")
                    repository.record_error(
                        result.run_id,
                        listing.source,
                        "valuation_scoring",
                        error,
                        listing_uid=listing.listing_uid,
                    )
            session.commit()

    def _valuation_needed(
        self,
        session: Session,
        listing: Listing,
        latest: Valuation | None,
        calculated_at: datetime,
    ) -> bool:
        if latest is None:
            return True
        if latest.method_version != self.settings.scoring.valuation_version:
            return True
        if not latest.input_fingerprint:
            return True
        latest_at = _as_utc(latest.calculated_at_utc)
        if latest_at > calculated_at:
            return True
        if calculated_at - latest_at >= timedelta(
            hours=self.settings.application.valuation_refresh_hours
        ):
            return True
        if (
            listing.price_last_changed_at_utc
            and _as_utc(listing.price_last_changed_at_utc) > latest_at
        ):
            return True
        if not listing.brand:
            return False
        if listing.reference_number:
            identity_match = Listing.reference_number == listing.reference_number
        elif listing.model:
            identity_match = Listing.model == listing.model
        else:
            return False
        evidence_changed = session.scalar(
            select(Listing.id)
            .where(
                Listing.id != listing.id,
                Listing.brand == listing.brand,
                identity_match,
                or_(
                    Listing.sold_price_usd.is_not(None),
                    Listing.estimated_all_in_usd.is_not(None),
                ),
                or_(
                    Listing.first_seen_at_utc > latest_at,
                    Listing.price_last_changed_at_utc > latest_at,
                    Listing.first_observed_sold_at_utc > latest_at,
                ),
            )
            .limit(1)
        )
        return evidence_changed is not None

    def _summary(self, result: PipelineResult, mode: str) -> dict[str, object]:
        return {
            "mode": mode,
            "sources": result.source_statuses,
            "messages": result.messages,
            "exports": {key: str(path) for key, path in result.exports.items()},
            "compliance_purges": result.counts.get("compliance_purges", 0),
            "source_readiness_fingerprint": source_readiness_fingerprint(self.settings),
        }

    @staticmethod
    def _count(counts: dict[str, int], outcome: object) -> None:
        counts["new_records"] += int(outcome.created)
        counts["updated_records"] += int(outcome.updated)
        counts["duplicate_records_prevented"] += int(outcome.duplicate_prevented)
        counts["sold_status_changes"] += int(outcome.sold_transition)
        counts["price_changes"] += int(outcome.price_changed)
        counts["compliance_purges"] += int(getattr(outcome, "erasure_events", 0))
