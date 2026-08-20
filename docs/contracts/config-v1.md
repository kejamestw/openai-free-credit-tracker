# Configuration schema v1

The JSON object has `schema_version: 1` and these sections: `ui`, `network`,
`updates`, `history`, `monitoring`, and `profiles`. Missing optional fields receive
documented defaults. Unknown fields are preserved during a same-schema round trip,
but never executed as settings. Unknown schema versions are rejected without
overwriting the source file.

Configuration may contain language, browser launch preference, request timeout,
update channel/check preference, retention, monitoring interval, and credential
references. It must never contain an Admin API key, Authorization header, raw API
response, or unmasked organization/project display data.

Writes use a sibling temporary file, flush and file-system synchronization where
supported, then atomic replacement. Before replacement, the last valid config is
stored as one backup. Invalid JSON/type/range data is quarantined and the application
starts with safe defaults and an actionable warning. Migration and destructive reset
first create a backup and never modify the input fixture in place.
