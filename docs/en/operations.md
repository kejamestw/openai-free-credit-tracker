# Operations, backup, and recovery

Interpret last successful sync, failed slices, completeness, and freshness together. Use `--data-path` to locate data, but do not copy a live WAL database directly; use built-in backup/integrity/export operations or their App API equivalents.

## Backup and restore

1. Pause monitoring or invoke built-in backup. Capture config and a consistent SQLite Backup API snapshot.
2. Keep metadata containing schema from/to, UTC timestamp, and SHA-256 with the snapshot.
3. To restore, fully exit tray/server/scheduler and confirm no second instance. Back up the current state first.
4. Built-in restore validates SHA-256 and SQLite integrity before atomic replacement.
5. Restart, run integrity again, and compare profiles, projects, totals, alerts, and credential references. Credential secrets stay in the OS store and are not part of a DB snapshot.

Do not open a newer forward-only database using an old executable. Install a version supporting the schema or restore a complete compatible backup.

The live Web UI intentionally does not expose restore. A desktop scheduler may still
own the database, so UI confirmation alone cannot prove an offline replacement is
safe. Exit the app and use `openai-free-credit-tracker restore --source <backup>
--confirm-restore`; the operations command acquires the shared runtime lock and fails
closed while another instance is active. The UI only creates and reports managed
backup names.

## Update and rollback

Updates must pass HTTPS host allowlisting, Ed25519 manifest verification, channel/SemVer/expiry/platform policy, artifact size/SHA-256, disk-space checks, and user consent. The old artifact remains available until the new version passes health checks. A journal identifies interrupted stages so recovery cannot destroy both versions. Manifests never update config, database contents, or credentials.

For manual recovery, exit completely, retain journal/log/request ID, reinstall the previous verified version, and restore a compatible verified backup if needed.

Normal uninstall removes the app, shortcuts, and startup entry but preserves user data. Credential removal, history deletion, profile deletion, and “erase all data” are separate destructive actions requiring scope disclosure, backup/export offer, and confirmation.
