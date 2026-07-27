"""Initial persistent watch-market schema.

This migration is an immutable SQLite schema snapshot. It deliberately does not
import the application's live ORM metadata.

Revision ID: 20260725_0001
Revises:
Create Date: 2026-07-25
"""

from alembic import op

revision = "20260725_0001"
down_revision = None
branch_labels = None
depends_on = None

UPGRADE_SQL = (
    """
    CREATE TABLE schema_versions (
        id INTEGER NOT NULL PRIMARY KEY,
        version VARCHAR(64) NOT NULL UNIQUE,
        applied_at_utc DATETIME NOT NULL
    )
    """,
    """
    CREATE TABLE runs (
        run_id VARCHAR(36) NOT NULL PRIMARY KEY,
        started_at_utc DATETIME NOT NULL,
        completed_at_utc DATETIME,
        discovery_window_start_utc DATETIME NOT NULL,
        discovery_window_end_utc DATETIME NOT NULL,
        status VARCHAR(24) NOT NULL,
        dry_run BOOLEAN NOT NULL,
        code_version VARCHAR(64) NOT NULL,
        new_records INTEGER NOT NULL,
        updated_records INTEGER NOT NULL,
        duplicate_records_prevented INTEGER NOT NULL,
        sold_status_changes INTEGER NOT NULL,
        price_changes INTEGER NOT NULL,
        errors INTEGER NOT NULL,
        summary JSON NOT NULL
    )
    """,
    """
    CREATE TABLE duplicate_groups (
        duplicate_group_id VARCHAR(64) NOT NULL PRIMARY KEY,
        confidence VARCHAR(16) NOT NULL,
        rationale TEXT,
        created_at_utc DATETIME NOT NULL
    )
    """,
    """
    CREATE TABLE exchange_rates (
        id INTEGER NOT NULL PRIMARY KEY,
        base_currency VARCHAR(3) NOT NULL,
        quote_currency VARCHAR(3) NOT NULL,
        rate NUMERIC(18, 8) NOT NULL,
        effective_at_utc DATETIME NOT NULL,
        provider VARCHAR(120) NOT NULL,
        CONSTRAINT uq_fx_rate
            UNIQUE (base_currency, quote_currency, effective_at_utc)
    )
    """,
    """
    CREATE TABLE source_runs (
        id INTEGER NOT NULL PRIMARY KEY,
        run_id VARCHAR(36) NOT NULL REFERENCES runs (run_id),
        source VARCHAR(32) NOT NULL,
        started_at_utc DATETIME NOT NULL,
        completed_at_utc DATETIME,
        status VARCHAR(24) NOT NULL,
        discovered_count INTEGER NOT NULL,
        refreshed_count INTEGER NOT NULL,
        error_count INTEGER NOT NULL,
        message TEXT,
        CONSTRAINT uq_source_run UNIQUE (run_id, source)
    )
    """,
    "CREATE INDEX ix_source_runs_run_id ON source_runs (run_id)",
    """
    CREATE TABLE listings (
        id INTEGER NOT NULL PRIMARY KEY,
        listing_uid VARCHAR(96) NOT NULL,
        source VARCHAR(32) NOT NULL,
        source_listing_id VARCHAR(160),
        source_ad_id VARCHAR(160),
        canonical_url TEXT NOT NULL,
        duplicate_group_id VARCHAR(64) REFERENCES duplicate_groups (duplicate_group_id),
        first_seen_at_utc DATETIME NOT NULL,
        last_seen_at_utc DATETIME NOT NULL,
        last_checked_at_utc DATETIME NOT NULL,
        original_posted_at_utc DATETIME,
        date_evidence TEXT,
        date_confidence VARCHAR(16) NOT NULL,
        current_status VARCHAR(24) NOT NULL,
        is_sold BOOLEAN NOT NULL,
        status_evidence TEXT,
        status_checked_at_utc DATETIME NOT NULL,
        sold_at_utc DATETIME,
        first_observed_sold_at_utc DATETIME,
        sold_price_original NUMERIC(14, 2),
        sold_price_currency VARCHAR(3),
        sold_price_usd NUMERIC(14, 2),
        sold_price_evidence TEXT,
        sold_price_confidence VARCHAR(16),
        title TEXT NOT NULL,
        brand VARCHAR(100),
        model VARCHAR(200),
        reference_number VARCHAR(120),
        approximate_year INTEGER,
        case_material VARCHAR(100),
        case_size_mm NUMERIC(5, 2),
        dial_description TEXT,
        movement_or_caliber VARCHAR(120),
        complications JSON NOT NULL,
        condition VARCHAR(120),
        condition_notes TEXT,
        box_included BOOLEAN,
        papers_included BOOLEAN,
        accessories JSON NOT NULL,
        service_history TEXT,
        seller_name VARCHAR(200),
        seller_type VARCHAR(24),
        seller_location VARCHAR(200),
        seller_reputation_evidence TEXT,
        transaction_protection TEXT,
        return_policy TEXT,
        initial_asking_price_original NUMERIC(14, 2),
        latest_asking_price_original NUMERIC(14, 2),
        asking_price_currency VARCHAR(3),
        initial_asking_price_usd NUMERIC(14, 2),
        latest_asking_price_usd NUMERIC(14, 2),
        stated_shipping_cost NUMERIC(14, 2),
        estimated_all_in_original NUMERIC(14, 2),
        estimated_all_in_usd NUMERIC(14, 2),
        price_last_changed_at_utc DATETIME,
        negotiable BOOLEAN,
        description_summary TEXT,
        authenticity_notes TEXT,
        image_quality_notes TEXT,
        risk_flags JSON NOT NULL,
        missing_information JSON NOT NULL,
        questions_to_ask_seller JSON NOT NULL,
        CONSTRAINT uq_listing_uid UNIQUE (listing_uid),
        CONSTRAINT uq_source_listing_id UNIQUE (source, source_listing_id)
    )
    """,
    "CREATE INDEX ix_listing_posted ON listings (original_posted_at_utc)",
    "CREATE INDEX ix_listings_brand ON listings (brand)",
    "CREATE INDEX ix_listing_brand_reference ON listings (brand, reference_number)",
    "CREATE INDEX ix_listings_source_ad_id ON listings (source_ad_id)",
    "CREATE INDEX ix_listing_status_source ON listings (current_status, source)",
    "CREATE INDEX ix_listings_reference_number ON listings (reference_number)",
    """
    CREATE TABLE collection_errors (
        id INTEGER NOT NULL PRIMARY KEY,
        run_id VARCHAR(36) NOT NULL REFERENCES runs (run_id),
        source VARCHAR(32),
        listing_uid VARCHAR(96),
        stage VARCHAR(64) NOT NULL,
        error_type VARCHAR(160) NOT NULL,
        message TEXT NOT NULL,
        occurred_at_utc DATETIME NOT NULL,
        retry_count INTEGER NOT NULL,
        recoverable BOOLEAN NOT NULL
    )
    """,
    "CREATE INDEX ix_collection_errors_run_id ON collection_errors (run_id)",
    """
    CREATE TABLE listing_aliases (
        id INTEGER NOT NULL PRIMARY KEY,
        listing_id INTEGER NOT NULL REFERENCES listings (id),
        source VARCHAR(32) NOT NULL,
        alias_type VARCHAR(32) NOT NULL,
        alias_value TEXT NOT NULL,
        created_at_utc DATETIME NOT NULL,
        CONSTRAINT uq_listing_alias UNIQUE (source, alias_type, alias_value)
    )
    """,
    "CREATE INDEX ix_listing_aliases_listing_id ON listing_aliases (listing_id)",
    """
    CREATE TABLE listing_observations (
        id INTEGER NOT NULL PRIMARY KEY,
        listing_id INTEGER NOT NULL REFERENCES listings (id),
        run_id VARCHAR(36) NOT NULL REFERENCES runs (run_id),
        observed_at_utc DATETIME NOT NULL,
        source_url TEXT NOT NULL,
        content_hash VARCHAR(64) NOT NULL,
        parser_version VARCHAR(32) NOT NULL,
        fetch_method VARCHAR(32) NOT NULL,
        retrieval_outcome VARCHAR(32) NOT NULL,
        parsed_fields JSON NOT NULL,
        evidence_excerpt TEXT,
        CONSTRAINT uq_observation UNIQUE (run_id, listing_id, content_hash)
    )
    """,
    "CREATE INDEX ix_listing_observations_run_id ON listing_observations (run_id)",
    "CREATE INDEX ix_listing_observations_listing_id ON listing_observations (listing_id)",
    """
    CREATE TABLE listing_status_history (
        id INTEGER NOT NULL PRIMARY KEY,
        listing_id INTEGER NOT NULL REFERENCES listings (id),
        run_id VARCHAR(36) NOT NULL REFERENCES runs (run_id),
        observed_at_utc DATETIME NOT NULL,
        old_status VARCHAR(24),
        new_status VARCHAR(24) NOT NULL,
        evidence TEXT,
        CONSTRAINT uq_status_history_run UNIQUE (run_id, listing_id, new_status)
    )
    """,
    "CREATE INDEX ix_listing_status_history_listing_id ON listing_status_history (listing_id)",
    "CREATE INDEX ix_listing_status_history_run_id ON listing_status_history (run_id)",
    """
    CREATE TABLE listing_price_history (
        id INTEGER NOT NULL PRIMARY KEY,
        listing_id INTEGER NOT NULL REFERENCES listings (id),
        run_id VARCHAR(36) NOT NULL REFERENCES runs (run_id),
        observed_at_utc DATETIME NOT NULL,
        price_type VARCHAR(24) NOT NULL,
        amount_original NUMERIC(14, 2) NOT NULL,
        currency VARCHAR(3) NOT NULL,
        amount_usd NUMERIC(14, 2),
        evidence TEXT,
        CONSTRAINT uq_price_history_run
            UNIQUE (run_id, listing_id, price_type, currency, amount_original)
    )
    """,
    "CREATE INDEX ix_listing_price_history_listing_id ON listing_price_history (listing_id)",
    "CREATE INDEX ix_listing_price_history_run_id ON listing_price_history (run_id)",
    """
    CREATE TABLE comparables (
        id INTEGER NOT NULL PRIMARY KEY,
        listing_id INTEGER NOT NULL REFERENCES listings (id),
        run_id VARCHAR(36) NOT NULL REFERENCES runs (run_id),
        source VARCHAR(64) NOT NULL,
        source_url TEXT NOT NULL,
        observed_at_utc DATETIME NOT NULL,
        transaction_date DATETIME,
        reference_number VARCHAR(120),
        condition VARCHAR(120),
        box_included BOOLEAN,
        papers_included BOOLEAN,
        price_type VARCHAR(24) NOT NULL,
        price_original NUMERIC(14, 2) NOT NULL,
        currency VARCHAR(3) NOT NULL,
        price_usd NUMERIC(14, 2) NOT NULL,
        relevance_weight FLOAT NOT NULL,
        evidence TEXT,
        CONSTRAINT uq_comparable
            UNIQUE (listing_id, source_url, observed_at_utc)
    )
    """,
    "CREATE INDEX ix_comparables_run_id ON comparables (run_id)",
    "CREATE INDEX ix_comparables_listing_id ON comparables (listing_id)",
    """
    CREATE TABLE valuations (
        id INTEGER NOT NULL PRIMARY KEY,
        listing_id INTEGER NOT NULL REFERENCES listings (id),
        run_id VARCHAR(36) NOT NULL REFERENCES runs (run_id),
        calculated_at_utc DATETIME NOT NULL,
        method_version VARCHAR(32) NOT NULL,
        fair_value_low_usd NUMERIC(14, 2) NOT NULL,
        fair_value_mid_usd NUMERIC(14, 2) NOT NULL,
        fair_value_high_usd NUMERIC(14, 2) NOT NULL,
        discount_to_fair_value_pct FLOAT,
        comparable_count INTEGER NOT NULL,
        completed_sale_comparable_count INTEGER NOT NULL,
        confidence VARCHAR(16) NOT NULL,
        assumptions JSON NOT NULL,
        CONSTRAINT uq_valuation_run UNIQUE (listing_id, run_id, method_version)
    )
    """,
    "CREATE INDEX ix_valuations_run_id ON valuations (run_id)",
    "CREATE INDEX ix_valuations_listing_id ON valuations (listing_id)",
    """
    CREATE TABLE deal_scores (
        id INTEGER NOT NULL PRIMARY KEY,
        listing_id INTEGER NOT NULL REFERENCES listings (id),
        run_id VARCHAR(36) NOT NULL REFERENCES runs (run_id),
        calculated_at_utc DATETIME NOT NULL,
        method_version VARCHAR(32) NOT NULL,
        price_points FLOAT NOT NULL,
        condition_points FLOAT NOT NULL,
        safety_points FLOAT NOT NULL,
        liquidity_points FLOAT NOT NULL,
        pre_cap_score FLOAT NOT NULL,
        total_score FLOAT NOT NULL,
        cap_reason TEXT,
        confidence VARCHAR(16) NOT NULL,
        risk_level VARCHAR(16) NOT NULL,
        rationale TEXT NOT NULL,
        recommended_action VARCHAR(24) NOT NULL,
        suggested_opening_offer_usd NUMERIC(14, 2),
        CONSTRAINT uq_deal_score_run UNIQUE (listing_id, run_id, method_version)
    )
    """,
    "CREATE INDEX ix_deal_scores_listing_id ON deal_scores (listing_id)",
    "CREATE INDEX ix_deal_scores_run_id ON deal_scores (run_id)",
)

DOWNGRADE_TABLES = (
    "deal_scores",
    "valuations",
    "comparables",
    "listing_price_history",
    "listing_status_history",
    "listing_observations",
    "listing_aliases",
    "collection_errors",
    "listings",
    "source_runs",
    "exchange_rates",
    "duplicate_groups",
    "runs",
    "schema_versions",
)


def upgrade() -> None:
    for statement in UPGRADE_SQL:
        op.execute(statement)


def downgrade() -> None:
    for table_name in DOWNGRADE_TABLES:
        op.drop_table(table_name)
