# Operations

## Normal daily operation

The native operating-system scheduler invokes `watch-tracker scheduled-run`.
That command no-ops if a successful full daily run already exists for the
current configured local date. A partial run suppresses launch-at-login retry
only when every selected source is permission-gated; transient failures remain
retryable.

`StartCalendarInterval` follows the Mac's system timezone. The expected trigger
is midnight Pacific while the host timezone is `America/Los_Angeles`. If the Mac
is asleep, launchd normally coalesces the event until wake; a powered-off or
logged-out per-user LaunchAgent cannot execute at midnight. A 30-minute
`StartInterval` provides a second catch-up trigger after login, wake, or a
transient failure. The command's local-date and readiness guard makes those
extra invocations no-ops after the daily outcome is complete.

Launchd stdout and stderr go to `/dev/null`; the application writes structured,
daily-rotated, mode-`600` records to `logs/watch_tracker.jsonl`. This avoids
unbounded launchd capture files.

## Failure recovery

1. Run `watch-tracker healthcheck`.
2. Inspect `logs/watch_tracker.jsonl`.
3. Run `watch-tracker run --dry-run --source SOURCE`.
4. Fix credentials, permissions, feed format, or parser fixtures.
5. Run a normal manual pipeline and repeat the health check.

A failed source must not cause another source's data to be rolled back. Do not
change statuses based on an empty or failed discovery result.
Incomplete collection or analysis preserves the last known good ranked CSV and
report. This prevents a healthy-looking fresh timestamp from being placed on
stale market state.

Runs limited with `--source` are diagnostics and never replace the authoritative
full-market exports. Run the unfiltered daily pipeline after the source check
succeeds.

`healthcheck` intentionally remains unhealthy when no source is authorized,
even if SQLite, migrations, exports, and launchd are otherwise correct. That
state means the infrastructure is operational but collection is not ready.

## Backups

`watch-tracker backup` uses SQLite's online backup API, runs an integrity check,
atomically installs the result, and retains 30 daily backups. Validation opens
the completed backup in read-only immutable mode, so it cannot create or modify
SQLite WAL/SHM files. Temporary database sidecars are removed on success and on
failure. A later backup also removes recognizable orphan temporary backup
artifacts left by an interrupted older process.

To restore:

1. Stop the LaunchAgent and acquire the application lock.
2. Validate the chosen backup with `PRAGMA integrity_check`.
3. Preserve the current database as a quarantined file.
4. Restore the selected database atomically.
5. Apply reviewed migrations, run an integrity check, restart the agent, and run
   `watch-tracker healthcheck`.

Never copy a live WAL database blindly.

## Reddit deletion artifact reset

An explicit Reddit deletion/removal is a source-specific erasure event, not a
normal status update. The logical purge and a durable `erasure_events` row commit
in one transaction. Every later run resumes any incomplete event before making
an ordinary backup or collecting new records. Completion requires:

1. Invalidate any valuation/score whose comparable lineage used the erased
   listing and remove the source content and history.
2. Remove all tracker-owned files in the backup directory with
   `purge_backup_artifacts(...)`.
3. Remove all tracker-owned current, dated, report, and interrupted temporary
   exports with `purge_export_artifacts(...)`.
4. Enable SQLite secure deletion, truncate the WAL, run `VACUUM`, truncate the
   WAL again, verify integrity, and remove managed journal/WAL/SHM sidecars.
5. Regenerate sanitized exports with rankings withheld.
6. Create and validate a fresh backup from the scrubbed database.
7. Mark the outbox event complete only after those artifacts succeed, then
   checkpoint the harmless completion metadata.

Purge helpers inspect only direct children and recognize the application's
exact filename formats. They do not recursively delete, and they leave
unrelated files and matching directories untouched.

If physical scrubbing, backup, or export regeneration fails, do not restore an
older artifact: that could reintroduce deleted Reddit content. The event remains
pending, health stays red, standalone backup/export commands refuse to run, and
the next pipeline invocation resumes the workflow.

## Source authorization

The Reddit approval request should explicitly cover listing normalization,
historical storage, status and price extraction, deterministic scoring,
retention, deletion handling, and whether any third-party model will process
content. The first release uses deterministic parsing and does not send raw
Reddit content to an external model.

Chrono24 must remain disabled unless written permission or a licensed feed
specifically allows this collection and database use.

After obtaining access, edit the mode-`600` runtime
`config/secrets.env`:

```dotenv
WATCH_TRACKER_REDDIT_ENABLED=true
WATCH_TRACKER_REDDIT_ACCESS_APPROVED=true
WATCH_TRACKER_REDDIT_DELETION_CONTRACT_VERIFIED=true
WATCH_TRACKER_REDDIT_CLIENT_ID=...
WATCH_TRACKER_REDDIT_CLIENT_SECRET=...
WATCH_TRACKER_REDDIT_USERNAME=...

WATCH_TRACKER_CHRONO24_ENABLED=true
WATCH_TRACKER_CHRONO24_ACCESS_AUTHORIZED=true
WATCH_TRACKER_CHRONO24_FEED_PATH=~/Library/Application Support/WatchTracker/data/authorized/chrono24.jsonl
```

Keep an authorized unattended feed under Application Support or another
launchd-readable location.

## Deployment releases

The macOS installer builds and validates a complete release before stopping the
LaunchAgent. A release identity covers both the application wheel and
`uv.lock`; dependencies are exported from that frozen lock and installed into a
fresh inactive release directory with hashes verified. Virtualenv entrypoints
contain absolute interpreter paths, so the directory is created at its final
immutable path and marked `.incomplete` until the CLI and dependency graph pass
validation. Only the stable `current` symlink is switched atomically.

Published release directories are never reinstalled into or repaired in place.
The installer acquires the application lock only for configuration, migration,
and the atomic `current` symlink change. If a post-stop deployment step fails,
it attempts to bootstrap the previously installed service again.
