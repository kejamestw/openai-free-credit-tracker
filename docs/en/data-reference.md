# Data reference

Database schema v2 contains profiles, projects, usage buckets, collection runs/slices, alert rules, dedup state, and notification history. Every identity, unique constraint, and query is profile-scoped.

- Times are UTC epoch values; quota-day attribution is always UTC.
- Sync is divided into UTC day/slices. Only a complete slice is reconciled in one transaction, including deletion of rows that disappeared after an upstream correction.
- Failed pagination retains diagnostic checkpoint state, then safely refetches the slice so an uncommitted page is never marked complete.
- SQLite uses foreign keys, WAL, and a bounded busy timeout. Integrity failure stops writes and preserves the original.
- v1→v2 migration creates a consistent snapshot and SHA-256 metadata before writing; failure rolls back, retry is safe, and restore is verified.

Public data uses an installation-specific HMAC `project_key`. The raw Project ID is private, excluded from repr/progress/notifications/logs, and available only through an explicit raw export. Display names and identifiers are separate.

CSV and JSON export schema v1 use explicit UTC and stable fields. IDs are masked by default or can be excluded; raw IDs are opt-in. Text is protected from spreadsheet formula injection and output is atomically replaced. See the [export contract](../contracts/export-v1.md), [catalog contract](../contracts/catalog-v1.md), and [model sources](../model-pricing.md).
