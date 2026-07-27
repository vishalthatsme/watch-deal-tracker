from __future__ import annotations

import os
import plistlib
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import inspect, select
from sqlalchemy.orm import sessionmaker

from watch_tracker.config import Settings
from watch_tracker.database.backup import integrity_check
from watch_tracker.database.models import Run
from watch_tracker.database.repository import Repository
from watch_tracker.domain import RunStatus

SCHEDULER_LABEL = "io.github.vishalthatsme.watch-deal-tracker"
CATCHUP_INTERVAL_SECONDS = 30 * 60
SECURE_UMASK = 0o077
STALE_AFTER = timedelta(hours=36)
STUCK_AFTER = timedelta(hours=1)


@dataclass(slots=True)
class HealthReport:
    healthy: bool
    checks: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _is_daily(run: Run) -> bool:
    return isinstance(run.summary, dict) and run.summary.get("mode") == "daily"


def system_timezone_name() -> str | None:
    """Return the host IANA timezone when /etc/localtime exposes it."""
    try:
        resolved = Path("/etc/localtime").resolve(strict=True)
    except OSError:
        return None
    parts = resolved.parts
    try:
        marker = parts.index("zoneinfo")
    except ValueError:
        return None
    value = "/".join(parts[marker + 1 :])
    return value or None


def timezones_equivalent(left: str, right: str, *, now: datetime) -> bool:
    try:
        left_zone = ZoneInfo(left)
        right_zone = ZoneInfo(right)
    except (ValueError, ZoneInfoNotFoundError):
        return False
    # Offset checks in winter and summer accept equivalent IANA aliases while
    # still catching configurations that make launchd fire at a different midnight.
    for month in (1, 7):
        sample = datetime(now.year, month, 1, 12, tzinfo=UTC)
        if sample.astimezone(left_zone).utcoffset() != sample.astimezone(right_zone).utcoffset():
            return False
    return True


def scheduler_plist_path() -> Path:
    return Path.home() / f"Library/LaunchAgents/{SCHEDULER_LABEL}.plist"


def scheduler_plist_configuration(path: Path | None = None) -> dict[str, Any]:
    plist_path = path or scheduler_plist_path()
    try:
        with plist_path.open("rb") as handle:
            payload = plistlib.load(handle)
    except plistlib.InvalidFileException as error:
        raise ValueError("plist is not valid XML or binary plist data") from error
    interval = payload.get("StartCalendarInterval")
    if not isinstance(interval, dict):
        raise ValueError("StartCalendarInterval must be a dictionary")
    hour = interval.get("Hour")
    minute = interval.get("Minute")
    if not isinstance(hour, int) or not isinstance(minute, int):
        raise ValueError("StartCalendarInterval must contain integer Hour and Minute values")
    catchup_interval = payload.get("StartInterval")
    if not isinstance(catchup_interval, int) or isinstance(catchup_interval, bool):
        raise ValueError("StartInterval must be an integer number of seconds")
    environment = payload.get("EnvironmentVariables")
    if not isinstance(environment, dict):
        environment = {}
    arguments = payload.get("ProgramArguments")
    if not isinstance(arguments, list):
        arguments = []
    return {
        "label": payload.get("Label"),
        "hour": hour,
        "minute": minute,
        "catchup_interval_seconds": catchup_interval,
        "timezone": environment.get("WATCH_TRACKER_TIMEZONE"),
        "python_dont_write_bytecode": environment.get("PYTHONDONTWRITEBYTECODE"),
        "program_arguments": arguments,
        "standard_output_path": payload.get("StandardOutPath"),
        "standard_error_path": payload.get("StandardErrorPath"),
        "umask": payload.get("Umask"),
    }


def launchd_runtime_status(label: str = SCHEDULER_LABEL) -> dict[str, str | int | bool | None]:
    domain = f"gui/{os.getuid()}/{label}"
    if sys.platform != "darwin":
        return {
            "supported": False,
            "loaded": None,
            "domain": domain,
            "state": None,
            "last_exit_code": None,
            "message": "launchd inspection is only supported on macOS",
        }
    try:
        completed = subprocess.run(
            ["/bin/launchctl", "print", domain],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return {
            "supported": True,
            "loaded": False,
            "domain": domain,
            "state": None,
            "last_exit_code": None,
            "message": str(error),
        }
    output = completed.stdout
    state_match = re.search(r"^\s*state = (.+?)\s*$", output, flags=re.MULTILINE)
    exit_match = re.search(r"^\s*last exit code = (-?\d+)\s*$", output, flags=re.MULTILINE)
    message = completed.stderr.strip() or ("loaded" if completed.returncode == 0 else "not loaded")
    return {
        "supported": True,
        "loaded": completed.returncode == 0,
        "domain": domain,
        "state": state_match.group(1) if state_match else None,
        "last_exit_code": int(exit_match.group(1)) if exit_match else None,
        "message": message,
    }


def _scheduler_health(
    settings: Settings,
    *,
    now: datetime,
    plist_path: Path | None,
) -> HealthReport:
    checks: dict[str, str] = {}
    warnings: list[str] = []
    healthy = True
    configured_timezone = settings.application.timezone
    checks["configured_timezone"] = configured_timezone

    system_timezone = system_timezone_name()
    checks["system_timezone"] = system_timezone or "unknown"
    if system_timezone is None:
        warnings.append("Could not verify the host IANA timezone")
    elif not timezones_equivalent(configured_timezone, system_timezone, now=now):
        healthy = False
        warnings.append(
            f"Configured timezone {configured_timezone} does not match host timezone "
            f"{system_timezone}; launchd uses the host timezone"
        )

    configured_path = plist_path or scheduler_plist_path()
    checks["launchd_plist"] = str(configured_path)
    if not configured_path.exists():
        healthy = False
        warnings.append("LaunchAgent is not installed")
    else:
        try:
            plist = scheduler_plist_configuration(configured_path)
        except (OSError, plistlib.InvalidFileException, ValueError) as error:
            healthy = False
            warnings.append(f"LaunchAgent plist is invalid: {error}")
        else:
            checks["plist_schedule"] = f"{plist['hour']:02}:{plist['minute']:02}"
            checks["plist_catchup_interval_seconds"] = str(plist["catchup_interval_seconds"])
            checks["plist_timezone"] = str(plist["timezone"] or "missing")
            expected_schedule = (
                settings.application.schedule_hour,
                settings.application.schedule_minute,
            )
            actual_schedule = (plist["hour"], plist["minute"])
            if plist["label"] != SCHEDULER_LABEL:
                healthy = False
                warnings.append("LaunchAgent plist label does not match the expected service")
            if actual_schedule != expected_schedule:
                healthy = False
                warnings.append(
                    "LaunchAgent calendar interval does not match configured daily schedule"
                )
            if plist["catchup_interval_seconds"] != CATCHUP_INTERVAL_SECONDS:
                healthy = False
                warnings.append(
                    "LaunchAgent catch-up interval does not match the expected 30 minutes"
                )
            if plist["timezone"] != configured_timezone:
                healthy = False
                warnings.append("LaunchAgent timezone does not match configured timezone")
            if plist["python_dont_write_bytecode"] != "1":
                healthy = False
                warnings.append(
                    "LaunchAgent must disable Python bytecode writes in immutable releases"
                )
            if "scheduled-run" not in plist["program_arguments"]:
                healthy = False
                warnings.append("LaunchAgent does not invoke the scheduled-run command")
            if (
                plist["standard_output_path"] != "/dev/null"
                or plist["standard_error_path"] != "/dev/null"
            ):
                healthy = False
                warnings.append(
                    "LaunchAgent stdout/stderr must use /dev/null; "
                    "structured logs rotate separately"
                )
            if plist["umask"] != SECURE_UMASK:
                healthy = False
                warnings.append("LaunchAgent must set a private mode-077 umask")

    runtime = launchd_runtime_status()
    checks["launchd_domain"] = str(runtime["domain"])
    if runtime["supported"]:
        checks["launchd_loaded"] = str(runtime["loaded"]).casefold()
        checks["launchd_state"] = str(runtime["state"] or "idle")
        checks["launchd_last_exit_code"] = str(
            runtime["last_exit_code"] if runtime["last_exit_code"] is not None else "unknown"
        )
        if not runtime["loaded"]:
            healthy = False
            warnings.append(f"LaunchAgent is not loaded: {runtime['message']}")
        elif runtime["last_exit_code"] not in (None, 0):
            healthy = False
            warnings.append(
                f"LaunchAgent's last process exited with code {runtime['last_exit_code']}"
            )
    else:
        checks["launchd_loaded"] = "unsupported"
        warnings.append(str(runtime["message"]))

    return HealthReport(healthy=healthy, checks=checks, warnings=warnings)


def _database_run_health(
    factory: sessionmaker,
    *,
    checked_at: datetime,
) -> HealthReport:
    checks: dict[str, str] = {}
    warnings: list[str] = []
    healthy = True
    with factory() as session:
        table_names = set(inspect(session.bind).get_table_names())
        required = {
            "runs",
            "source_runs",
            "listings",
            "listing_observations",
            "erasure_events",
        }
        missing = required - table_names
        if missing:
            return HealthReport(
                healthy=False,
                checks={"schema": f"Missing tables: {', '.join(sorted(missing))}"},
            )

        checks["schema"] = "Required tables present"
        if Repository(session).has_pending_erasures():
            healthy = False
            checks["pending_erasure_events"] = "present"
            warnings.append(
                "A compliance erasure is incomplete; database scrubbing and "
                "artifact regeneration must resume"
            )
        else:
            checks["pending_erasure_events"] = "none"
        recent_runs = list(
            session.scalars(
                select(Run).where(Run.dry_run.is_(False)).order_by(Run.started_at_utc.desc())
            )
        )
        running = [run for run in recent_runs if run.status == RunStatus.RUNNING.value]
        stuck = [run for run in running if checked_at - _as_utc(run.started_at_utc) > STUCK_AFTER]
        if stuck:
            oldest = min(stuck, key=lambda run: _as_utc(run.started_at_utc))
            age = checked_at - _as_utc(oldest.started_at_utc)
            healthy = False
            checks["stuck_run"] = oldest.run_id
            warnings.append(
                f"Run {oldest.run_id} has remained Running for {age.total_seconds() / 3600:.1f}h"
            )
        elif running:
            checks["active_run"] = running[0].run_id

        latest_daily = next(
            (
                run
                for run in recent_runs
                if _is_daily(run) and run.status != RunStatus.RUNNING.value
            ),
            None,
        )
        if latest_daily is None:
            healthy = False
            warnings.append("No completed full daily collection run exists yet")
        else:
            checks["latest_daily_run"] = f"{latest_daily.run_id}; status={latest_daily.status}"
            completed = latest_daily.completed_at_utc
            if completed is None:
                healthy = False
                warnings.append(
                    f"Latest daily run {latest_daily.run_id} has no completion timestamp"
                )
            else:
                completed_utc = _as_utc(completed)
                checks["latest_daily_completed_at"] = completed_utc.isoformat()
                age = checked_at - completed_utc
                if age > STALE_AFTER:
                    healthy = False
                    warnings.append(
                        f"Latest daily run is stale ({age.total_seconds() / 3600:.1f}h)"
                    )
                elif age < -timedelta(minutes=5):
                    healthy = False
                    warnings.append("Latest daily run completion timestamp is in the future")
            if latest_daily.status != RunStatus.SUCCESS.value:
                healthy = False
                sources = (
                    latest_daily.summary.get("sources")
                    if isinstance(latest_daily.summary, dict)
                    else None
                )
                detail = f"; sources={sources}" if sources else ""
                warnings.append(
                    f"Latest full daily run status is {latest_daily.status}, not Success{detail}"
                )

    return HealthReport(healthy=healthy, checks=checks, warnings=warnings)


def check_health(
    settings: Settings,
    factory: sessionmaker,
    *,
    now: datetime | None = None,
    plist_path: Path | None = None,
) -> HealthReport:
    checked_at = _as_utc(now or datetime.now(UTC))
    checks: dict[str, str] = {}
    warnings: list[str] = []
    healthy = True

    database_ok, database_message = integrity_check(settings.paths.database)
    checks["database_integrity"] = database_message
    healthy &= database_ok

    if settings.paths.database.exists():
        database = _database_run_health(factory, checked_at=checked_at)
        checks.update(database.checks)
        warnings.extend(database.warnings)
        healthy &= database.healthy
    else:
        checks["schema"] = "Skipped because the database does not exist"

    free = shutil.disk_usage(settings.paths.database.parent).free
    checks["disk_free_bytes"] = str(free)
    if free < 250 * 1024 * 1024:
        warnings.append("Less than 250 MiB free in the database volume")
        healthy = False

    latest_export = settings.paths.exports / "watch_listings_latest.csv"
    if latest_export.exists():
        modified = datetime.fromtimestamp(latest_export.stat().st_mtime, tz=UTC)
        export_age = checked_at - modified
        checks["latest_export"] = str(latest_export)
        checks["latest_export_modified_at"] = modified.isoformat()
        if export_age > STALE_AFTER:
            healthy = False
            warnings.append(
                f"Latest CSV export is stale ({export_age.total_seconds() / 3600:.1f}h)"
            )
        elif export_age < -timedelta(minutes=5):
            healthy = False
            warnings.append("Latest CSV export timestamp is in the future")
    else:
        healthy = False
        warnings.append("Latest CSV export does not exist yet")

    reddit_ready = bool(
        settings.sources.reddit.enabled
        and settings.sources.reddit.access_approved
        and settings.sources.reddit.deletion_contract_verified
        and settings.sources.reddit.client_id
        and settings.sources.reddit.client_secret
        and settings.sources.reddit.username
    )
    chrono24_ready = bool(
        settings.sources.chrono24.enabled
        and settings.sources.chrono24.access_authorized
        and settings.sources.chrono24.authorized_feed_path
        and settings.sources.chrono24.authorized_feed_path.exists()
    )
    checks["reddit_collection"] = "ready" if reddit_ready else "permission/credentials required"
    checks["chrono24_collection"] = (
        "ready" if chrono24_ready else "written permission/licensed feed required"
    )
    if not reddit_ready and not chrono24_ready:
        warnings.append("No listing source is currently authorized and ready")
        healthy = False

    scheduler = _scheduler_health(settings, now=checked_at, plist_path=plist_path)
    checks.update(scheduler.checks)
    warnings.extend(scheduler.warnings)
    healthy &= scheduler.healthy

    return HealthReport(healthy=healthy, checks=checks, warnings=warnings)
