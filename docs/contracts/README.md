# Stable application contracts

These documents define the tracker-owned v1 interfaces. They do not promise that
OpenAI models, prices, incentive programs, or upstream response fields will remain
unchanged. Implementations may add optional fields, but cannot remove or change the
meaning of existing v1 fields without publishing a new schema or API version.

- `api-v1.md`: loopback HTTP API and error policy.
- `config-v1.md`: non-sensitive configuration and migration policy.
- `database-v1.md`: SQLite ownership, migration, backup, and compatibility.
- `export-v1.md`: stable CSV and JSON export formats.
- `catalog-v1.md`: model catalog content and schema versioning.
- `update-manifest-v1.md`: signed release/update metadata.
- `compatibility.md`: SemVer, deprecation, and supported upgrade rules.

All timestamps are RFC 3339 UTC unless a contract explicitly says epoch seconds.
Unknown optional fields must be ignored. Missing or invalid required fields must
produce a stable application error code, never a raw Python exception.
