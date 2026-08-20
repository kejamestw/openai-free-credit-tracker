# Compatibility and support policy

The project uses SemVer for application-owned contracts. Within v1.x, optional fields
and endpoints may be added; required fields cannot be removed or reinterpreted.
Breaking changes require a new major version and a documented migration/export path.

Public deprecations are documented for at least one minor release before removal.
Security or upstream emergency changes may shorten that period only when retaining
the behavior would create material harm. Supported releases and end-of-support dates
are listed in `SECURITY.md`.

Direct upgrades to v1.0 are tested from repository-owned v0.2, v0.3, v0.4, and
v0.5 contract snapshots. These fixtures describe unpublished pre-v1 contracts;
their names are not evidence that a public tag, installer, or release existed.
The checked matrix lives in `tests/fixtures/upgrade/matrix.json`:

- v0.2 represents config schema v1 before persistent history (SQLite schema 0).
- v0.3 and v0.4 intentionally alias one schema-v1 database lineage. Their config
  documents differ, but inventing separate database formats would misstate the
  implementation history.
- v0.5 represents config schema v1 with the current profile-aware SQLite schema 2.

Tests parse each config through the production consumer, materialize a working
copy of each static SQL snapshot, migrate it through the database service, and
query the resulting data. Fixture inputs and the matrix are hash-checked before
and after each test so migration never rewrites golden evidence. If direct
migration cannot be made safe, the release must provide a tested intermediate
upgrade or export/import path. Downgrade never writes to a newer database; update
rollback restores the pre-migration backup as a unit.
