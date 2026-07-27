from __future__ import annotations

import os
import plistlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from watch_tracker import health
from watch_tracker.cli import _scheduled_run_satisfies_guard
from watch_tracker.database.models import Base, ErasureEvent, Run
from watch_tracker.database.session import create_sqlite_engine, make_session_factory
from watch_tracker.domain import RunStatus


def _scheduler_plist(settings, tmp_path):
    path = tmp_path / f"{health.SCHEDULER_LABEL}.plist"
    path.write_bytes(
        plistlib.dumps(
            {
                "Label": health.SCHEDULER_LABEL,
                "ProgramArguments": ["/runtime/watch-tracker", "scheduled-run"],
                "EnvironmentVariables": {
                    "WATCH_TRACKER_TIMEZONE": settings.application.timezone,
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
                "StartCalendarInterval": {"Hour": 0, "Minute": 0},
                "StartInterval": health.CATCHUP_INTERVAL_SECONDS,
                "StandardOutPath": "/dev/null",
                "StandardErrorPath": "/dev/null",
                "Umask": health.SECURE_UMASK,
            }
        )
    )
    return path


def _healthy_runtime(settings, tmp_path, monkeypatch, now):
    engine = create_sqlite_engine(settings.paths.database)
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    settings.sources.reddit.access_approved = True
    settings.sources.reddit.client_id = "client"
    settings.sources.reddit.client_secret = "secret"
    settings.sources.reddit.username = "collector"
    settings.sources.reddit.deletion_contract_verified = True
    export = settings.paths.exports / "watch_listings_latest.csv"
    export.write_text("listing_uid\n", encoding="utf-8")
    os.utime(export, (now.timestamp(), now.timestamp()))
    plist_path = _scheduler_plist(settings, tmp_path)
    monkeypatch.setattr(health, "system_timezone_name", lambda: settings.application.timezone)
    monkeypatch.setattr(
        health,
        "launchd_runtime_status",
        lambda: {
            "supported": True,
            "loaded": True,
            "domain": f"gui/501/{health.SCHEDULER_LABEL}",
            "state": "not running",
            "last_exit_code": 0,
            "message": "loaded",
        },
    )
    return factory, plist_path


def _run(
    run_id: str,
    *,
    now: datetime,
    status: RunStatus,
    summary: dict,
    started_hours_ago: float = 2,
    completed_hours_ago: float | None = 1,
) -> Run:
    return Run(
        run_id=run_id,
        started_at_utc=now - timedelta(hours=started_hours_ago),
        completed_at_utc=(
            now - timedelta(hours=completed_hours_ago) if completed_hours_ago is not None else None
        ),
        discovery_window_start_utc=now - timedelta(hours=50),
        discovery_window_end_utc=now - timedelta(hours=2),
        status=status.value,
        dry_run=False,
        code_version="test",
        summary=summary,
    )


def test_health_requires_a_recent_successful_full_daily_run(
    settings, tmp_path, monkeypatch
) -> None:
    now = datetime(2026, 7, 25, 8, tzinfo=UTC)
    factory, plist_path = _healthy_runtime(settings, tmp_path, monkeypatch, now)
    with factory() as session:
        session.add(
            _run(
                "daily-success",
                now=now,
                status=RunStatus.SUCCESS,
                summary={"mode": "daily", "sources": {"reddit": "Successful"}},
            )
        )
        session.commit()

    report = health.check_health(settings, factory, now=now, plist_path=plist_path)

    assert report.healthy
    assert report.checks["latest_daily_run"] == "daily-success; status=Success"


def test_partial_or_non_daily_runs_do_not_make_daily_health_green(
    settings, tmp_path, monkeypatch
) -> None:
    now = datetime(2026, 7, 25, 8, tzinfo=UTC)
    factory, plist_path = _healthy_runtime(settings, tmp_path, monkeypatch, now)
    with factory() as session:
        session.add_all(
            [
                _run(
                    "old-daily",
                    now=now,
                    status=RunStatus.SUCCESS,
                    summary={"mode": "daily"},
                    started_hours_ago=4,
                    completed_hours_ago=3,
                ),
                _run(
                    "manual-value",
                    now=now,
                    status=RunStatus.SUCCESS,
                    summary={"mode": "valuation"},
                    started_hours_ago=1,
                    completed_hours_ago=0.5,
                ),
                _run(
                    "daily-partial",
                    now=now,
                    status=RunStatus.PARTIAL,
                    summary={"mode": "daily", "sources": {"reddit": "Failed"}},
                    started_hours_ago=2,
                    completed_hours_ago=1,
                ),
            ]
        )
        session.commit()

    report = health.check_health(settings, factory, now=now, plist_path=plist_path)

    assert not report.healthy
    assert report.checks["latest_daily_run"] == "daily-partial; status=Partial"
    assert any("status is Partial" in warning for warning in report.warnings)


def test_health_detects_stuck_runs_and_stale_exports(settings, tmp_path, monkeypatch) -> None:
    now = datetime(2026, 7, 25, 8, tzinfo=UTC)
    factory, plist_path = _healthy_runtime(settings, tmp_path, monkeypatch, now)
    with factory() as session:
        session.add_all(
            [
                _run(
                    "daily-success",
                    now=now,
                    status=RunStatus.SUCCESS,
                    summary={"mode": "daily"},
                ),
                _run(
                    "stuck",
                    now=now,
                    status=RunStatus.RUNNING,
                    summary={},
                    started_hours_ago=2,
                    completed_hours_ago=None,
                ),
            ]
        )
        session.commit()
    export = settings.paths.exports / "watch_listings_latest.csv"
    old = now - timedelta(hours=48)
    os.utime(export, (old.timestamp(), old.timestamp()))

    report = health.check_health(settings, factory, now=now, plist_path=plist_path)

    assert not report.healthy
    assert report.checks["stuck_run"] == "stuck"
    assert any("CSV export is stale" in warning for warning in report.warnings)


def test_health_is_red_while_a_compliance_erasure_is_pending(
    settings, tmp_path, monkeypatch
) -> None:
    now = datetime(2026, 7, 25, 8, tzinfo=UTC)
    factory, plist_path = _healthy_runtime(settings, tmp_path, monkeypatch, now)
    with factory() as session:
        run = _run(
            "daily-success",
            now=now,
            status=RunStatus.SUCCESS,
            summary={"mode": "daily"},
        )
        session.add(run)
        session.flush()
        session.add(
            ErasureEvent(
                source="reddit",
                source_object_id="pending-post",
                event_type="reddit_content_deletion",
                detected_run_id=run.run_id,
                detected_at_utc=now,
                status="Pending",
                logical_purge_completed_at_utc=now,
            )
        )
        session.commit()

    report = health.check_health(settings, factory, now=now, plist_path=plist_path)

    assert not report.healthy
    assert report.checks["pending_erasure_events"] == "present"
    assert any("compliance erasure is incomplete" in warning for warning in report.warnings)


def test_scheduled_guard_only_accepts_success_or_all_permission_required() -> None:
    readiness = "ready-v1"
    assert _scheduled_run_satisfies_guard(
        SimpleNamespace(
            status=RunStatus.SUCCESS.value,
            summary={"mode": "daily", "source_readiness_fingerprint": readiness},
        ),
        readiness,
    )
    assert _scheduled_run_satisfies_guard(
        SimpleNamespace(
            status=RunStatus.PARTIAL.value,
            summary={
                "mode": "daily",
                "source_readiness_fingerprint": readiness,
                "sources": {
                    "reddit": "PermissionRequired",
                    "chrono24": "PermissionRequired",
                },
            },
        ),
        readiness,
    )
    assert not _scheduled_run_satisfies_guard(
        SimpleNamespace(
            status=RunStatus.PARTIAL.value,
            summary={
                "mode": "daily",
                "source_readiness_fingerprint": readiness,
                "sources": {"reddit": "Successful", "chrono24": "PermissionRequired"},
            },
        ),
        readiness,
    )
    assert not _scheduled_run_satisfies_guard(
        SimpleNamespace(
            status=RunStatus.PARTIAL.value,
            summary={
                "mode": "daily",
                "source_readiness_fingerprint": readiness,
                "sources": {"reddit": "Failed"},
            },
        ),
        readiness,
    )
    assert not _scheduled_run_satisfies_guard(
        SimpleNamespace(
            status=RunStatus.SUCCESS.value,
            summary={"mode": "daily", "source_readiness_fingerprint": "changed"},
        ),
        readiness,
    )


def test_scheduler_health_requires_catchup_and_private_log_settings(
    settings, tmp_path, monkeypatch
) -> None:
    now = datetime(2026, 7, 25, 8, tzinfo=UTC)
    plist_path = _scheduler_plist(settings, tmp_path)
    payload = plistlib.loads(plist_path.read_bytes())
    payload["StartInterval"] = 600
    payload["StandardErrorPath"] = str(tmp_path / "unbounded.log")
    payload["Umask"] = 0o022
    plist_path.write_bytes(plistlib.dumps(payload))
    monkeypatch.setattr(health, "system_timezone_name", lambda: settings.application.timezone)
    monkeypatch.setattr(
        health,
        "launchd_runtime_status",
        lambda: {
            "supported": True,
            "loaded": True,
            "domain": f"gui/501/{health.SCHEDULER_LABEL}",
            "state": "not running",
            "last_exit_code": 0,
            "message": "loaded",
        },
    )

    report = health._scheduler_health(settings, now=now, plist_path=plist_path)

    assert not report.healthy
    assert any("catch-up interval" in warning for warning in report.warnings)
    assert any("stdout/stderr" in warning for warning in report.warnings)
    assert any("mode-077" in warning for warning in report.warnings)


def test_health_does_not_create_a_missing_database(settings, tmp_path, monkeypatch) -> None:
    now = datetime(2026, 7, 25, 8, tzinfo=UTC)
    engine = create_sqlite_engine(settings.paths.database)
    factory = make_session_factory(engine)
    plist_path = _scheduler_plist(settings, tmp_path)
    monkeypatch.setattr(health, "system_timezone_name", lambda: settings.application.timezone)
    monkeypatch.setattr(
        health,
        "launchd_runtime_status",
        lambda: {
            "supported": True,
            "loaded": True,
            "domain": f"gui/501/{health.SCHEDULER_LABEL}",
            "state": "not running",
            "last_exit_code": 0,
            "message": "loaded",
        },
    )

    report = health.check_health(settings, factory, now=now, plist_path=plist_path)

    assert not report.healthy
    assert report.checks["schema"] == "Skipped because the database does not exist"
    assert not settings.paths.database.exists()


def test_launchd_runtime_status_parses_loaded_service(monkeypatch) -> None:
    monkeypatch.setattr(health.sys, "platform", "darwin")
    monkeypatch.setattr(
        health.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout="state = not running\nlast exit code = 2\n",
            stderr="",
        ),
    )

    result = health.launchd_runtime_status()

    assert result["loaded"] is True
    assert result["state"] == "not running"
    assert result["last_exit_code"] == 2
