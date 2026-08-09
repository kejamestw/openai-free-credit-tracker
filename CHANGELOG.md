# Changelog

This project follows [Semantic Versioning](https://semver.org/).

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
