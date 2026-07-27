# Data dictionary

## Core tables

- `runs`: one execution, its exact UTC window, version, outcome, and counts.
- `source_runs`: per-source outcome and error isolation for a run.
- `listings`: latest normalized state for one immutable `listing_uid`.
- `listing_aliases`: later-discovered source IDs without changing identity.
- `listing_observations`: versioned parsed snapshots and content hashes.
- `listing_status_history`: append-only explicit status transitions.
- `listing_price_history`: append-only asking-price and confirmed-sale events.
- `duplicate_groups`: cross-post/relisting groups, separate from listing identity.
- `comparables`: evidence records used by valuations.
- `valuations`: versioned fair-value ranges, confidence, and assumptions.
- `deal_scores`: versioned components, pre-cap/final score, risk, and action.
- `exchange_rates`: the conversion rate, effective date, provider, and retrieval
  timestamp retained for an observation.
- `erasure_events`: durable logical-purge, physical-scrub, and artifact-reset
  state that can resume after a crash.
- `collection_errors`: sanitized per-stage operational errors.

## Identity

`listing_uid` is immutable and unique. It uses `source:source_listing_id` when
available and a deterministic canonical-URL hash otherwise. Multi-watch Reddit
ads create offer IDs with stable item suffixes. `source_ad_id` links those offers
to the parent submission for refresh and deletion handling.

`duplicate_group_id` is nullable and groups distinct advertisements believed to
represent the same physical watch. It must not be inferred from reference and
price alone.

## Sale fields

`is_sold` records an explicitly observed sale. `latest_asking_price_*` remains
the last advertised amount. `sold_price_*` stays null unless the final
transaction amount is explicitly disclosed. `first_observed_sold_at_utc` is not
the same as the actual `sold_at_utc`.

CSV list-valued fields are JSON encoded. Missing values are blank/null, never
zero.

## Data handling and deletion

Normal listing history is append-only, and daily backups follow configured
retention. Explicit Reddit deletion/removal is the exception:

- the listing remains only as a minimal identity tombstone with a generic
  removed status;
- source text, seller fields, prices, observations, comparables, valuations,
  scores, and status evidence are cleared;
- comparable evidence IDs invalidate downstream valuations even when the erased
  listing was evidence for a different target;
- SQLite free pages and WAL/journal sidecars are physically scrubbed;
- all managed backups and exports are discarded; and
- a fresh validated backup and sanitized exports are generated before the
  durable erasure event completes.

The reset intentionally sacrifices older recovery points so a restore cannot
reintroduce deleted Reddit content. It does not apply to a merely inaccessible
listing, an empty search result, or an unverified sale marker.

When Reddit reports a deleted author but retained content, stored author fields
and historical observations carrying that identity are erased through the same
durable physical/artifact workflow.
