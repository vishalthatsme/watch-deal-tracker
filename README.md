# Watch Deal Tracker

A local-first, scheduled collector and risk-adjusted deal analyst for selected
high-end watch listings. SQLite is the durable source of truth; CSV and Markdown
files are atomic exports.

## Current source-access posture

The application deliberately fails closed:

- Chrono24's platform terms prohibit automated reading/search and building a
  personal database from search data without permission. The Chrono24 adapter
  therefore reads only a local feed that the user is authorized or licensed to
  use and is disabled by default.
- Reddit currently requires explicit approval and registered OAuth access.
  Configure the Reddit adapter only after approval for this exact storage and
  analysis use case.

There is no HTML, RSS, unauthenticated JSON, proxy, CAPTCHA, or browser
workaround.

## Reddit API review summary

This repository is public so Reddit can review the exact implementation before
granting access. The application is private, single-user, non-commercial, and
read-only:

- It obtains an app-only OAuth token with `POST /api/v1/access_token`.
- It reads only `GET /r/Watchexchange/new` for rolling 48-hour discovery and
  `GET /r/Watchexchange/api/info` for stored submission refreshes.
- It runs at midnight Pacific, spaces requests by at least 1.5 seconds, honors
  rate-limit responses, and stops before exceeding 100 HTTP attempts in one
  invocation, including token requests and retries.
- It retains normalized listing identity, permalink, timestamp, title, up to
  700 characters of the relevant listing section, author/flair, asking price,
  status, and an explicitly disclosed sale price. It derives a deterministic,
  versioned valuation and score locally.
- It does not fetch comments or perform posts, comments, votes, messages,
  seller contact, user profiling, identity matching, redistribution, AI
  training, or external model processing.
- The only unrelated network integration is the Frankfurter currency service;
  it receives currency codes and dates, never Reddit content or identifiers.

Normalized listing history is retained for the approved market-research
purpose. Logs, ordinary evidence, and backups have 30-day limits. An explicit
Reddit content deletion/removal purges the submission, prices, author fields,
observations, comparables, valuations, and scores; securely scrubs SQLite and
its sidecars; and replaces every managed backup and export. A deleted author
triggers equivalent removal of author-identifying and seller-authored fields.
An omitted ID is not treated as proof of deletion without a confirmed endpoint
contract.

Collection stays fail-closed until both the use case and the exact deletion
semantics are approved. Relevant review entry points are the
[Reddit source adapter](src/watch_tracker/sources/reddit.py),
[repository erasure logic](src/watch_tracker/database/repository.py),
[physical erasure workflow](src/watch_tracker/database/secure_erasure.py),
[pipeline deletion tests](tests/integration/test_pipeline.py), and
[source-access policy](docs/source-access.md).

## Local setup

```bash
UV_CACHE_DIR=/private/tmp/watch-tracker-uv-cache \
UV_PYTHON_INSTALL_DIR=.python \
uv sync --python 3.12 --extra dev

.venv/bin/watch-tracker migrate
.venv/bin/pytest
.venv/bin/watch-tracker run --dry-run
```

Until at least one source is approved and configured, the dry run deliberately
returns a nonzero diagnostic status instead of pretending collection succeeded.

Copy `config/secrets.env.example` to `config/secrets.env` only when approved
source credentials are available, then set mode `600`. The application loads
that narrowly scoped file and rejects group/world-readable secret files. The
macOS scheduler never embeds credentials in its plist.

For an authorized Chrono24 feed, set
`WATCH_TRACKER_CHRONO24_ENABLED=true`,
`WATCH_TRACKER_CHRONO24_ACCESS_AUTHORIZED=true`, and the feed path. For Reddit,
leave `WATCH_TRACKER_REDDIT_ENABLED=true` and add the approval flag and OAuth
credentials only after Reddit approves this use and confirms the deletion
semantics; both `WATCH_TRACKER_REDDIT_ACCESS_APPROVED` and
`WATCH_TRACKER_REDDIT_DELETION_CONTRACT_VERIFIED` must then be true.

## Common commands

```bash
.venv/bin/watch-tracker migrate
.venv/bin/watch-tracker run
.venv/bin/watch-tracker run --dry-run
.venv/bin/watch-tracker run --source reddit
.venv/bin/watch-tracker discover
.venv/bin/watch-tracker refresh-status
.venv/bin/watch-tracker value
.venv/bin/watch-tracker backfill --hours 168
.venv/bin/watch-tracker export
.venv/bin/watch-tracker backup
.venv/bin/watch-tracker healthcheck
.venv/bin/watch-tracker scheduler-status
```

The daily job performs two passes:

1. Discover listings whose original post timestamp falls in the rolling 48-hour
   window.
2. Refresh stored listings. Reddit records also receive a deletion-compliance
   sweep.

An inaccessible page or failed source never becomes a sale. A final sale price
is stored only when explicitly disclosed.

## Data locations

- Database: `data/database/watch_market.sqlite`
- Latest full export: `data/exports/watch_listings_latest.csv`
- Active opportunities: `data/exports/watch_active_deals.csv`
- Sold records: `data/exports/watch_sales_history.csv`
- Dated reports: `data/exports/watch_deal_report_YYYY-MM-DD.md`
- Validated backups: `data/backups/`
- Structured logs: `logs/watch_tracker.jsonl`

See [architecture](docs/architecture.md), [operations](docs/operations.md),
[data dictionary](docs/data-dictionary.md), and
[scoring methodology](docs/scoring-methodology.md). Source permission and
credential requirements are documented in
[source access](docs/source-access.md).

## macOS deployment

The production LaunchAgent runs from
`~/Library/Application Support/WatchTracker`, not from Documents, to avoid
unattended macOS privacy failures. The reviewed installer builds and stages the
wheel, migrates the separate runtime database, validates the plist, and
registers the per-user agent:

```bash
operations/install_macos_runtime.sh
```

It fires at local midnight, at login, and on a 30-minute catch-up interval.
After a completed daily outcome, the local-date/readiness guard makes catch-up
invocations no-ops.
`watch-tracker scheduler-status` verifies the installed plist, host timezone,
and launchd state while computing the expected next Pacific trigger.

The installer uses immutable releases identified by both the wheel and
`uv.lock`, installs hash-verified frozen dependencies into a fresh staging
release that is kept inactive and marked incomplete, validates it before
stopping launchd, and changes the `current` symlink atomically. It updates the
runtime default configuration only when that file still matches the previously
distributed default; local customizations are preserved.

## License

The source is made public for platform/security review. It remains proprietary;
see [LICENSE](LICENSE).
