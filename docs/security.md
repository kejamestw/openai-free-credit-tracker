# Security design and threat model

## Assets and trust boundaries

The primary sensitive asset is an Organization Owner Admin API key. Usage and Costs responses may also contain organization-sensitive operational or billing information.

The trusted path is the local application process, its bundled static files, the user's browser session, the operating system networking stack, TLS, and `api.openai.com`. The public internet, other LAN devices, unrelated local processes, browser extensions, copied logs/screenshots, downloaded executables, and repository content are outside that boundary.

## Implemented controls

- The server always binds to `127.0.0.1` with an operating-system-selected port; callers cannot configure a wildcard or LAN address.
- Requests must use a loopback `Host` value. DNS-rebinding-style Host values receive a sanitized `403` response.
- Static paths are percent-decoded, normalized below the bundled `web/` root, and reject `..`, NUL, backslash traversal, and paths resolving outside that root.
- The browser sends the key only in `X-Admin-Key`. The upstream client sends it only as the HTTPS `Authorization` header to the fixed `https://api.openai.com/v1` base URL.
- Both browser and server validate the `sk-admin-` key shape. A normal project key is rejected before any upstream request.
- The UI does not use localStorage, sessionStorage, IndexedDB, cookies, service workers, or configuration files. The server does not persist a key or API response.
- Responses use `Cache-Control: no-store` and restrictive CSP, referrer, framing, same-origin, and content-type headers.
- Public errors expose a stable code, actionable sanitized message, and request ID—not exception text or upstream bodies.
- Logs contain only request ID, HTTP method, event category, and status. They omit keys, URLs with data, Authorization headers, Project/Organization IDs, and API bodies.
- CI and release gates scan tracked UTF-8 text for unexpected control characters and key-shaped strings, and scan reachable Git patch history for key-shaped strings.
- Tests and fixtures build synthetic keys at runtime and use anonymized mock payloads. Automated tests never call OpenAI.

## Error behavior

Authentication (`401`), authorization (`403`), rate limit (`429`), upstream `5xx`, timeout, offline/TLS/network, invalid JSON, and unsupported schema failures map to safe error codes. Usage failure fails the query; Costs failure after Usage succeeds is a visible partial success.

## Residual risks

- A malicious local process or privileged user can access loopback traffic or application memory. Loopback binding is not a sandbox.
- A malicious or over-privileged browser extension may read the input field or page memory.
- Endpoint security products may inspect process memory, console output, or network metadata.
- The first v0.1.0 executable is not code-signed. SHA-256 detects accidental or post-publication mismatch only when the checksum itself is obtained from a trusted Release page; it does not replace signing.
- Model eligibility and complimentary rules are maintained by this project and can become stale. They do not grant credits or override OpenAI billing.
- A crash can leave ordinary OS diagnostic metadata. The application intentionally prints no key or raw upstream response, but clean-VM inspection remains a required manual acceptance case.

Use a dedicated, revocable Admin key for acceptance, close the application immediately afterward, and revoke the key if exposure is suspected. Do not use a production key on an untrusted machine.

## Release security checklist

Before publishing v0.1.0:

1. Run `python scripts\audit_repository.py` on a full-history checkout.
2. Run all automated tests and JavaScript checks.
3. Build through `scripts\build_windows.bat`; verify packaged version and smoke output.
4. Generate and independently compare the EXE SHA-256.
5. Perform the roadmap's clean Windows 10/11, key-lifecycle, loopback/LAN, path-traversal, and shutdown/restart acceptance cases.
6. Confirm the release tag, package/UI version, changelog, EXE, and checksum all agree.

Never place a real key or an unredacted OpenAI response in fixtures, issues, pull requests, logs, screenshots, or release artifacts.
