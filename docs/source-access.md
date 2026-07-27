# Source access and compliance

Reviewed 2026-07-25.

## Chrono24

Chrono24's [Platform Terms](https://www.chrono24.com/info/agb.htm), sections
6.1–6.3, prohibit automated reading/search and using platform search data to
create a personal database without approval. The shipped adapter therefore
contains no Chrono24 page scraper. It accepts only a local feed covered by
written permission or a license for this use.

Keep `WATCH_TRACKER_CHRONO24_ENABLED` and
`WATCH_TRACKER_CHRONO24_ACCESS_AUTHORIZED` false until that access exists.
After written authorization, set both true and configure
`WATCH_TRACKER_CHRONO24_FEED_PATH`.

## Reddit

Reddit's [Responsible Builder
Policy](https://support.reddithelp.com/hc/en-us/articles/42728983564564-Responsible-Builder-Policy)
requires explicit approval before API access. Its [Data API
guidance](https://support.reddithelp.com/hc/en-us/articles/16160319875092-Reddit-Data-API-Wiki)
requires OAuth, an identifying User-Agent, rate-limit handling, and deletion
compliance.

The adapter enforces a configurable ceiling of 100 HTTP attempts per
invocation by default. OAuth token requests, API requests, and retry attempts
all consume that budget, and no request is sent after it is exhausted.

The approval request should describe:

- r/Watchexchange-only collection
- Rolling 48-hour discovery
- Normalized historical listing storage
- Asking-price, status, and disclosed sale-price extraction
- Deterministic valuation and deal scoring
- Retention and deleted-content purging
- Whether any third-party model will process content

The current implementation performs deterministic parsing and does not send raw
Reddit content to an external model. It also does not fetch or store Reddit
comments; sale status and disclosed sale price must come from the submission
title/body or another separately authorized, deletion-aware feed.

When the approved API reports that a submission was deleted or removed, source
content and derived price, valuation, and score evidence are purged from
SQLite through a durable event. Free pages and WAL/journal sidecars are
physically scrubbed, and the application discards every managed historical
backup and CSV/Markdown export before creating sanitized replacements. This
source-specific erasure rule overrides ordinary historical and backup
retention, including for a previously sold listing.

After approval, place OAuth settings in the runtime
`config/secrets.env`, set the file mode to `600`, and set
`WATCH_TRACKER_REDDIT_ENABLED=true` and
`WATCH_TRACKER_REDDIT_ACCESS_APPROVED=true`. Collection remains fail-closed
until the approved integration's response/deletion semantics have been
confirmed for this exact endpoint and
`WATCH_TRACKER_REDDIT_DELETION_CONTRACT_VERIFIED=true` is also set. Do not set
that flag based only on generic API documentation.

Do not substitute HTML scraping, RSS, unauthenticated JSON, mobile endpoints,
proxies, CAPTCHA solving, or browser automation when access is unavailable.

Raw Reddit API responses are not written to logs or the evidence directory.
Any future raw-evidence store or external sink must participate in the same
deletion workflow before it can be enabled.
