from __future__ import annotations

import csv
import json
import os
import re
import tempfile
from contextlib import suppress
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from watch_tracker.config import Settings
from watch_tracker.database.models import DealScore, Listing, Valuation
from watch_tracker.domain import ListingStatus

CSV_COLUMNS = [
    "listing_uid",
    "duplicate_group_id",
    "source",
    "source_listing_id",
    "source_ad_id",
    "canonical_url",
    "first_seen_at_utc",
    "last_seen_at_utc",
    "last_checked_at_utc",
    "original_posted_at_utc",
    "date_evidence",
    "date_confidence",
    "current_status",
    "is_sold",
    "status_evidence",
    "status_checked_at_utc",
    "sold_at_utc",
    "first_observed_sold_at_utc",
    "sold_price_original",
    "sold_price_currency",
    "sold_price_usd",
    "sold_price_evidence",
    "sold_price_confidence",
    "title",
    "brand",
    "model",
    "reference_number",
    "approximate_year",
    "case_material",
    "case_size_mm",
    "condition",
    "condition_notes",
    "box_included",
    "papers_included",
    "service_history",
    "seller_name",
    "seller_type",
    "seller_location",
    "seller_reputation_evidence",
    "transaction_protection",
    "initial_asking_price_original",
    "latest_asking_price_original",
    "asking_price_currency",
    "initial_asking_price_usd",
    "latest_asking_price_usd",
    "stated_shipping_cost",
    "estimated_all_in_original",
    "estimated_all_in_usd",
    "price_last_changed_at_utc",
    "risk_flags",
    "missing_information",
    "questions_to_ask_seller",
    "fair_value_low_usd",
    "fair_value_mid_usd",
    "fair_value_high_usd",
    "discount_to_fair_value_pct",
    "comparable_count",
    "completed_sale_comparable_count",
    "valuation_confidence",
    "deal_score",
    "deal_score_confidence",
    "risk_level",
    "deal_rationale",
    "recommended_action",
    "suggested_opening_offer_usd",
]

_CURRENT_EXPORT_NAMES = {
    "watch_listings_latest.csv",
    "watch_active_deals.csv",
    "watch_sales_history.csv",
}
_DATED_EXPORT_PATTERN = re.compile(
    r"(?:watch_listings_\d{4}-\d{2}-\d{2}\.csv"
    r"|watch_deal_report_\d{4}-\d{2}-\d{2}\.md)"
)
_TEMP_EXPORT_PATTERN = re.compile(
    r"\.(?:watch_listings_latest\.csv"
    r"|watch_active_deals\.csv"
    r"|watch_sales_history\.csv"
    r"|watch_listings_\d{4}-\d{2}-\d{2}\.csv"
    r"|watch_deal_report_\d{4}-\d{2}-\d{2}\.md)"
    r"\.[A-Za-z0-9_-]+\.tmp"
)


def _is_managed_export_artifact(name: str) -> bool:
    return (
        name in _CURRENT_EXPORT_NAMES
        or _DATED_EXPORT_PATTERN.fullmatch(name) is not None
        or _TEMP_EXPORT_PATTERN.fullmatch(name) is not None
    )


def purge_export_artifacts(export_directory: Path) -> list[Path]:
    """Remove only Watch Tracker-generated exports directly inside an export directory."""
    if not export_directory.exists():
        return []
    if not export_directory.is_dir():
        raise NotADirectoryError(export_directory)

    removed: list[Path] = []
    for artifact in sorted(export_directory.iterdir()):
        if not _is_managed_export_artifact(artifact.name):
            continue
        if not (artifact.is_file() or artifact.is_symlink()):
            continue
        artifact.unlink()
        removed.append(artifact)
    return removed


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    if text.startswith(("=", "+", "-", "@", "\t", "\r")):
        return f"'{text}"
    return text


def _atomic_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({column: _text(row.get(column)) for column in CSV_COLUMNS})
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        with suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        with suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


class ExportService:
    def __init__(self, settings: Settings, session: Session) -> None:
        self.settings = settings
        self.session = session

    def _rows(self, *, include_analysis: bool = True) -> list[dict[str, Any]]:
        valuations = (
            list(
                self.session.scalars(
                    select(Valuation).order_by(
                        Valuation.listing_id,
                        Valuation.calculated_at_utc.desc(),
                        Valuation.id.desc(),
                    )
                )
            )
            if include_analysis
            else []
        )
        scores = (
            list(
                self.session.scalars(
                    select(DealScore).order_by(
                        DealScore.listing_id,
                        DealScore.calculated_at_utc.desc(),
                        DealScore.id.desc(),
                    )
                )
            )
            if include_analysis
            else []
        )
        latest_valuation: dict[int, Valuation] = {}
        valuation_by_id: dict[int, Valuation] = {}
        valuation_by_listing_run: dict[tuple[int, str], Valuation] = {}
        latest_score: dict[int, DealScore] = {}
        for valuation in valuations:
            latest_valuation.setdefault(valuation.listing_id, valuation)
            valuation_by_id[valuation.id] = valuation
            valuation_by_listing_run[(valuation.listing_id, valuation.run_id)] = valuation
        for score in scores:
            latest_score.setdefault(score.listing_id, score)

        rows: list[dict[str, Any]] = []
        listings = list(
            self.session.scalars(
                select(Listing).order_by(
                    Listing.current_status != ListingStatus.ACTIVE.value,
                    Listing.id,
                )
            )
        )
        for listing in listings:
            row = {column: getattr(listing, column, None) for column in CSV_COLUMNS}
            score = latest_score.get(listing.id)
            valuation = (
                (
                    valuation_by_id.get(score.valuation_id)
                    if score and score.valuation_id is not None
                    else None
                )
                or (valuation_by_listing_run.get((listing.id, score.run_id)) if score else None)
                or latest_valuation.get(listing.id)
            )
            if valuation:
                row.update(
                    {
                        "fair_value_low_usd": valuation.fair_value_low_usd,
                        "fair_value_mid_usd": valuation.fair_value_mid_usd,
                        "fair_value_high_usd": valuation.fair_value_high_usd,
                        "discount_to_fair_value_pct": valuation.discount_to_fair_value_pct,
                        "comparable_count": valuation.comparable_count,
                        "completed_sale_comparable_count": (
                            valuation.completed_sale_comparable_count
                        ),
                        "valuation_confidence": valuation.confidence,
                    }
                )
            if score:
                row.update(
                    {
                        "deal_score": score.total_score,
                        "deal_score_confidence": score.confidence,
                        "risk_level": score.risk_level,
                        "deal_rationale": score.rationale,
                        "recommended_action": score.recommended_action,
                        "suggested_opening_offer_usd": score.suggested_opening_offer_usd,
                    }
                )
            rows.append(row)
        return rows

    def export(
        self,
        generated_at: datetime,
        *,
        include_analysis: bool = True,
    ) -> dict[str, Path]:
        rows = self._rows(include_analysis=include_analysis)
        rows.sort(
            key=lambda row: (
                row.get("current_status") != ListingStatus.ACTIVE.value,
                -(float(row.get("deal_score") or 0)),
                row.get("listing_uid") or "",
            )
        )
        local_date = (
            generated_at.astimezone(ZoneInfo(self.settings.application.timezone)).date().isoformat()
        )
        paths = {
            "latest": self.settings.paths.exports / "watch_listings_latest.csv",
            "active": self.settings.paths.exports / "watch_active_deals.csv",
            "sales": self.settings.paths.exports / "watch_sales_history.csv",
            "snapshot": self.settings.paths.exports / f"watch_listings_{local_date}.csv",
            "report": self.settings.paths.exports / f"watch_deal_report_{local_date}.md",
        }
        _atomic_csv(paths["latest"], rows)
        _atomic_csv(
            paths["active"],
            [row for row in rows if row["current_status"] == ListingStatus.ACTIVE.value],
        )
        _atomic_csv(paths["sales"], [row for row in rows if row.get("is_sold")])
        _atomic_csv(paths["snapshot"], rows)
        _atomic_text(
            paths["report"],
            self._report(rows, generated_at, include_analysis=include_analysis),
        )
        return paths

    def _report(
        self,
        rows: list[dict[str, Any]],
        generated_at: datetime,
        *,
        include_analysis: bool = True,
    ) -> str:
        active = [row for row in rows if row["current_status"] == ListingStatus.ACTIVE.value]
        rankable = (
            [
                row
                for row in active
                if (row.get("comparable_count") or 0) >= 3
                and (row.get("completed_sale_comparable_count") or 0) >= 1
                and row.get("valuation_confidence") in {"Medium", "High"}
                and row.get("deal_score") is not None
            ][:10]
            if include_analysis
            else []
        )
        lines = [
            "# Watch Deal Report",
            "",
            f"Generated: {generated_at.isoformat()}",
            "",
            f"- Listings in database: {len(rows)}",
            f"- Active listings: {len(active)}",
            f"- Confirmed sold listings: {sum(bool(row.get('is_sold')) for row in rows)}",
            f"- Rankable active deals: {len(rankable)}",
            "",
            "## Top active deals",
            "",
        ]
        if not rankable:
            lines.append(
                (
                    "Rankings were intentionally withheld while compliance erasure "
                    "artifacts were regenerated."
                )
                if not include_analysis
                else "No active listing has enough independent comparable evidence to rank yet."
            )
        for rank, row in enumerate(rankable, 1):
            lines.extend(
                [
                    f"### {rank}. {row.get('brand') or 'Unknown'} — "
                    f"{row.get('model') or row.get('reference_number') or row.get('title')}",
                    "",
                    f"- Score: {row.get('deal_score')}/10 ({row.get('deal_score_confidence')})",
                    f"- Asking/all-in: ${_text(row.get('estimated_all_in_usd'))}",
                    f"- Fair value: ${_text(row.get('fair_value_low_usd'))}–"
                    f"${_text(row.get('fair_value_high_usd'))}",
                    f"- Risk: {row.get('risk_level')}",
                    f"- Recommendation: {row.get('recommended_action')}",
                    f"- URL: {row.get('canonical_url')}",
                    f"- Rationale: {row.get('deal_rationale')}",
                    "",
                ]
            )
        lines.extend(
            [
                "## Method limitation",
                "",
                "Listings with no independent comparable evidence receive a provisional, "
                "low-confidence score capped at 5.0 and are excluded from the ranking.",
                "",
            ]
        )
        return "\n".join(lines)
