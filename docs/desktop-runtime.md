# Desktop runtime

The desktop runtime owns the loopback server, tray, scheduler thread, native
notification adapter, activation signal, and single-instance lock. Core usage,
SQLite, export, and API contracts remain platform independent.

## Capability and fallback policy

The production desktop factory selects Windows Credential Manager, macOS
Keychain, or Linux Secret Service and a private file-based instance lock. It
never falls back to a plaintext credential file. Linux requires a graphical
session and a desktop DBus session before tray and notifications are advertised.
If tray startup or secure credential storage is unavailable, monitoring remains
paused and the loopback Dashboard opens in foreground mode.

Notification permission is requested at most once per runtime. A denied result
is retained as a capability state; subsequent threshold events are recorded as
suppressed and do not trigger another prompt. A denied notification permission
does not stop usage synchronization.

The optional GUI dependencies are installed with:

```text
python -m pip install ".[desktop]"
```

`pystray` provides the tray integration and its capabilities are checked at
runtime. `desktop-notifier` provides Windows Toast, macOS Notification Center,
and freedesktop.org notification backends. Their imports are optional and
isolated from core startup.

## Lifecycle and actions

The tray has matching actions on every supported platform:

- Open Dashboard.
- Sync now.
- Pause or resume monitoring.
- Enable or disable startup at sign-in.
- Open About.
- Exit.

A secondary launch cannot start another server. It writes a bounded local
activation signal and exits; the primary process consumes that signal and opens
its own current random-port Dashboard. Exit first pauses all schedulers, then
stops notification and tray backends, stops the server, clears activation state,
and releases the lock.

Each enabled profile has an independent monotonic scheduler and status. A global
non-blocking execution lock prevents overlap between manual and background
collections. Long sleep or resume gaps cause at most one catch-up run per
profile. Authentication failure or a missing credential stops only that
profile; retryable failures use the bounded scheduler backoff.

## Alerts and privacy

Alert crossings use SQLite-backed rules and UTC-day deduplication. Only complete,
fresh observations can emit notifications. Notification text is translated by
the locale catalog and contains a masked profile label, percentage, and no raw
project or organization identifier. The click target is an allowlisted internal
Dashboard route with validated UTC-day and pseudonymous project filters.

## Native verification status

Unit and contract tests inject fake credential, tray, notification, startup,
clock, activation, server, and synchronization boundaries. Native tests are
opt-in with `RUN_NATIVE_DESKTOP_TESTS=1` on isolated per-OS CI runners.

This repository has not established local macOS or Linux GUI acceptance evidence.
A stable release still requires signed/notarized macOS artifacts and real desktop
tests on the documented Windows, macOS, and Ubuntu support matrix.
