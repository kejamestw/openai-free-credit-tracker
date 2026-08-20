# Troubleshooting

## Startup or local page failures

- Use the `http://127.0.0.1:<port>` URL opened by the app, never a local HTML file, stale fixed port, or remote dev server.
- Check for an existing instance. A stale lock is recovered only after its owner is confirmed gone.
- `--smoke-test` checks bundled resources and loopback binding without creating or reading user data.
- `--config-path`, `--data-path`, and `--log-path` locate diagnostic files.

## 401 / 403 / 429 / 5xx / timeout

- 401: revalidate this profile's Admin Key; never substitute another profile's credential.
- 403: confirm Organization Owner permission and the correct loopback browser origin.
- 429: allow scheduler backoff; never lower the interval below 300 seconds.
- 5xx/timeout/offline: completed slices stay intact and the failed slice is safely refetched. Costs failure does not discard successful Usage.

## Credentials, tray, and notifications

Unlock Windows Credential Manager/macOS Keychain when access is denied. Linux requires a DBus Secret Service; without one, foreground one-time mode remains available and no plaintext fallback is used. Missing tray/notification capability does not disable core queries/history, and denied notification permission is not requested repeatedly.

## Database busy/corrupt or full disk

Do not delete the original database. Exit, preserve log/request ID, and run integrity. Corruption switches to read-only/stopped-write behavior; restore only after verifying backup SHA and integrity. After freeing disk space, check atomic temp/backup state before retrying.

For stale data or duplicate notifications, inspect last sync and incomplete slices. Stale data cannot send quota-safety alerts; dedup is persisted by profile/rule/group/project/UTC day. Share only anonymized diagnostics and version information.
