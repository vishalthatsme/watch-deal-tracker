"""Add durable erasure work, evidence lineage, and FX retrieval timestamps.

Revision ID: 20260725_0003
Revises: 20260725_0002
Create Date: 2026-07-25
"""

import sqlalchemy as sa
from alembic import op

revision = "20260725_0003"
down_revision = "20260725_0002"
branch_labels = None
depends_on = None


def _create_comparables_v3() -> None:
    op.execute(
        """
        CREATE TABLE comparables_v3 (
            id INTEGER NOT NULL PRIMARY KEY,
            listing_id INTEGER NOT NULL REFERENCES listings (id),
            evidence_listing_id INTEGER REFERENCES listings (id),
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
            CONSTRAINT uq_comparable UNIQUE (
                listing_id, run_id, source_url, observed_at_utc
            )
        )
        """
    )
    op.execute(
        """
        INSERT INTO comparables_v3 (
            id, listing_id, evidence_listing_id, run_id, source, source_url,
            observed_at_utc, transaction_date, reference_number, condition,
            box_included, papers_included, price_type, price_original,
            currency, price_usd, relevance_weight, evidence
        )
        SELECT
            c.id,
            c.listing_id,
            (
                SELECT l.id
                FROM listings AS l
                WHERE l.canonical_url = c.source_url
                ORDER BY l.id
                LIMIT 1
            ),
            c.run_id,
            c.source,
            c.source_url,
            c.observed_at_utc,
            c.transaction_date,
            c.reference_number,
            c.condition,
            c.box_included,
            c.papers_included,
            c.price_type,
            c.price_original,
            c.currency,
            c.price_usd,
            c.relevance_weight,
            c.evidence
        FROM comparables AS c
        """
    )
    op.drop_table("comparables")
    op.rename_table("comparables_v3", "comparables")
    op.create_index("ix_comparables_listing_id", "comparables", ["listing_id"])
    op.create_index(
        "ix_comparables_evidence_listing_id",
        "comparables",
        ["evidence_listing_id"],
    )
    op.create_index("ix_comparables_run_id", "comparables", ["run_id"])


def upgrade() -> None:
    _create_comparables_v3()

    with op.batch_alter_table("exchange_rates") as batch:
        batch.add_column(
            sa.Column("retrieved_at_utc", sa.DateTime(timezone=True), nullable=True)
        )
    op.execute(
        """
        UPDATE exchange_rates
        SET retrieved_at_utc = effective_at_utc
        WHERE retrieved_at_utc IS NULL
        """
    )
    with op.batch_alter_table("exchange_rates") as batch:
        batch.alter_column(
            "retrieved_at_utc",
            existing_type=sa.DateTime(timezone=True),
            nullable=False,
        )

    op.create_table(
        "erasure_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("source_object_id", sa.String(length=200), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("detected_run_id", sa.String(length=36), nullable=False),
        sa.Column("detected_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column(
            "logical_purge_completed_at_utc",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "physical_scrub_completed_at_utc",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "artifacts_regenerated_at_utc",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["detected_run_id"], ["runs.run_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source",
            "source_object_id",
            "event_type",
            name="uq_erasure_event_source_object",
        ),
    )
    op.create_index(
        "ix_erasure_events_detected_run_id",
        "erasure_events",
        ["detected_run_id"],
    )
    op.create_index(
        "ix_erasure_events_status_detected",
        "erasure_events",
        ["status", "detected_at_utc"],
    )


def downgrade() -> None:
    op.drop_index("ix_erasure_events_status_detected", table_name="erasure_events")
    op.drop_index("ix_erasure_events_detected_run_id", table_name="erasure_events")
    op.drop_table("erasure_events")

    with op.batch_alter_table("exchange_rates") as batch:
        batch.drop_column("retrieved_at_utc")

    op.execute(
        """
        CREATE TABLE comparables_v2 (
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
            CONSTRAINT uq_comparable UNIQUE (
                listing_id, run_id, source_url, observed_at_utc
            )
        )
        """
    )
    op.execute(
        """
        INSERT INTO comparables_v2 (
            id, listing_id, run_id, source, source_url, observed_at_utc,
            transaction_date, reference_number, condition, box_included,
            papers_included, price_type, price_original, currency,
            price_usd, relevance_weight, evidence
        )
        SELECT
            id, listing_id, run_id, source, source_url, observed_at_utc,
            transaction_date, reference_number, condition, box_included,
            papers_included, price_type, price_original, currency,
            price_usd, relevance_weight, evidence
        FROM comparables
        """
    )
    op.drop_table("comparables")
    op.rename_table("comparables_v2", "comparables")
    op.create_index("ix_comparables_listing_id", "comparables", ["listing_id"])
    op.create_index("ix_comparables_run_id", "comparables", ["run_id"])
