from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Run(Base):
    __tablename__ = "runs"

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    started_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    discovery_window_start_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    discovery_window_end_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    code_version: Mapped[str] = mapped_column(String(64), nullable=False)
    new_records: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_records: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duplicate_records_prevented: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sold_status_changes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    price_changes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    errors: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    source_runs: Mapped[list[SourceRun]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class SourceRun(Base):
    __tablename__ = "source_runs"
    __table_args__ = (UniqueConstraint("run_id", "source", name="uq_source_run"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    discovered_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    refreshed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    message: Mapped[str | None] = mapped_column(Text)

    run: Mapped[Run] = relationship(back_populates="source_runs")


class DuplicateGroup(Base):
    __tablename__ = "duplicate_groups"

    duplicate_group_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    confidence: Mapped[str] = mapped_column(String(16), nullable=False)
    rationale: Mapped[str | None] = mapped_column(Text)
    created_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Listing(Base):
    __tablename__ = "listings"
    __table_args__ = (
        UniqueConstraint("listing_uid", name="uq_listing_uid"),
        UniqueConstraint("source", "source_listing_id", name="uq_source_listing_id"),
        Index("ix_listing_status_source", "current_status", "source"),
        Index("ix_listing_brand_reference", "brand", "reference_number"),
        Index("ix_listing_posted", "original_posted_at_utc"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    listing_uid: Mapped[str] = mapped_column(String(96), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_listing_id: Mapped[str | None] = mapped_column(String(160))
    source_ad_id: Mapped[str | None] = mapped_column(String(160), index=True)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False)
    duplicate_group_id: Mapped[str | None] = mapped_column(
        ForeignKey("duplicate_groups.duplicate_group_id")
    )
    first_seen_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_checked_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    original_posted_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    date_evidence: Mapped[str | None] = mapped_column(Text)
    date_confidence: Mapped[str] = mapped_column(String(16), nullable=False)
    current_status: Mapped[str] = mapped_column(String(24), nullable=False)
    is_sold: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status_evidence: Mapped[str | None] = mapped_column(Text)
    status_checked_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sold_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_observed_sold_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sold_price_original: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    sold_price_currency: Mapped[str | None] = mapped_column(String(3))
    sold_price_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    sold_price_evidence: Mapped[str | None] = mapped_column(Text)
    sold_price_confidence: Mapped[str | None] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(Text, nullable=False)
    brand: Mapped[str | None] = mapped_column(String(100), index=True)
    model: Mapped[str | None] = mapped_column(String(200))
    reference_number: Mapped[str | None] = mapped_column(String(120), index=True)
    approximate_year: Mapped[int | None] = mapped_column(Integer)
    case_material: Mapped[str | None] = mapped_column(String(100))
    case_size_mm: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    dial_description: Mapped[str | None] = mapped_column(Text)
    movement_or_caliber: Mapped[str | None] = mapped_column(String(120))
    complications: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    condition: Mapped[str | None] = mapped_column(String(120))
    condition_notes: Mapped[str | None] = mapped_column(Text)
    box_included: Mapped[bool | None] = mapped_column(Boolean)
    papers_included: Mapped[bool | None] = mapped_column(Boolean)
    accessories: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    service_history: Mapped[str | None] = mapped_column(Text)
    seller_name: Mapped[str | None] = mapped_column(String(200))
    seller_type: Mapped[str | None] = mapped_column(String(24))
    seller_location: Mapped[str | None] = mapped_column(String(200))
    seller_reputation_evidence: Mapped[str | None] = mapped_column(Text)
    transaction_protection: Mapped[str | None] = mapped_column(Text)
    return_policy: Mapped[str | None] = mapped_column(Text)
    initial_asking_price_original: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    latest_asking_price_original: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    asking_price_currency: Mapped[str | None] = mapped_column(String(3))
    initial_asking_price_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    latest_asking_price_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    stated_shipping_cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    estimated_all_in_original: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    estimated_all_in_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    price_last_changed_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    negotiable: Mapped[bool | None] = mapped_column(Boolean)
    description_summary: Mapped[str | None] = mapped_column(Text)
    authenticity_notes: Mapped[str | None] = mapped_column(Text)
    image_quality_notes: Mapped[str | None] = mapped_column(Text)
    risk_flags: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    missing_information: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    questions_to_ask_seller: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)


class ListingAlias(Base):
    __tablename__ = "listing_aliases"
    __table_args__ = (
        UniqueConstraint("source", "alias_type", "alias_value", name="uq_listing_alias"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id"), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    alias_type: Mapped[str] = mapped_column(String(32), nullable=False)
    alias_value: Mapped[str] = mapped_column(Text, nullable=False)
    created_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ListingObservation(Base):
    __tablename__ = "listing_observations"
    __table_args__ = (
        UniqueConstraint("run_id", "listing_id", "content_hash", name="uq_observation"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id"), nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"), nullable=False, index=True)
    observed_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(32), nullable=False)
    fetch_method: Mapped[str] = mapped_column(String(32), nullable=False)
    retrieval_outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    parsed_fields: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    evidence_excerpt: Mapped[str | None] = mapped_column(Text)


class ListingStatusHistory(Base):
    __tablename__ = "listing_status_history"
    __table_args__ = (
        UniqueConstraint("run_id", "listing_id", "new_status", name="uq_status_history_run"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id"), nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"), nullable=False, index=True)
    observed_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    old_status: Mapped[str | None] = mapped_column(String(24))
    new_status: Mapped[str] = mapped_column(String(24), nullable=False)
    evidence: Mapped[str | None] = mapped_column(Text)


class ListingPriceHistory(Base):
    __tablename__ = "listing_price_history"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "listing_id",
            "price_type",
            "currency",
            "amount_original",
            name="uq_price_history_run",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id"), nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"), nullable=False, index=True)
    observed_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    price_type: Mapped[str] = mapped_column(String(24), nullable=False)
    amount_original: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    amount_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    evidence: Mapped[str | None] = mapped_column(Text)


class Comparable(Base):
    __tablename__ = "comparables"
    __table_args__ = (
        UniqueConstraint(
            "listing_id",
            "run_id",
            "source_url",
            "observed_at_utc",
            name="uq_comparable",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id"), nullable=False, index=True)
    evidence_listing_id: Mapped[int | None] = mapped_column(ForeignKey("listings.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    observed_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    transaction_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reference_number: Mapped[str | None] = mapped_column(String(120))
    condition: Mapped[str | None] = mapped_column(String(120))
    box_included: Mapped[bool | None] = mapped_column(Boolean)
    papers_included: Mapped[bool | None] = mapped_column(Boolean)
    price_type: Mapped[str] = mapped_column(String(24), nullable=False)
    price_original: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    price_usd: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    relevance_weight: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    evidence: Mapped[str | None] = mapped_column(Text)


class Valuation(Base):
    __tablename__ = "valuations"
    __table_args__ = (
        UniqueConstraint("listing_id", "run_id", "method_version", name="uq_valuation_run"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id"), nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"), nullable=False, index=True)
    calculated_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    method_version: Mapped[str] = mapped_column(String(32), nullable=False)
    input_fingerprint: Mapped[str | None] = mapped_column(String(64))
    fair_value_low_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    fair_value_mid_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    fair_value_high_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    discount_to_fair_value_pct: Mapped[float | None] = mapped_column(Float)
    comparable_count: Mapped[int] = mapped_column(Integer, nullable=False)
    completed_sale_comparable_count: Mapped[int] = mapped_column(Integer, nullable=False)
    confidence: Mapped[str] = mapped_column(String(16), nullable=False)
    assumptions: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)


class DealScore(Base):
    __tablename__ = "deal_scores"
    __table_args__ = (
        UniqueConstraint("listing_id", "run_id", "method_version", name="uq_deal_score_run"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id"), nullable=False, index=True)
    valuation_id: Mapped[int | None] = mapped_column(ForeignKey("valuations.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"), nullable=False, index=True)
    calculated_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    method_version: Mapped[str] = mapped_column(String(32), nullable=False)
    price_points: Mapped[float] = mapped_column(Float, nullable=False)
    condition_points: Mapped[float] = mapped_column(Float, nullable=False)
    safety_points: Mapped[float] = mapped_column(Float, nullable=False)
    liquidity_points: Mapped[float] = mapped_column(Float, nullable=False)
    pre_cap_score: Mapped[float] = mapped_column(Float, nullable=False)
    total_score: Mapped[float] = mapped_column(Float, nullable=False)
    cap_reason: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[str] = mapped_column(String(16), nullable=False)
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    recommended_action: Mapped[str] = mapped_column(String(24), nullable=False)
    suggested_opening_offer_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))


class ExchangeRate(Base):
    __tablename__ = "exchange_rates"
    __table_args__ = (
        UniqueConstraint("base_currency", "quote_currency", "effective_at_utc", name="uq_fx_rate"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    base_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    quote_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    rate: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    effective_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    retrieved_at_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    provider: Mapped[str] = mapped_column(String(120), nullable=False)


class ErasureEvent(Base):
    __tablename__ = "erasure_events"
    __table_args__ = (
        UniqueConstraint(
            "source",
            "source_object_id",
            "event_type",
            name="uq_erasure_event_source_object",
        ),
        Index("ix_erasure_events_status_detected", "status", "detected_at_utc"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_object_id: Mapped[str] = mapped_column(String(200), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    detected_run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.run_id"), nullable=False, index=True
    )
    detected_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="Pending")
    logical_purge_completed_at_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    physical_scrub_completed_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    artifacts_regenerated_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class CollectionError(Base):
    __tablename__ = "collection_errors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"), nullable=False, index=True)
    source: Mapped[str | None] = mapped_column(String(32))
    listing_uid: Mapped[str | None] = mapped_column(String(96))
    stage: Mapped[str] = mapped_column(String(64), nullable=False)
    error_type: Mapped[str] = mapped_column(String(160), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    recoverable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
