# Security Policy

## Supported versions

| Version | Status |
|---|---|
| 0.1.x | Supported after an approved v0.1.0 release |
| 0.1.0-alpha.x | Unsupported once v0.1.0 is released |

The feature branch and unreleased build artifacts are development candidates, not supported releases.

## Reporting a vulnerability

Do not disclose vulnerability details, Admin API keys, project keys, sensitive response bodies, billing data, or organization identifiers in a public issue.

Use **Report a vulnerability** on the repository's GitHub **Security** page. If private vulnerability reporting is unavailable, open a public issue containing no sensitive or exploit details and ask the maintainers for a private contact channel.

Include the affected version, operating system, minimal reproduction steps, impact, and whether a key may have been exposed. Revoke any potentially exposed key immediately; do not send the key itself.

## Security properties

- The local HTTP server binds only to `127.0.0.1`.
- The Admin API key is held in memory only and is not intentionally persisted.
- Keys never belong in URLs, logs, browser storage, configuration files, fixtures, or repository history.
- Static file requests are constrained to the bundled web root.
- Public error responses are sanitized and correlated by request ID.
- Release artifacts are accompanied by SHA-256 checksums; v0.1.0 artifacts are not code-signed.

See [docs/security.md](docs/security.md) for the full threat model, controls, limitations, and release checklist.
