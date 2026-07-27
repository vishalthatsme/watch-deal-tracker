# Architecture

The application is a single scheduled Python process with one SQLite writer.
It has five boundaries:

1. Source adapters return normalized listing candidates.
2. The repository resolves immutable identities and writes current state plus
   append-only observations, price changes, and status changes.
3. Valuation stores the comparable evidence used for each estimate.
4. Scoring stores versioned components, confidence, risk caps, and rationale.
5. Exporting creates replace-on-success CSV and Markdown views.

Each listing is ingested in a savepoint, each source commits independently, and
the outer run always finalizes success, partial, or failure state after its run
row exists. A malformed record therefore does not discard its valid siblings,
and a source failure does not roll back another source's work. Search-result
absence never changes an existing listing's state.

SQLite uses WAL, foreign keys, and a busy timeout. An application file lock
prevents concurrent manual and scheduled runs. SQLAlchemy keeps the persistence
boundary portable if a later multi-worker deployment warrants PostgreSQL.

The production runtime is separate from the source checkout. Wheel hashes name
and the frozen dependency lock jointly identify immutable releases; a validated
migration precedes an atomic symlink switch. The LaunchAgent points only at the
stable symlink and the Application Support database/configuration.

## Source adapters

`RedditOAuthSource` uses only approved read-only OAuth access. It paginates the
new feed to the exact cutoff and batches status/deletion checks. Deleted Reddit
content receives a source-specific purge because platform deletion requirements
override historical retention. It does not fetch comments. An approved endpoint
must have verified deletion semantics before collection can be enabled.

`Chrono24AuthorizedFeedSource` deliberately makes no network request. It accepts
authorized JSON or JSONL records and requires separate enabled and explicit
authorization flags. Feed timestamps require offsets, currency codes and money
are validated, and malformed records are isolated and reported.

## Reliability invariants

- `listing_uid` and `(source, source_listing_id)` prevent repeat daily inserts.
- Asking price, all-in cost, and disclosed final sale price remain separate.
- Unknown or inaccessible status never overwrites an explicit known status.
- No comparable evidence means no invented fair value and no ranked “deal.”
- Scores link to the exact valuation used, even when a valuation is reused.
- Explicit Reddit erasure creates a durable transactional outbox event, removes
  evidence lineage, physically scrubs SQLite/WAL bytes, and replaces managed
  backups and exports before the event can complete.
- Partial collection or analysis never replaces the last known good ranked
  export; erasure recovery may publish a sanitized, intentionally unranked view.
- Health is green only after a recent successful full daily run, fresh export,
  current schema, authorized source, no pending erasure, valid scheduler, and
  intact database.

## Future extension points

- Add licensed marketplace feeds as `SourceAdapter` implementations.
- Add external comparable providers without changing collection.
- Move the SQLAlchemy models to PostgreSQL when there are multiple writers.
- Add notification providers behind run-health events.
- Add a UI that reads current and historical tables without becoming a writer.
