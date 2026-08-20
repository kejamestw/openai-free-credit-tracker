# SQLite data and migration contract v1

All connections, pragmas, transactions, integrity checks, backups, and migrations go
through the database service. Foreign keys and a busy timeout are enabled. Journal
mode is chosen centrally for a local desktop workload.

Migrations are forward-only and recorded in `schema_migrations`. Before a migration
writes, the service creates a consistent SQLite backup with a version and SHA-256.
A failed/interrupted migration leaves the prior database usable and never silently
marks the migration complete. A newer unsupported schema opens with an actionable
error rather than being downgraded.

The automated migration failure matrix covers an interruption after backup,
backup permission failure, disk-full metadata publication, a read-only write path,
SQLite lock contention, invalid migration SQL, and retry after every failure. Each
case proves that the schema marker and v1 data remain intact before retry. A busy or
locked database is transient contention, not corruption: initialization fails with
a retryable migration error and does not place the database into corruption
read-only mode. Golden fixture SQL is always materialized into a temporary working
database and its source bytes are verified unchanged.

Native filesystem power-loss behavior, hardware I/O faults, and platform-specific
snapshot/restore semantics still require VM or hardware validation. Unit tests use
a migration checkpoint to simulate interruption at deterministic transaction
boundaries; production callers use the no-op checkpoint.

Usage uniqueness includes profile, UTC bucket range, normalized project key, model,
and service tier; nullable dimensions are normalized before insertion. A completed
API slice is reconciled transactionally so upstream corrections can update or remove
stale local rows. Partial pages are never marked complete. Integrity failure stops
writes, preserves the original file, and offers integrity, backup, restore, and export
operations.

Raw Admin keys and raw upstream response bodies are forbidden. Project identifiers
are represented by stable local pseudonyms unless the user explicitly exports them.
