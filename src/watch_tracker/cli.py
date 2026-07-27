from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import typer
from filelock import FileLock, Timeout
from sqlalchemy import select

from watch_tracker.config import Settings, load_settings
from watch_tracker.database.backup import create_backup
from watch_tracker.database.migrations import (
    assert_at_head,
    migration_state,
    upgrade_to_head,
)
from watch_tracker.database.models import Run
from watch_tracker.database.repository import Repository
from watch_tracker.database.session import create_sqlite_engine, make_session_factory
from watch_tracker.domain import RunStatus
from watch_tracker.health import (
    check_health,
    launchd_runtime_status,
    scheduler_plist_configuration,
    scheduler_plist_path,
    system_timezone_name,
    timezones_equivalent,
)
from watch_tracker.logging import configure_logging
from watch_tracker.pipeline import DailyPipeline
from watch_tracker.services.exports import ExportService
from watch_tracker.services.readiness import source_readiness_fingerprint

app = typer.Typer(
    no_args_is_help=True,
    help="Collect, maintain, value, and export high-end watch listings.",
)


def _settings(config: Path | None) -> Settings:
    settings = load_settings(config)
    configure_logging(
        settings.paths.logs,
        settings.application.log_level,
        settings.retention.log_days,
    )
    return settings


def _runtime(settings: Settings):
    engine = create_sqlite_engine(settings.paths.database)
    return engine, make_session_factory(engine)


@contextmanager
def _application_lock(settings: Settings, timeout: float = 60) -> Iterator[None]:
    if os.getenv("WATCH_TRACKER_DEPLOYMENT_LOCK_HELD", "").casefold() in {
        "1",
        "true",
        "yes",
    }:
        yield
        return
    lock = FileLock(str(settings.paths.lock))
    try:
        lock.acquire(timeout=timeout)
        os.chmod(settings.paths.lock, 0o600)
    except Timeout as error:
        raise RuntimeError("Timed out waiting for the watch-tracker application lock") from error
    try:
        yield
    finally:
        lock.release()


def _parse_as_of(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _print_result(result: object) -> None:
    typer.echo(
        json.dumps(
            {
                "run_id": result.run_id,
                "status": result.status,
                "window_start": result.window_start.isoformat(),
                "window_end": result.window_end.isoformat(),
                "counts": result.counts,
                "sources": result.source_statuses,
                "exports": {key: str(path) for key, path in result.exports.items()},
                "messages": result.messages,
            },
            indent=2,
        )
    )


def _scheduled_run_satisfies_guard(run: Run, readiness_fingerprint: str) -> bool:
    if not isinstance(run.summary, dict):
        return False
    if run.summary.get("source_readiness_fingerprint") != readiness_fingerprint:
        return False
    if run.status == RunStatus.SUCCESS.value:
        return True
    if run.status != RunStatus.PARTIAL.value:
        return False
    sources = run.summary.get("sources")
    return (
        bool(sources)
        and isinstance(sources, dict)
        and all(status == "PermissionRequired" for status in sources.values())
    )


@app.command()
def migrate(
    config: Path | None = typer.Option(None, exists=True, dir_okay=False),
) -> None:
    """Back up an existing database and apply reviewed schema migrations."""
    settings = _settings(config)
    with _application_lock(settings):
        engine, _ = _runtime(settings)
        current, expected = migration_state(settings, engine)
        if current == expected:
            typer.echo(f"Database is already at migration head {expected}.")
            return
        if settings.paths.database.exists() and settings.paths.database.stat().st_size > 0:
            backup = create_backup(
                settings.paths.database,
                settings.paths.backups,
                settings.retention.daily_backups,
                label="pre-migration",
            )
            typer.echo(f"Validated pre-migration backup: {backup}")
        upgrade_to_head(settings)
        current, expected = migration_state(settings, engine)
        if current != expected:
            raise typer.Exit(code=1)
        typer.echo(f"Database migrated to {expected}: {settings.paths.database}")


@app.command("run")
def run_pipeline(
    config: Path | None = typer.Option(None, exists=True, dir_okay=False),
    dry_run: bool = typer.Option(False, help="Collect and validate without persistent changes."),
    source: str | None = typer.Option(None, help="Limit diagnostics to one source."),
    as_of: str | None = typer.Option(None, help="UTC or offset ISO timestamp."),
) -> None:
    """Run the complete discovery, refresh, valuation, scoring, and export pipeline."""
    settings = _settings(config)
    engine, factory = _runtime(settings)
    assert_at_head(settings, engine)
    result = DailyPipeline(settings, factory).run(
        as_of=_parse_as_of(as_of),
        dry_run=dry_run,
        source_filter=source,
        mode="source_diagnostic" if source else "daily",
    )
    _print_result(result)
    if dry_run and (
        not result.source_statuses
        or any(status != "DryRunSuccess" for status in result.source_statuses.values())
    ):
        raise typer.Exit(code=2)
    if result.status == RunStatus.FAILED.value:
        raise typer.Exit(code=1)
    if result.status == RunStatus.PARTIAL.value:
        raise typer.Exit(code=2)


@app.command()
def discover(
    config: Path | None = typer.Option(None, exists=True, dir_okay=False),
    source: str | None = typer.Option(None, help="Limit discovery to one source."),
    as_of: str | None = typer.Option(None, help="UTC or offset ISO timestamp."),
) -> None:
    """Discover the configured rolling window without refreshing older records."""
    settings = _settings(config)
    engine, factory = _runtime(settings)
    assert_at_head(settings, engine)
    result = DailyPipeline(settings, factory).run(
        as_of=_parse_as_of(as_of),
        source_filter=source,
        refresh=False,
        mode="discovery",
    )
    _print_result(result)
    if result.status == RunStatus.FAILED.value:
        raise typer.Exit(code=1)
    if result.status == RunStatus.PARTIAL.value:
        raise typer.Exit(code=2)


@app.command("refresh-status")
def refresh_status(
    config: Path | None = typer.Option(None, exists=True, dir_okay=False),
    source: str | None = typer.Option(None, help="Limit refresh to one source."),
) -> None:
    """Refresh existing records without discovering new advertisements."""
    settings = _settings(config)
    engine, factory = _runtime(settings)
    assert_at_head(settings, engine)
    result = DailyPipeline(settings, factory).run(
        source_filter=source,
        discover=False,
        mode="status_refresh",
    )
    _print_result(result)
    if result.status == RunStatus.FAILED.value:
        raise typer.Exit(code=1)
    if result.status == RunStatus.PARTIAL.value:
        raise typer.Exit(code=2)


@app.command()
def value(
    config: Path | None = typer.Option(None, exists=True, dir_okay=False),
) -> None:
    """Recalculate active valuations and scores from committed evidence."""
    settings = _settings(config)
    engine, factory = _runtime(settings)
    assert_at_head(settings, engine)
    result = DailyPipeline(settings, factory).run(
        discover=False,
        refresh=False,
        mode="valuation",
    )
    _print_result(result)


@app.command()
def backfill(
    hours: int = typer.Option(..., min=49, help="Intentional discovery lookback in hours."),
    config: Path | None = typer.Option(None, exists=True, dir_okay=False),
    source: str | None = typer.Option(None, help="Limit backfill to one source."),
    as_of: str | None = typer.Option(None, help="UTC or offset ISO timestamp."),
) -> None:
    """Intentionally discover an older window while preserving normal daily settings."""
    settings = _settings(config)
    settings.application.discovery_window_hours = hours
    engine, factory = _runtime(settings)
    assert_at_head(settings, engine)
    result = DailyPipeline(settings, factory).run(
        as_of=_parse_as_of(as_of),
        source_filter=source,
        refresh=False,
        mode="backfill",
    )
    _print_result(result)
    if result.status == RunStatus.FAILED.value:
        raise typer.Exit(code=1)
    if result.status == RunStatus.PARTIAL.value:
        raise typer.Exit(code=2)


@app.command("scheduled-run")
def scheduled_run(
    config: Path | None = typer.Option(None, exists=True, dir_okay=False),
) -> None:
    """Run once per local date, including safe launch-at-login catch-up behavior."""
    settings = _settings(config)
    engine, factory = _runtime(settings)
    assert_at_head(settings, engine)
    timezone = ZoneInfo(settings.application.timezone)
    today = datetime.now(timezone).date()
    readiness_fingerprint = source_readiness_fingerprint(settings)
    with factory() as session:
        pending_erasure = Repository(session).has_pending_erasures()
        recent_runs = list(
            session.scalars(
                select(Run)
                .where(Run.status.in_([RunStatus.SUCCESS.value, RunStatus.PARTIAL.value]))
                .order_by(Run.started_at_utc.desc())
            )
        )
        latest_daily = next(
            (
                run
                for run in recent_runs
                if isinstance(run.summary, dict) and run.summary.get("mode") == "daily"
            ),
            None,
        )
        if latest_daily and not pending_erasure:
            started = latest_daily.started_at_utc
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
            if started.astimezone(timezone).date() == today and _scheduled_run_satisfies_guard(
                latest_daily,
                readiness_fingerprint,
            ):
                typer.echo(f"Scheduled run already completed for {today}; no-op.")
                return
    result = DailyPipeline(settings, factory).run(mode="daily")
    _print_result(result)
    if result.status == RunStatus.FAILED.value:
        raise typer.Exit(code=1)
    # Permission-gated sources make a run Partial but should not cause launchd
    # to hammer retries; source health is visible in the run record.


@app.command()
def backup(
    config: Path | None = typer.Option(None, exists=True, dir_okay=False),
) -> None:
    """Create and verify a transaction-safe SQLite backup."""
    settings = _settings(config)
    with _application_lock(settings):
        engine, factory = _runtime(settings)
        assert_at_head(settings, engine)
        with factory() as session:
            if Repository(session).has_pending_erasures():
                raise RuntimeError(
                    "A compliance erasure is pending; run the pipeline before creating a backup"
                )
        path = create_backup(
            settings.paths.database,
            settings.paths.backups,
            settings.retention.daily_backups,
        )
    typer.echo(str(path))


@app.command()
def export(
    config: Path | None = typer.Option(None, exists=True, dir_okay=False),
) -> None:
    """Regenerate atomic CSV and Markdown exports from committed state."""
    settings = _settings(config)
    with _application_lock(settings):
        engine, factory = _runtime(settings)
        assert_at_head(settings, engine)
        with factory() as session:
            if Repository(session).has_pending_erasures():
                raise RuntimeError(
                    "A compliance erasure is pending; run the pipeline before exporting"
                )
            paths = ExportService(settings, session).export(datetime.now(UTC))
    typer.echo(json.dumps({key: str(path) for key, path in paths.items()}, indent=2))


@app.command()
def healthcheck(
    config: Path | None = typer.Option(None, exists=True, dir_okay=False),
) -> None:
    """Check database, latest run, exports, and local operational health."""
    settings = _settings(config)
    engine, factory = _runtime(settings)
    current, expected = migration_state(settings, engine)
    report = check_health(settings, factory)
    report.checks["migration"] = f"current={current}; expected={expected}"
    if current != expected:
        report.healthy = False
        report.warnings.append("Database migration is not at head")
    typer.echo(
        json.dumps(
            {
                "healthy": report.healthy,
                "checks": report.checks,
                "warnings": report.warnings,
            },
            indent=2,
        )
    )
    if not report.healthy:
        raise typer.Exit(code=1)


@app.command("scheduler-status")
def scheduler_status(
    config: Path | None = typer.Option(None, exists=True, dir_okay=False),
) -> None:
    """Show configured and installed scheduling state plus the launchd runtime state."""
    settings = _settings(config)
    timezone = ZoneInfo(settings.application.timezone)
    now = datetime.now(timezone)
    candidate = datetime.combine(
        now.date(),
        time(settings.application.schedule_hour, settings.application.schedule_minute),
        tzinfo=timezone,
    )
    if candidate <= now:
        candidate += timedelta(days=1)
    path = scheduler_plist_path()
    warnings: list[str] = []
    plist: dict[str, object] | None = None
    if path.exists():
        try:
            plist = scheduler_plist_configuration(path)
        except (OSError, ValueError) as error:
            warnings.append(f"LaunchAgent plist is invalid: {error}")
    else:
        warnings.append("LaunchAgent is not installed")
    expected_schedule = (
        settings.application.schedule_hour,
        settings.application.schedule_minute,
    )
    plist_matches = bool(
        plist
        and (plist["hour"], plist["minute"]) == expected_schedule
        and plist["timezone"] == settings.application.timezone
        and "scheduled-run" in plist["program_arguments"]
    )
    if plist and not plist_matches:
        warnings.append("Installed LaunchAgent does not match the configured schedule")
    system_timezone = system_timezone_name()
    timezone_matches = (
        timezones_equivalent(
            settings.application.timezone,
            system_timezone,
            now=now.astimezone(UTC),
        )
        if system_timezone
        else None
    )
    if timezone_matches is False:
        warnings.append("Configured timezone differs from the host timezone used by launchd")
    elif timezone_matches is None:
        warnings.append("Could not verify the host IANA timezone")
    runtime = launchd_runtime_status()
    if runtime["supported"] and not runtime["loaded"]:
        warnings.append(f"LaunchAgent is not loaded: {runtime['message']}")
    if runtime["last_exit_code"] not in (None, 0):
        warnings.append(f"LaunchAgent's last process exited with code {runtime['last_exit_code']}")
    typer.echo(
        json.dumps(
            {
                "configured_timezone": settings.application.timezone,
                "system_timezone": system_timezone or "unknown",
                "timezone_matches_system": timezone_matches,
                "configured_schedule": {
                    "hour": settings.application.schedule_hour,
                    "minute": settings.application.schedule_minute,
                },
                "expected_next_trigger": candidate.isoformat(),
                "plist": str(path),
                "plist_exists": path.exists(),
                "plist_configuration": plist,
                "plist_matches_configuration": plist_matches,
                "launchd": runtime,
                "warnings": warnings,
                "note": (
                    "The next trigger is computed from configuration; launchd exposes current "
                    "service state but not a reliable authoritative next-fire timestamp."
                ),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    app()
