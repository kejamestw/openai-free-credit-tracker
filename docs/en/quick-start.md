# Quick start

OpenAI Free Credit Tracker is a local-first desktop tool. It presents OpenAI Admin Usage, Costs, and the project's model catalog as separate signals and never labels a list-price estimate as a bill.

## Install and make a first query

1. Download the artifact for your OS and architecture plus `SHA256SUMS.txt`, the SBOM, and signed manifest from a GitHub Release.
2. Compare SHA-256 first. On macOS also verify `codesign`/`spctl`; on Windows verify Authenticode when supplied. Never bypass Gatekeeper or an OS security warning.
3. Use only the random `http://127.0.0.1:<port>` URL opened by the app. Do not open `web/index.html` directly.
4. Enter an Organization Admin API Key for a one-time Dashboard query. It is used for that request and the field is cleared afterward.
5. For background sync, create a profile and explicitly consent to storing its key in Windows Credential Manager, macOS Keychain, or Linux Secret Service. Without a secure backend, only foreground one-time queries are allowed.

Run from source:

```powershell
python -m pip install -e .
python -m quota_monitor
python -m quota_monitor --version
python -m quota_monitor --smoke-test
```

`--no-browser` suppresses browser launch. `--config-path`, `--data-path`, and `--log-path` print paths without creating directories.

Quota days always run from `00:00 UTC` to the next `00:00 UTC`. Local formatting does not change ownership. Stale or incomplete data is marked and cannot trigger a misleading quota-safety notification. Project/profile IDs are pseudonymized by default; raw-ID export requires an explicit choice.

Continue with [configuration](config-reference.md), [data](data-reference.md), [operations](operations.md), and [troubleshooting](troubleshooting.md).
