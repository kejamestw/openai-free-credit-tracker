# OpenAI Free Credit Tracker

> A local-first tool for OpenAI Admin Usage, complimentary quota, costs, history, and alerts.

![Python](https://img.shields.io/badge/Python-3.10--3.14-3776AB)
![License](https://img.shields.io/badge/License-Apache--2.0-green)
![Status](https://img.shields.io/badge/status-v1%20candidate-orange)

English | [繁體中文](README.md) · [Full docs](docs/en/quick-start.md) · [Stable contracts](docs/contracts/README.md) · [Roadmap](docs/roadmap/README.md)

## What it does

- Measures OpenAI complimentary usage on `00:00 UTC` boundaries while presenting Usage, actual Costs API data, and catalog list-price estimates as separate signals.
- Supports multiple profiles/projects, history sync, CSV/JSON export, retention, integrity checks, and recoverable migrations.
- Uses a non-overlapping scheduler with a safe minimum interval, sleep/resume handling, 429/5xx backoff, authentication fail-stop, and stale-data rules.
- Sends quota alerts only on new threshold crossings, with persistent profile/rule/group/project/UTC-day deduplication.
- Ships Traditional Chinese and English resources, keyboard navigation, dark mode, reduced motion, 200% scaling, and screen-reader labels.
- Shares one core and versioned App API across Windows, macOS, and Linux; platform paths, credentials, startup, tray, notifications, instance locking, and updates remain behind adapters.

This is an unofficial project and is not affiliated with or endorsed by OpenAI. Eligibility, models, prices, and API behavior can change. Use official OpenAI data for billing decisions.

## Secure by default

A one-time Admin API Key exists only in request memory and its field is cleared afterward. It never belongs in URLs, config, SQLite, browser storage, logs, exceptions, notifications, backups, or exports. Background sync requires explicit consent and an available OS credential backend; otherwise it fails closed.

The local server binds only to a random `127.0.0.1` port and validates exact Host/port, Origin, fetch site, and static paths. Public failures use safe envelopes and request IDs. Credentials, data, sync, alerts, and exports are profile-isolated.

Never post Admin API Keys, raw API bodies, Organization/Project IDs, or billing data in issues, screenshots, or Git. Revoke a suspected key immediately. See [Security](docs/en/security.md) and the [threat model](docs/threat-model.md).

## Run from source

Python 3.10–3.14 is supported:

```powershell
python -m pip install -e .
python -m quota_monitor
```

Windows can also run `scripts\run_windows.bat`. Useful diagnostics:

```powershell
python -m quota_monitor --version
python -m quota_monitor --smoke-test
python -m quota_monitor --no-browser
python -m quota_monitor --config-path --data-path --log-path
```

Use only the `http://127.0.0.1:<random-port>` opened by the app. Do not open `web/index.html` directly or serve it from a remote dev server.

## Packaging and releases

Candidate pipelines build on each native runner:

- Windows portable EXE and per-user installer.
- Native macOS `.app`/DMG.
- Linux tarball and AppImage.
- `SHA256SUMS.txt`, platform artifact manifest, CycloneDX SBOM, and a signed update manifest.

A stable Release remains gated on platform signing, macOS hardened runtime/notarization, clean-VM install/upgrade/rollback, a 72-hour three-platform soak, and independent security/documentation acceptance. No gate is marked complete without real evidence. Follow the [Quick Start](docs/en/quick-start.md) to verify official artifacts.

Local Windows builds:

```bat
scripts\build_windows.bat
scripts\build_installer_windows.bat
```

## Develop and verify

```powershell
python -m pip install -e . -r requirements-dev.txt
python scripts/validate_models.py
python scripts/validate_locales.py
python scripts/validate_contracts.py
python scripts/audit_repository.py
python scripts/audit_dependencies.py --output build/dependency-audit.json
python scripts/generate_sbom.py --output build/OpenAI-Free-Credit-Tracker.cdx.json
python -m pytest -q --basetemp build/pytest-local
node --check web/js/domain.js
node --check web/js/app.js
node --test tests/frontend_domain.test.cjs
```

CI runs core/contract/security tests on Windows, macOS, Linux, and Python 3.10/3.14. Release workflows use native runners, commit-pinned Actions, and separate candidate builds from tag publication.

## Documentation

- [Quick start](docs/en/quick-start.md)
- [Configuration](docs/en/config-reference.md)
- [Data and exports](docs/en/data-reference.md)
- [Backup, restore, update, and uninstall](docs/en/operations.md)
- [Troubleshooting](docs/en/troubleshooting.md)
- [Maintainer and release guide](docs/en/maintainer-guide.md)
- [Contributing](CONTRIBUTING.md) · [Vulnerability reporting](SECURITY.md)

## License

Apache License 2.0. See [LICENSE](LICENSE).
