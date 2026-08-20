# Threat model

## Scope and assets

This local-first desktop application handles an Organization Owner Admin API key,
usage/cost metadata, local profile names, project pseudonyms, SQLite history,
configuration, exports, update metadata, and release artifacts. The operating-system
account, OS credential service, loopback browser boundary, CI/release pipeline, and
upstream OpenAI Admin API are separate trust boundaries.

The application does not protect data from an attacker who already controls the same
OS account or can read its process memory. It does aim to prevent accidental secret
persistence, LAN exposure, cross-site browser access, unsafe update execution, path
escape, common export injection, and supply-chain substitution.

## Threats and required controls

| Boundary | Threat | Controls |
|---|---|---|
| Browser → loopback | DNS rebinding, hostile Host/Origin, CSRF, CORS abuse | Bind only `127.0.0.1`; validate Host/Origin/fetch metadata; no credentialed CORS; reject unsupported methods; no-store and CSP headers. |
| Static files | Plain/encoded traversal, symlink/path disclosure, MIME confusion | Decode once, reject dot/backslash/NUL segments, resolve below bundled root, fixed public errors, nosniff. |
| Admin key | URL/log/config/DB/temp/crash/notification leakage | Memory or OS credential store only; never command-line/URL; allowlisted logging and redaction; no raw upstream body; explicit forget operation. |
| Upstream API | Schema drift, malicious optional fields, pagination loops, partial pages | Adapter boundary, typed validation, stable errors, opaque IDs, repeated-cursor detection, transactional slice completeness. |
| SQLite | Corruption, busy writer, partial migration, cross-profile reads | Central connection service, foreign keys, timeout/journal policy, migration table, pre-write backup/hash, profile-scoped keys/queries, integrity read-only failure. |
| Export | Formula injection, identifier disclosure, partial file | CSV text escaping, default masking, explicit inclusion choice, stable schema, atomic replacement. |
| Credential backend | Locked/unavailable service, wrong-profile key, plaintext fallback | Platform adapter contract, no plaintext fallback, disable background sync, per-profile credential reference, explicit consent/deletion. |
| Tray/notifications | Key/profile/project disclosure, malicious deep link, orphan process | Safe labels, masked identifiers, validated internal route parameters, single instance, deterministic shutdown. |
| Updater | Manifest/artifact tampering, downgrade, redirect, path injection, interrupted replace | Ed25519 signed canonical manifest, HTTPS host allowlist after redirect, SemVer/channel/minimum checks, generated temp names, size/hash/disk checks, backup/health check/rollback. |
| CI/release | Secret exposure, mutable artifacts, untrusted actions, tag mismatch | Least permissions, protected environments/tags, pinned actions, build-before-sign, immutable versioned assets, checksums, SBOM, provenance and malware scan. |

## Secret lifecycle

Manual foreground queries keep the Admin key only in the browser input and server
request handling lifetime. Background monitoring requires explicit consent and a
platform credential backend. Credential references are opaque local IDs; profile or
config records never contain the secret. A revoked/locked/missing credential disables
automatic collection and asks the user to re-enter it. “Forget key” deletes the OS
credential, stops related work, and clears application references; uninstall preserves
credentials unless the user separately confirms credential deletion.

The key is forbidden in application logs, exception messages, upstream response
copies, crash diagnostics, notifications, process arguments, update metadata,
temporary filenames, database backups, and exports. Automated scans use fake keys and
anonymous organization/project fixtures only.

## Data deletion and recovery

Migration, cleanup, profile deletion, and “remove all data” are distinct operations.
Each destructive operation previews scope and requires confirmation. Migration and
restore preserve a consistent backup; integrity failure stops writes and preserves the
original database. Update rollback restores application and compatible pre-migration
data together. Published artifacts and tags are immutable; a compromised or defective
release is withdrawn and superseded, never silently replaced.

## Residual risks and release gates

- An unsigned development artifact has no publisher identity; checksums alone only
  detect mismatch when obtained from a trusted release page.
- OS credential services and signing infrastructure require platform-specific real
  tests; fake adapter tests are not equivalent evidence.
- Loopback isolation does not defend against a malicious process already running as
  the same user. The API therefore exposes no persisted Admin key and minimizes local
  operations that accept untrusted input.
- Upstream eligibility and quota policy can change independently of catalog prices.
  The UI must retain the non-official disclaimer and source/completeness separation.
- Critical/high security findings block release. Time-bounded medium/low exceptions
  require an owner, expiry, mitigation, and a public-safe release note where useful.

Security changes require regression tests at the affected boundary and review against
`docs/security.md`, `SECURITY.md`, the update contract, and the release workflows.
