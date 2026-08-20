# Changelog

This project follows [Semantic Versioning](https://semver.org/).

## [1.0.0-rc.1] - Unreleased

This consolidated release candidate implements the previously unpublished v0.2.0
through v1.0.0 roadmap line. Stable publication remains gated on signed native
artifacts, clean-machine lifecycle tests, a two-candidate updater exercise,
independent review, real-API reconciliation, and native three-platform soak evidence.

### Added

- Versioned config, catalog, database, export, loopback API, update-manifest, and
  compatibility contracts with strict validators and migration fixtures.
- Profile-scoped SQLite history, resumable/reconciled collection slices, project
  filtering, 30/365-day trends, completeness states, retention preview/apply,
  backup, restore, integrity checks, and privacy-preserving CSV/JSON exports.
- Multi-profile native credential ownership, duplicate-credential prevention,
  per-profile scheduler state, threshold alerts, persistent deduplication,
  notification history, tray lifecycle, startup, and single-instance activation.
- English and Traditional Chinese resources, runtime locale switching, responsive
  dashboard/profile/history/alert/settings/update views, keyboard semantics,
  reduced-motion support, and accessible incomplete-data trend rendering.
- Ed25519/JCS update metadata, key rotation, expiry/channel/downgrade policy,
  bounded HTTPS download, explicit download/install consent, crash journal,
  health check, rollback, and manual-recovery states. Linux AppImage installation
  is atomic; unsupported Windows/macOS in-process self-replacement fails closed.
- Native Windows, macOS x86_64/arm64, and Linux packaging orchestration; installer,
  DMG, tar, and AppImage assets; signed-release metadata; SBOM, dependency,
  license, malware, quality, and reproducible candidate evidence workflows.

### Changed

- Hardened the OpenAI client to exact organization Usage/Costs paths, rejected
  redirects, bounded response bodies, complete cursor handling, and stable safe
  errors without credential or upstream-body disclosure.
- Updated the model catalog against first-party OpenAI model documentation on
  2026-08-19 and separated quota/eligibility policy from public list pricing.
- Replaced the prototype one-time dashboard composition with a local-first desktop
  runtime while preserving the memory-only query path.

### Fixed

- Reconciled corrected upstream slices so rows removed by the source do not remain
  as stale over-counts; normalized nullable dimensions before uniqueness checks.
- Made migrations backup-first and retryable, isolated all persisted state by
  profile, and rejected stale active-profile generations after slow requests.
- Accounted for modern Usage cache-write breakdown without double-counting the
  inclusive `input_tokens` total, while flagging list-price estimates as incomplete.
- Kept the manifest compatibility floor independent from the target version so
  RC1 can discover RC2, made beta pointer retries byte-idempotent, and required
  post-install health checks to match the signed target version before commit.

### Security

- Added native secret-store adapters with fail-closed capability detection,
  memory-only key transfer, sanitized diagnostics, safe deep links, strict Host and
  Origin checks, bounded JSON, no-store responses, and repository/history audits.
- Added fail-closed Windows Authenticode, macOS Developer ID/notarization, Linux
  OpenPGP, Ed25519 update signing, immutable candidate reuse, and protected-tag
  publication gates without committing private keys or placeholder trust roots.

## [0.1.0] - Unreleased

### Added

- UTC-day Usage and Costs aggregation with complete cursor pagination.
- Explicit complimentary service-tier classification, cached-input accounting, unknown-model handling, and separate other-usage totals.
- Five UI query states, partial Costs success, accessible pricing tooltips, request IDs, and responsive quota cards.
- Local source and packaged resource smoke tests, repository secret/control-character audit, and Windows checksum generation.

### Changed

- Centralized the `0.1.0` version in the Python package and wired it to package metadata, CLI, API, UI, User-Agent, build, and release checks.
- Updated Standard catalog pricing from the official OpenAI pricing page as verified on 2026-07-31.
- Made Costs API failure non-fatal when Usage data is valid.
- Made the Windows build fail fast and validate the generated one-file executable.

### Fixed

- Replaced raw browser `Failed to fetch` messages with local-server troubleshooting guidance.
- Made Windows run/build scripts prefer a repository `.venv` before falling back to `py -3` or `python`.
- Repaired the dashboard JavaScript syntax error and tracked text control characters.
- Resolved bundled `web/` and `data/` paths through PyInstaller's one-file extraction root.
- Prevented duplicate or malformed pagination cursors and invalid upstream schemas from producing misleading totals.

### Security

- Restricted serving to loopback with Host checks, path-traversal rejection, no-store and browser-hardening headers, sanitized error envelopes, and safe request logging.
- Added actionable handling for 401, 403, 429, upstream 5xx, timeout, offline/network, invalid JSON, and unsupported response shapes.
- Kept tests fixture-only and prevented key-shaped secrets or unexpected control characters from passing CI or release gates.

## [0.1.0-alpha.1] - 2026-07-30

### Added

- Initial local dashboard prototype, model catalog, Usage/Costs integration skeleton, Python package, and Windows build workflow.
