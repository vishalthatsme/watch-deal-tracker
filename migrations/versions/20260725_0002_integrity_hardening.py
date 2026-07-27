"""Harden valuation linkage and remove the redundant schema-version table.

Revision ID: 20260725_0002
Revises: 20260725_0001
Create Date: 2026-07-25
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260725_0002"
down_revision = "20260725_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("schema_versions")
    comparable_uniques = {
        tuple(item["column_names"])
        for item in inspect(op.get_bind()).get_unique_constraints("comparables")
    }
    desired_comparable_unique = (
        "listing_id",
        "run_id",
        "source_url",
        "observed_at_utc",
    )
    if desired_comparable_unique not in comparable_uniques:
        # SQLite reports inline UNIQUE constraints from raw DDL without a
        # droppable name. Rebuilding this leaf table is deterministic and
        # avoids relying on Alembic's generated constraint naming.
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
        op.create_index("ix_comparables_run_id", "comparables", ["run_id"])
        op.create_index("ix_comparables_listing_id", "comparables", ["listing_id"])
    with op.batch_alter_table("valuations") as batch:
        batch.add_column(sa.Column("input_fingerprint", sa.String(length=64), nullable=True))
        batch.alter_column(
            "fair_value_low_usd",
            existing_type=sa.Numeric(14, 2),
            nullable=True,
        )
        batch.alter_column(
            "fair_value_mid_usd",
            existing_type=sa.Numeric(14, 2),
            nullable=True,
        )
        batch.alter_column(
            "fair_value_high_usd",
            existing_type=sa.Numeric(14, 2),
            nullable=True,
        )
    with op.batch_alter_table("deal_scores") as batch:
        batch.add_column(sa.Column("valuation_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_deal_scores_valuation_id",
            "valuations",
            ["valuation_id"],
            ["id"],
        )
        batch.create_index("ix_deal_scores_valuation_id", ["valuation_id"])


def downgrade() -> None:
    op.execute("UPDATE valuations SET fair_value_low_usd = 0 WHERE fair_value_low_usd IS NULL")
    op.execute("UPDATE valuations SET fair_value_mid_usd = 0 WHERE fair_value_mid_usd IS NULL")
    op.execute("UPDATE valuations SET fair_value_high_usd = 0 WHERE fair_value_high_usd IS NULL")
    with op.batch_alter_table("deal_scores") as batch:
        batch.drop_index("ix_deal_scores_valuation_id")
        batch.drop_constraint("fk_deal_scores_valuation_id", type_="foreignkey")
        batch.drop_column("valuation_id")
    with op.batch_alter_table("valuations") as batch:
        batch.alter_column(
            "fair_value_high_usd",
            existing_type=sa.Numeric(14, 2),
            nullable=False,
        )
        batch.alter_column(
            "fair_value_mid_usd",
            existing_type=sa.Numeric(14, 2),
            nullable=False,
        )
        batch.alter_column(
            "fair_value_low_usd",
            existing_type=sa.Numeric(14, 2),
            nullable=False,
        )
        batch.drop_column("input_fingerprint")
    op.execute(
        """
        CREATE TABLE comparables_v1 (
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
                listing_id, source_url, observed_at_utc
            )
        )
        """
    )
    op.execute(
        """
        INSERT INTO comparables_v1 (
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
    op.rename_table("comparables_v1", "comparables")
    op.create_index("ix_comparables_run_id", "comparables", ["run_id"])
    op.create_index("ix_comparables_listing_id", "comparables", ["listing_id"])
    op.create_table(
        "schema_versions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("applied_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("version"),
    )
