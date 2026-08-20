# App HTTP API v1

The application listens on an OS-selected port at `127.0.0.1`. It does not expose a
LAN or wildcard bind option. Public endpoints are under `/api/v1`; unversioned
v0.x endpoints are compatibility aliases and may be deprecated after the documented
support window.

## Endpoints

| Method | Endpoint | Contract |
|---|---|---|
| GET | `/api/v1/health` | Runtime version, API version, and readiness; no secrets. |
| GET | `/api/v1/catalog` | Validated catalog v1 plus runtime version and fallback warning. |
| GET | `/api/v1/config` | Effective non-sensitive config, server-authoritative defaults, and restart-required fields. |
| PUT | `/api/v1/config` | Validate and atomically replace non-sensitive config. |
| GET | `/api/v1/data` | Current usage/costs; requires an in-memory Admin key header. |
| POST | `/api/v1/sync` | Sync the active profile; optional explicit UTC range or `days` (default 30). |
| GET | `/api/v1/history` | Date/profile/project-filtered daily records and completeness. |
| GET | `/api/v1/projects` | Profile-scoped `project_key`, safe display name, and bucket count only. |
| GET | `/api/v1/profiles` | Safe profile metadata; never credential material. |
| POST | `/api/v1/profiles` | Verify an Admin key capability, store it in the native credential store, and create metadata. |
| GET | `/api/v1/profiles/{profile_id}` | Read one safe profile metadata record. |
| PUT | `/api/v1/profiles/{profile_id}` | Update `display_name` and/or `enabled`. |
| DELETE | `/api/v1/profiles/{profile_id}` | Delete inactive metadata only when it has no history. |
| POST | `/api/v1/profiles/{profile_id}/activate` | Persist and activate an enabled profile. Body is `{}`. |
| PUT | `/api/v1/profiles/{profile_id}/credential` | Verify and replace the native credential. |
| DELETE | `/api/v1/profiles/{profile_id}/credential` | Remove the native credential and disable the profile. |
| POST | `/api/v1/export` | Return a profile-scoped CSV or JSON schema-v1 export. |
| GET | `/api/v1/alerts` | List profile-scoped alert rules. |
| POST | `/api/v1/alerts` | Create or replace a profile-scoped alert rule. |
| DELETE | `/api/v1/alerts/{rule_id}` | Delete a profile-scoped alert rule. |
| GET | `/api/v1/alerts/history` | Read sanitized profile-scoped notification history (limit 1-1000). |
| POST | `/api/v1/notifications/test` | Send a generic desktop test notification for the selected profile and record its sanitized result. |
| GET | `/api/v1/update/status` | Read the safe journal state, progress, authenticated release-notes URL, and recovery requirement. |
| GET/POST | `/api/v1/update/check` | GET fetches/authenticates read-only metadata; POST additionally prepares an available update. POST accepts no JSON body. |
| POST | `/api/v1/update/consent-download` | Record explicit consent bound to `version`; body is `{"version":"x.y.z","confirm":true}`. |
| POST | `/api/v1/update/download` | Start bounded download, verification, and staging; body is `{}`. |
| POST | `/api/v1/update/consent-install` | Record a separate install consent bound to `version`; body is `{"version":"x.y.z","confirm":true}`. |
| POST | `/api/v1/update/install` | Start install, health check, commit, or rollback; body is `{}`. |
| POST | `/api/v1/update/resume` | Recover an interrupted journal using the cached authenticated manifest; body is `{}`. |
| POST | `/api/v1/operations/retention/preview` | Preview profile-scoped deletion for `retention_days` (1–3650); returns cutoff, row count, and an opaque one-use token without deleting. |
| POST | `/api/v1/operations/retention/apply` | Apply exactly one preview with `preview_token` and `confirm: true`; stale/used tokens fail closed. |
| GET | `/api/v1/operations/integrity` | Run quick or full SQLite integrity checking. |
| POST | `/api/v1/operations/backup` | Create a verified backup in the managed data directory. Body is `{}`. |
| POST | `/api/v1/operations/restore` | Restore a managed backup with `backup_name` and `confirm: true`. |

Successful JSON responses contain `request_id` where useful. Errors always use:

```json
{"error":{"code":"stable_code","message":"localized-or-safe-message","request_id":"opaque-id","params":{}}}
```

`code` and parameter names are stable; human-readable text is not a consumer
contract. Responses use `Cache-Control: no-store`, reject untrusted Host/Origin and
cross-site fetch metadata, and do not enable credentialed CORS. Admin keys must not
appear in URLs, bodies returned to the browser, logs, diagnostics, or persistence.

Integer time query parameters use Unix seconds in UTC. JSON timestamps use RFC 3339
UTC. Unknown optional request fields are rejected on state-changing endpoints;
unknown optional response fields must be ignored by clients.

Request targets are limited to 4096 UTF-8 bytes, at most 16 query fields, and no
duplicate query fields. JSON bodies are limited to 65536 bytes, must be UTF-8
objects with unique keys and standard finite numbers, and reject unknown fields.
History and export ranges are limited to 366 days. Daily history boundaries are
`00:00:00Z`.

Profile identifiers are opaque local `prof_` values. Profile responses expose only
whether a credential is configured and whether verification established usage
capability or an authoritative opaque identity. They never expose credential
references, Admin keys, raw organization identity, or raw upstream project IDs.
The default credential verifier proves access to the organization usage endpoint
but records no organization identity because that endpoint is not authoritative
for identity. An injected verifier may return an opaque identity only when its
source is authoritative.

The config response's `defaults` object is non-sensitive and follows config schema
v1. A restore-defaults client must preview changed field paths and obtain a separate
confirmation before sending a complete `PUT /api/v1/config`. The bundled UI resets
only editable settings; it deliberately preserves `profiles.active_profile_id`,
forward-compatible unknown fields, native credentials, and all database content.

Test notifications use fixed translation keys and generic text. They never interpolate
the profile display name, credential reference, or Admin API Key. If the native adapter
or permission is unavailable, the endpoint returns `capability_unavailable`; failed
attempts are retained only as a sanitized notification-history status.

`POST /api/v1/sync` is restricted to the active profile. The runtime snapshots the
active-profile generation before a slow sync; if activation changes before it
finishes, the request returns `active_profile_changed` and the caller must discard
the response. Persisted slices remain scoped to the original profile. History,
alerts, and exports always carry an explicit resolved `profile_id`.

HTTP and operations-CLI exports accept only `project_id_policy` values `mask` and
`exclude`; raw project IDs are unavailable at these boundaries. Restore requires
explicit confirmation. The operations CLI additionally acquires the same offline
instance lock held by the dashboard before replacing the active database.

Update action requests never accept a URL, artifact name, hash, key, or filesystem
path. The server selects these only from an authenticated manifest and managed local
journal. Status responses omit artifact URLs, hashes, signatures, cache paths, and
exception text. Download and install are separate consents and each consent is bound
to the exact prepared version. Long-running work returns `202`; clients observe its
bounded progress through `GET /api/v1/update/status`.

Update status also exposes boolean action capabilities (`can_consent_download`,
`can_download`, `can_consent_install`, `can_install`, and `can_resume`) plus
`installation_available`. Clients must render actions from these capabilities rather
than infer support from a journal state. Unsupported Windows/macOS packages can
download and verify but fail closed for install and install recovery.

Retention configuration never schedules deletion by itself. A preview token is
process-local, opaque, bounded in count, profile-scoped, and consumed by one apply
attempt. Apply recounts the same cutoff inside the database transaction; any history
change after preview returns `retention_preview_stale` and deletes nothing. The UI
must display the reviewed cutoff and row count and obtain a separate irreversible
action confirmation.
