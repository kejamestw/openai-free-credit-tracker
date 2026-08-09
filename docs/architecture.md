# Architecture

## v0.1.0 boundaries

v0.1.0 is a single-user, local-first Windows application. It contains a static browser UI, a Python loopback HTTP server, and an outbound client for two OpenAI organization Admin API endpoints. It has no database, account system, background agent, telemetry, updater, export feature, or cloud service.

```text
Browser UI
  │ GET /api/catalog
  │ GET /api/data + X-Admin-Key
  ▼
Python ThreadingHTTPServer (127.0.0.1:<random-port>)
  ├─ bundled web/ and data/models.json
  ├─ Usage aggregation and catalog-price estimation
  └─ OpenAI Admin client
       ├─ GET /v1/organization/usage/completions
       └─ GET /v1/organization/costs
```

## Components

- `src/quota_monitor/app.py` owns CLI parsing, resource smoke checks, server lifetime, and browser launch.
- `src/quota_monitor/server.py` owns loopback binding, Host validation, static routing, request IDs, security headers, and the public error envelope.
- `src/quota_monitor/openai_client.py` validates Admin key shape, performs HTTPS requests, paginates through service callers, and converts upstream/network failures into sanitized typed errors.
- `src/quota_monitor/usage_service.py` defines the UTC window and aggregates eligible service-tier usage, other usage, cached tokens, output tokens, unknown models, and catalog-price estimates.
- `src/quota_monitor/cost_service.py` aggregates Costs API pages. Its failure is intentionally non-fatal after Usage succeeds, producing a partial-success result.
- `src/quota_monitor/model_catalog.py` resolves source and PyInstaller resource paths, loads `data/models.json`, and resolves aliases or dated model names.
- `src/quota_monitor/version.py` is the package version source used by package metadata, CLI output, User-Agent, API catalog, UI, and release gates.
- `web/` is framework-free HTML, CSS, and JavaScript. `domain.js` contains testable pure presentation rules.

## Data flow and semantics

1. The browser loads `/api/catalog`; the server adds the package version at runtime.
2. The user submits an Admin key. JavaScript checks its shape and sends it only in the `X-Admin-Key` request header to `/api/data`.
3. The server validates the key again, then requests the current interval from `00:00 UTC` through the current UTC time.
4. Usage pagination uses `next_page` until exhausted and rejects absent, invalid, or repeated cursors.
5. Only exact known incentive-tier values are counted in complimentary quota groups. Default, missing, unknown, or ineligible-model usage is reported under `other_usage`.
6. Cached input remains part of total input and is separately tracked for price estimation; total quota usage is input plus output, avoiding double-counting cached tokens.
7. Known models receive a list-price estimate. Unknown models remain visible as unpriced token totals.
8. Costs pages are summed in USD. A Costs failure produces `costs.available = false` while preserving the completed Usage response.

Catalog-price estimates and Costs API actual totals are deliberately separate signals. Neither is presented as an official complimentary balance.

## Local HTTP contract

- `GET /api/catalog`: local catalog plus the runtime version.
- `GET /api/data`: aggregated Usage and Costs result; requires `X-Admin-Key`.
- Other existing paths: static files below the resolved `web/` root.
- Errors: `{"error":{"code":"…","message":"…","request_id":"…"}}`.
- Every response includes `X-Request-ID`, `Cache-Control: no-store`, CSP, anti-framing, no-referrer, same-origin resource policy, and MIME-sniffing protection.

The response and safe diagnostic log contain no Authorization header, Admin key, Project ID, Organization ID, upstream response body, or raw API URL.

## PyInstaller one-file layout

Source execution resolves resources from the repository root. A one-file PyInstaller process resolves them from `sys._MEIPASS`. The build includes `web/` and `data/`, and `--smoke-test` verifies every required static/catalog resource plus the `127.0.0.1` bind before exiting.

The executable is portable but not self-updating. Release workflow gates require a matching `v<package-version>` tag, complete automated tests, packaged smoke checks, and a generated SHA-256 file.
