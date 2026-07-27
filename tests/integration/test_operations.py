from __future__ import annotations

from pathlib import Path

from alembic import command

from watch_tracker.database.backup import create_backup, integrity_check
from watch_tracker.database.migrations import (
    alembic_config,
    migration_state,
    upgrade_to_head,
)
from watch_tracker.database.session import create_sqlite_engine


def test_fresh_migration_and_validated_backup(settings) -> None:
    upgrade_to_head(settings)
    engine = create_sqlite_engine(settings.paths.database)
    assert migration_state(settings, engine) == ("20260725_0003", "20260725_0003")
    with engine.connect() as connection:
        comparable_columns = {
            row[1] for row in connection.exec_driver_sql("PRAGMA table_info(comparables)")
        }
        exchange_columns = {
            row[1] for row in connection.exec_driver_sql("PRAGMA table_info(exchange_rates)")
        }
        tables = {
            row[0]
            for row in connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "evidence_listing_id" in comparable_columns
    assert "retrieved_at_utc" in exchange_columns
    assert "erasure_events" in tables
    backup = create_backup(settings.paths.database, settings.paths.backups, retain=2)
    assert backup.exists()
    assert integrity_check(backup) == (True, "ok")


def test_erasure_lineage_migration_backfills_existing_evidence_and_fx(settings) -> None:
    command.upgrade(alembic_config(settings), "20260725_0002")
    engine = create_sqlite_engine(settings.paths.database)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            INSERT INTO runs (
                run_id, started_at_utc, completed_at_utc,
                discovery_window_start_utc, discovery_window_end_utc,
                status, dry_run, code_version, new_records, updated_records,
                duplicate_records_prevented, sold_status_changes, price_changes,
                errors, summary
            ) VALUES (
                'legacy-run', '2026-07-25 07:00:00', '2026-07-25 07:01:00',
                '2026-07-23 07:00:00', '2026-07-25 07:00:00',
                'Success', 0, 'legacy', 0, 0, 0, 0, 0, 0, '{}'
            )
            """
        )
        for listing_id, uid, url in (
            (1, "fixture:target", "https://example.invalid/target"),
            (2, "reddit:evidence", "https://example.invalid/evidence"),
        ):
            connection.exec_driver_sql(
                """
                INSERT INTO listings (
                    id, listing_uid, source, source_listing_id, canonical_url,
                    first_seen_at_utc, last_seen_at_utc, last_checked_at_utc,
                    date_confidence, current_status, is_sold,
                    status_checked_at_utc, title, complications, accessories,
                    risk_flags, missing_information, questions_to_ask_seller
                ) VALUES (
                    ?, ?, 'fixture', ?, ?,
                    '2026-07-25 07:00:00', '2026-07-25 07:00:00',
                    '2026-07-25 07:00:00', 'High', 'Active', 0,
                    '2026-07-25 07:00:00', 'Legacy listing',
                    '[]', '[]', '[]', '[]', '[]'
                )
                """,
                (listing_id, uid, uid, url),
            )
        connection.exec_driver_sql(
            """
            INSERT INTO comparables (
                listing_id, run_id, source, source_url, observed_at_utc,
                price_type, price_original, currency, price_usd,
                relevance_weight
            ) VALUES (
                1, 'legacy-run', 'reddit', 'https://example.invalid/evidence',
                '2026-07-25 07:00:00', 'asking', 6000, 'USD', 6000, 0.6
            )
            """
        )
        connection.exec_driver_sql(
            """
            INSERT INTO exchange_rates (
                base_currency, quote_currency, rate, effective_at_utc, provider
            ) VALUES (
                'EUR', 'USD', 1.1, '2026-07-24 00:00:00', 'legacy'
            )
            """
        )
    engine.dispose()

    command.upgrade(alembic_config(settings), "head")
    engine = create_sqlite_engine(settings.paths.database)
    with engine.connect() as connection:
        evidence_listing_id = connection.exec_driver_sql(
            "SELECT evidence_listing_id FROM comparables"
        ).scalar_one()
        effective_at, retrieved_at = connection.exec_driver_sql(
            "SELECT effective_at_utc, retrieved_at_utc FROM exchange_rates"
        ).one()

    assert evidence_listing_id == 2
    assert retrieved_at == effective_at


def test_installer_does_not_relocate_a_built_virtualenv() -> None:
    root = Path(__file__).resolve().parents[2]
    script = (root / "operations/install_macos_runtime.sh").read_text(encoding="utf-8")

    assert 'staging_root="${release_root}"' in script
    assert '"${uv_binary}" venv "${staging_root}/venv"' in script
    assert '/bin/mv "${staging_root}" "${release_root}"' not in script


def test_public_deployment_files_are_portable() -> None:
    root = Path(__file__).resolve().parents[2]
    script = (root / "operations/install_macos_runtime.sh").read_text(encoding="utf-8")
    template = (
        root / "operations/launchd/watch-deal-tracker.plist.template"
    ).read_text(encoding="utf-8")

    assert 'project_root="${script_directory:h}"' in script
    assert 'runtime_root="${user_home}/Library/Application Support/WatchTracker"' in script
    assert 'launchd_label="io.github.vishalthatsme.watch-deal-tracker"' in script
    assert "WATCH_TRACKER_LABEL" in template
    assert "/WATCH_TRACKER_RUNTIME" in template
    assert "/Users/" not in script
    assert "/Users/" not in template
