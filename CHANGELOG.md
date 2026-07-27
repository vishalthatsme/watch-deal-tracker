# Changelog

## 0.1.0 — 2026-07-25

- Initial local-first tracker, schema, source adapters, daily pipeline, scoring,
  exports, backups, health checks, tests, and macOS scheduling assets.
- Added migration 0003 with crash-resumable physical erasure, comparable
  lineage, deleted-author handling, and FX retrieval timestamps.
- Hardened multi-watch identity/reconciliation, cross-source deduplication,
  evidence-weighted scoring, last-known-good publication, and fail-closed source
  access.
- Staged immutable macOS releases from the wheel plus frozen lock, with
  midnight scheduling and 30-minute catch-up checks.
