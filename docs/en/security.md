# Security and privacy

The HTTP server binds only to a random IPv4 loopback port. It validates exact Host/port, Origin, and `Sec-Fetch-Site`, and rejects DNS rebinding, cross-site requests, traversal, and arbitrary URL/file deep links. Responses use no-store, CSP, nosniff, frame/referrer restrictions, and stable error envelopes.

A one-time key exists only in browser/application request memory. Background credentials may exist only in Windows Credential Manager, macOS Keychain, or Linux Secret Service; config/database store opaque references. With no secure backend the app fails closed. Keys must never enter URLs, argv, logs, exceptions, notifications, temp files, backups, exports, fixtures, or repository content.

Updates use canonical JSON and Ed25519, then verify artifact size and SHA-256. Least-privilege CI, commit-pinned Actions, dependency/secret/history audit, an SBOM, and artifact manifests are release gates. Production releases still require platform-appropriate signing; checksums do not replace a trusted signing origin.

See the full [threat model](../threat-model.md). Report vulnerabilities through GitHub Security private vulnerability reporting. Never put exploits, keys, raw API bodies, organization identifiers, or billing data in public issues. Revoke a suspected key immediately.
