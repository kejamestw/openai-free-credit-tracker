# Security design summary

This document is the public implementation summary. See the versioned [threat model](threat-model.md), [English security guide](en/security.md), [繁體中文安全說明](zh-TW/security.md), and repository [reporting policy](../SECURITY.md).

## Boundaries and assets

Sensitive assets are Organization Admin API Keys, raw Organization/Project identifiers, Usage/Costs data, historical databases, exports, signing keys, and platform credentials. The app trusts its own process and bundled resources, the selected OS credential service, loopback networking, TLS to a fixed upstream/update host, and protected release infrastructure. LAN peers, arbitrary websites, browser extensions, unrelated local processes, copied logs, and unsigned downloads are outside that boundary.

## Implemented controls

- Loopback server: random `127.0.0.1` port, exact Host/port and browser Origin/fetch-site checks, no-store, CSP, no framing/referrer, nosniff, bounded bodies, strict JSON, traversal/symlink containment, and stable safe errors.
- Credentials: one-time keys are cleared after each request; background keys use only Windows Credential Manager, macOS Keychain, or Linux Secret Service. Config/database store opaque references and fail closed when the backend is missing or locked.
- Persistence: profile-scoped SQLite identities and constraints, transactional complete-slice reconciliation, WAL/foreign keys/busy timeout, integrity fail-stop, atomic config/export, and checksummed pre-migration snapshots.
- Privacy: install-specific HMAC project pseudonyms; raw IDs are private and excluded from normal UI/log/notification/export. Notifications use safe profile labels and validated local deep links.
- Scheduler and alerts: safe minimum interval, non-overlap, monotonic time, backoff, auth fail-stop, stale-data suppression, and persistent UTC-day deduplication.
- Updates: HTTPS allowlist after redirects, canonical signed manifest, Ed25519 keyring/rotation, channel/SemVer/expiry/platform validation, bounded download, disk/size/SHA-256 checks, explicit consent, staged replacement, health check, rollback, and crash journal.
- Supply chain: least-privilege workflows, full commit pins, repository/history secret scan, dependency audit, SBOM, checksums, artifact manifest, native platform builds, and fail-closed stable signing gates.

## Residual risks

A privileged local attacker can inspect application/browser memory or replace trusted components. A malicious extension can read a visible key field. A compromised OS credential service or release signing identity defeats its trust boundary. Upstream Usage/Costs can be delayed or change shape, and project-maintained eligibility/pricing can become stale. Notifications and exports can reveal operational information to people with desktop/file access. Platform signing, clean-VM integration, update rollback, malware scan, independent review, and 72-hour soak require real release evidence and cannot be proven by unit tests alone.

Use a dedicated revocable Admin Key, keep the OS and browser trusted, verify signed release provenance, choose private export destinations, and revoke a key immediately after suspected exposure.
