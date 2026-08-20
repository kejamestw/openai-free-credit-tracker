# Security Policy

## Supported versions

| Version | Status |
|---|---|
| 1.0.x | Supported after an approved v1.0.0 stable release |
| Release candidates / source snapshots | Testing only; not supported releases |
| 0.x | Unsupported after v1.0.0 is released |

No source commit, workflow artifact, draft Release, or unsigned candidate becomes a supported release merely because it can be downloaded.

## Report a vulnerability

Use **Report a vulnerability** on this repository's GitHub **Security** page. If private vulnerability reporting is unavailable, open a public issue containing no exploit or sensitive details and ask for a private contact channel.

Include the affected version, operating system, minimal reproduction, impact, and whether a credential may have been exposed. Never send the credential, raw API response, billing data, database, export, Organization ID, or Project ID. Revoke a possibly exposed key immediately.

## Security properties

- The local server binds only to a random IPv4 loopback port and validates exact Host/port, Origin, fetch-site, and path boundaries.
- One-time Admin API Keys remain in request memory. Background keys can be stored only in Windows Credential Manager, macOS Keychain, or Linux Secret Service; no plaintext fallback exists.
- Keys are prohibited from URLs, argv, logs, exceptions, notifications, config, SQLite, temp files, backups, exports, fixtures, and repository content.
- Every profile scopes credentials, projects, usage, sync state, alert rules, notification history, and exports.
- Config and database writes are atomic. Database migrations create a consistent checksummed backup and fail without destroying the previous database.
- Update manifests use canonical JSON and Ed25519. Artifacts are bounded and verified by size/SHA-256 before staged installation, health checks, commit, or rollback.
- CI uses least privilege, commit-pinned Actions, repository/history secret scans, dependency audit, and CycloneDX SBOM generation.

These controls do not sandbox a hostile local administrator, privileged process, browser extension, or compromised OS credential service. Platform signing and notarization must be verified for each stable artifact.

See [docs/en/security.md](docs/en/security.md), [docs/zh-TW/security.md](docs/zh-TW/security.md), and the full [threat model](docs/threat-model.md).
