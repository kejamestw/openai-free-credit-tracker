# Configuration reference (schema v1)

Configuration is non-sensitive UTF-8 JSON written by atomic replace with a last-known-good backup. Unknown optional fields round-trip. Invalid/future-schema or secret-like content is not loaded; the original is preserved for diagnosis. Admin API Keys never belong in config.

| Field | Default | Constraint / sensitivity | Application |
|---|---:|---|---|
| `schema_version` | `1` | Must be 1; non-sensitive | Startup |
| `ui.language` | `zh-TW` | BCP-47; `zh-TW` and `en` are supported | Live |
| `ui.open_browser_on_start` | `true` | Boolean | Next startup |
| `network.request_timeout_seconds` | `45` | 5–300 | New requests |
| `updates.channel` | `beta` in prerelease builds; otherwise `stable` | `stable` or `beta` | Next check; UI reports restart semantics |
| `updates.check_on_start` | `true` | Boolean | Next startup |
| `history.retention_days` | `null` | `null` or 1–3650; preview deletion first | Live |
| `monitoring.enabled` | `false` | Requires a secure credential backend | Live |
| `monitoring.interval_seconds` | `900` | At least 300 | Live |
| `monitoring.freshness_threshold_seconds` | `1800` | At least the interval | Live |
| `profiles.active_profile_id` | `null` | Opaque local ID, never a key | Live |
| `startup.enabled` | `false` | Off by default; platform adapter managed | Live or safe rollback |

Run `openai-free-credit-tracker --config-path` for the exact location. Windows uses Roaming AppData for config and Local AppData for data/cache/logs; macOS uses Application Support/Caches/Logs; Linux follows XDG config/data/cache/state. `OPENAI_CREDIT_TRACKER_{CONFIG,DATA,CACHE,LOG}_DIR` accepts absolute managed overrides; relative paths are rejected.

On corruption, the app uses a valid backup or safe defaults and preserves the original damaged file. The UI reports the source, warning, and path.

The Settings UI can preview restoring defaults. It lists every editable field that
would change and requires a second confirmation before the existing atomic config
replace is used. This operation preserves the active profile selection, unknown
forward-compatible fields, native credentials, and the entire history database.
