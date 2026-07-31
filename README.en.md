# OpenAI Free Credit Tracker

> A local-first dashboard for OpenAI complimentary daily tokens, eligible models, service tiers, and API costs.

[繁體中文](README.md) | English

## Features

- Tracks the Standard and Mini/Nano complimentary-token groups separately.
- Counts only Usage API rows explicitly marked as `incentivized-tier` or a known data-sharing incentive tier in the quota cards.
- Uses `00:00 UTC` as the daily boundary.
- Separates confirmed complimentary usage, other usage, catalog-price estimates, and actual Costs API totals.
- Keeps valid Usage data visible if the Costs API fails.
- Keeps the Admin API key in process and page memory only; it is never intentionally persisted.
- Binds the local HTTP server to a random port on `127.0.0.1` only.

## Important disclaimer

This is an unofficial community project and is not affiliated with or endorsed by OpenAI. Complimentary-token rules, eligible models, prices, and API behavior can change. Treat OpenAI's documentation and billing console as authoritative.

Never paste an Admin API key into a third-party site, public issue, screenshot, or Git commit. For manual acceptance, use a dedicated Organization Owner Admin API key that can be revoked immediately afterward.

## Windows portable executable

An approved release is expected to contain:

- `OpenAI-Free-Credit-Tracker.exe`
- `SHA256SUMS.txt`

Verify the download in PowerShell before running it:

```powershell
Get-FileHash .\OpenAI-Free-Credit-Tracker.exe -Algorithm SHA256
Get-Content .\SHA256SUMS.txt
```

After the values match, start the executable. The default browser opens `http://127.0.0.1:<random-port>`. Close the console window or press `Ctrl+C` to stop the server. A newly started process always requires the key to be entered again.

The v0.1.0 executable must still pass the clean Windows 10/11 manual acceptance cases in the roadmap before release.

## Run from source

Python 3.10 or newer is required:

```powershell
python -m pip install -e .
python -m quota_monitor
```

On Windows, `scripts\run_windows.bat` is also available.

Version and source-resource smoke checks:

```powershell
python -m quota_monitor --version
python -m quota_monitor --smoke-test
```

### Troubleshooting

If the UI shows "Query failed: cannot reach the local service", confirm that the Tracker EXE or command prompt window is still open and that you are using the `http://127.0.0.1:<random-port>` page opened by the app. Do not open `web/index.html` directly or serve the page from another dev server or remote URL.

## Build the Windows executable

```bat
scripts\build_windows.bat
```

The script installs project and development dependencies, builds a PyInstaller one-file executable, validates its bundled resources and loopback bind, and prints the packaged version. It exits nonzero if any step fails. The output is:

```text
dist\OpenAI-Free-Credit-Tracker.exe
```

## Security model

- The browser sends the key to the loopback server in `X-Admin-Key`; the server alone creates the upstream OpenAI `Authorization` header.
- The key is not placed in URLs, logs, localStorage, sessionStorage, IndexedDB, or configuration files.
- Every local HTTP response uses `Cache-Control: no-store` and restrictive browser security headers.
- Static files are served only from the bundled `web/` root; path-traversal requests are rejected.
- The application does not request or persist Project IDs or Organization IDs.

See [docs/security.md](docs/security.md) for trust boundaries and residual risks.

## Usage and cost semantics

The catalog-price estimate comes from `data/models.json`; it is not the same signal as the Costs API's actual total. The estimate is:

```text
cost =
  uncached input tokens × input price / 1,000,000
+ cached input tokens × cached-input price / 1,000,000
+ output tokens × output price / 1,000,000
```

Cost badges use a standard sample of 1,000 input plus 1,000 output tokens:

- Low: below US$0.003
- Medium: US$0.003 to below US$0.012
- High: US$0.012 or above

## Known limitations

- An Organization Owner Admin API key is required; a regular project key is not sufficient.
- Usage and Costs data may be delayed, and the calculated complimentary usage is not an official remaining-balance guarantee.
- v0.1.0 does not persist settings, history, exports, or keys.
- v0.1.0 targets Windows. macOS, Linux, auto-update, tray integration, alerts, multi-project workflows, and localization are out of scope.
- The primary metric covers Completions Usage; tools, fine-tuning, and Evals are not included in the complimentary group totals.

## Development validation

```powershell
python -m pip install -r requirements-dev.txt
python scripts\validate_models.py
python scripts\audit_repository.py
python -m pytest -q
node --check web\js\domain.js
node --check web\js\app.js
node --test tests\frontend_domain.test.cjs
```

Read [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), and [docs/security.md](docs/security.md) before contributing or reporting a vulnerability. Never attach keys, full sensitive responses, or billing data to a public issue.

## License

Apache License 2.0. See [LICENSE](LICENSE).
