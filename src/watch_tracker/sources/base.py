from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime

from watch_tracker.domain import ListingCandidate


class SourceAccessError(RuntimeError):
    def __init__(self, source: str, code: str, message: str) -> None:
        super().__init__(message)
        self.source = source
        self.code = code


@dataclass(slots=True)
class RefreshOutcome:
    candidates: list[ListingCandidate]
    deleted_source_ad_ids: set[str]
    # A source can return a parent record containing zero or more current
    # offers.  Keeping both sets lets the pipeline reconcile offers removed by
    # an edit without treating an absent API response as proof of deletion.
    returned_source_ad_ids: set[str] = field(default_factory=set)
    current_source_listing_ids: set[str] = field(default_factory=set)


@dataclass(slots=True)
class SourceRecordFailure:
    stage: str
    record_id: str | None
    error: Exception


class SourceAdapter(ABC):
    name: str

    @abstractmethod
    def discover(self, window_start: datetime, window_end: datetime) -> list[ListingCandidate]:
        raise NotImplementedError

    @abstractmethod
    def refresh(self, source_ad_ids: list[str]) -> RefreshOutcome:
        raise NotImplementedError

    def close(self) -> None:
        return None

    def record_failure(
        self,
        stage: str,
        error: Exception,
        record_id: str | None = None,
    ) -> None:
        failures = getattr(self, "_record_failures", None)
        if failures is None:
            failures = []
            self._record_failures = failures
        failures.append(SourceRecordFailure(stage, record_id, error))

    def drain_record_failures(self) -> list[SourceRecordFailure]:
        failures = list(getattr(self, "_record_failures", []))
        self._record_failures = []
        return failures
