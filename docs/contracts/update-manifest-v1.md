# Signed update manifest v1

Manifests use schema v1, RFC 8785-compatible canonical JSON for all non-signature
fields, and Ed25519 signatures identified by `key_id`. Signed fields include channel,
version, publication/expiry time, minimum updater version, release notes, critical
flag, and every artifact URL, OS, architecture, format, size, and SHA-256.

Only HTTPS URLs on an explicit host allowlist are accepted, including after redirects.
Stable channels reject prereleases; beta channels require an explicit prerelease.
Downgrades, expired manifests, unknown keys/schemas, unsigned data, oversized data,
insufficient disk space, and unsupported updater versions fail closed.

The release pipeline builds immutable artifacts first, computes size/hash, then signs
the canonical manifest using a protected secret or signing service. Private keys may
never enter the repository, artifacts, command line, or logs. Rotation overlaps old
and new public keys for at least one supported updater release. Published artifacts
are never replaced in place; repairs use a new version.

Automatic checking is opt-in and non-blocking. Download and install each require
explicit consent. Downloads use an application-cache temporary directory and a local
generated name, then verify signature, size, and hash before installation. Health
check failure restores both application files and the migration-safe data backup.

The packaged product obtains exact `manifest_urls` for both `stable` and `beta`,
exact host allowlists, and an Ed25519 public-key ring from an immutable bundled
`data/update-trust.json` schema-v1 resource. Runtime config selects only its matching
URL; prerelease builds default to beta and final builds to stable. Stable checks use
the fixed GitHub latest-final asset URL. Beta checks use a fixed
`raw.githubusercontent.com/.../update-channels/beta.json` pointer; its
contents remain untrusted until the bundled key authenticates the manifest. Beta
promotion is monotonic, compare-and-swap guarded, and copies the exact verified
candidate bytes while Git retains prior pointer revisions. Source builds, unsigned
candidate packages without that resource, unsupported package formats, and invalid
trust documents keep installation unavailable. No loopback API request may
override the source URL, redirect allowlist, artifact path, target path, or keyring.

The consolidated release line fixes `minimum_updater_version` at
`1.0.0-rc.1`, the first public package containing this updater contract. It is
not copied from the target release version; consequently RC1 can accept RC2 and
later schema-v1-compatible manifests. Raising this floor is an explicit
compatibility decision and requires corresponding consumer tests.

The keyring contains one to four strict key IDs and raw 32-byte Ed25519 public keys.
The active protected signing public key must be present and byte-equal. Rotation
bundles old and new keys together for at least one supported release before and
after the active-key switch. Duplicate IDs, unknown fields, invalid base64/length,
and oversized keyrings fail closed.

After a successful check, the authenticated manifest is persisted in the managed
update cache before its fingerprint is journaled. Every later consent, download,
stage, install, health-check, and resume operation re-authenticates that cached
manifest and matches its fingerprint to the journal. Status exposes only phase,
version/channel, byte progress, safe error code, critical flag, and the signed,
allowlisted release-notes URL. Interrupted download/install states remain observable;
resume discards partial downloads or rolls back interrupted installs. A failed
rollback enters `manual-recovery` and never silently commits.

Post-install health checks first require the new executable's bounded `--version`
output to exactly match the signed target version, then run the side-effect-free
packaged smoke/server-resource check. Either failure triggers rollback.

Production automatic installation is enabled only for a Linux AppImage, where the
running inode can be replaced atomically and the new path can be health-checked.
Windows frozen executables and macOS application bundles fail closed until a signed,
separately shipped helper with platform-native replacement and restart semantics is
available. They may check, download, and verify an update, but the runtime uses a
refusing installer boundary, exposes `installation_available: false`, and never
offers install or install-recovery actions in the UI. In particular, recovery cannot
fall back to an in-process replacement of the running Windows executable.
