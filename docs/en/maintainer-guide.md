# Maintainer guide

Update `docs/contracts` and golden/consumer tests before changing behavior. v1.x may add optional config/API fields but must not remove or reinterpret existing fields. Database migrations are forward-only and require a consistent checksummed pre-write backup. Export/catalog schema changes require a new schema version; catalog content may update independently.

Upstream fixtures must remove keys, raw organization/project IDs, billing, and request metadata. Parser tests retain unknown optional fields. Pricing/eligibility/quota updates require source, verified/effective dates, and `validate_models.py`.

## Verification

```powershell
python -m pip install -e ".[desktop]" -r requirements-dev.txt -r requirements-build.txt
python scripts/validate_models.py
python scripts/validate_locales.py
python scripts/validate_contracts.py
python scripts/audit_repository.py
python scripts/audit_dependencies.py --output build/dependency-audit.json
python scripts/generate_sbom.py --output build/OpenAI-Free-Credit-Tracker.cdx.json
python scripts/inventory_licenses.py --output build/license-inventory.json --fail-on-unknown
python scripts/run_quality_harness.py all --days 365 --projects 100 --hours 72 --output build/quality-evidence.json
python -m pytest -q --basetemp build/pytest-release
node --check web/js/domain.js
node --check web/js/app.js
node --test tests/frontend_domain.test.cjs
```

## Release

1. Freeze features; accept only blocker/security/data-loss/compatibility/documentation fixes.
2. Build an unpublished candidate with workflow_dispatch before creating a tag.
3. Run tests and packaged smoke/resource/architecture/desktop-import checks on three native runners. Produce the vulnerability audit, fail-closed license inventory, platform SBOMs, the full 365-day/100-project performance result, and accelerated 72-hour simulated-soak evidence.
4. Apply the documented Windows/macOS/Linux signing policy. macOS requires hardened runtime and notarization; Linux and Windows trust roots must also be documented. Signed beta and stable jobs run in the protected `release-stable` environment and fail closed without production secrets. Beta packages bundle the protected public trust root and use the signed monotonic `update-channels/beta.json` pointer, allowing RC1 to discover RC2.
5. Complete install/upgrade/uninstall and credential/tray/notification/update rollback on clean Windows 10/11, supported macOS, and Ubuntu 22.04/24.04. Attach machine/time/log hashes and a 72-hour soak report.
6. Fix RC1 findings, publish RC2, exercise RC1-to-RC2 update/rollback, and retain an observation period with no P0/P1.
7. Let every signed beta or stable candidate aggregate refresh ClamAV definitions and fail closed while scanning the complete immutable artifact/evidence set. Retain the versioned malware report inside the signed manifest and checksum set.
8. Only after every gate has evidence, confirm tag/package/UI/manifest/assets agree, then create the protected signed tag and final Release. Publication must reuse the successful candidate from the exact tag commit and must not rebuild, regenerate evidence, or rescan different bytes.

For incidents, withdraw a channel manifest or release asset without rewriting a published tag. Preserve provenance, release a compatible patch, and rotate/revoke the update keyring immediately if signing material may be compromised.
