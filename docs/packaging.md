# Packaging and release pipeline

Release artifacts are built once on native GitHub-hosted runners, verified there,
and uploaded as a candidate. The tag workflow does not rebuild anything: it only
publishes a successful signed candidate whose source commit is exactly the tag
commit and whose manifest version matches the tag. Prerelease SemVer tags consume
the `beta` candidate channel and become GitHub prereleases; final versions consume
the `stable` channel.

## Supported artifacts

| Platform | Native runner | Outputs |
| --- | --- | --- |
| Windows x86_64 | `windows-2022` | Portable `.exe`, per-user Inno Setup `.exe` |
| macOS x86_64 | `macos-15-intel` | `.app.zip`, `.dmg` |
| macOS arm64 | `macos-15` | `.app.zip`, `.dmg` |
| Linux x86_64 | `ubuntu-22.04` | `.tar.gz`, AppImage |

Every native build checks the embedded package version, bundled web/model/locale
resources, loopback-only smoke startup, SQLite migrations and integrity, CSV/JSON
exports, clean database/export handle shutdown, and importability of the packaged
tray, image, notification, and native event-loop modules. The import check does
not create a tray icon, request notification permission, or send a notification.
Each platform also emits a CycloneDX 1.6 SBOM. The aggregate contains
`SHA256SUMS.txt` and a versioned
`artifact-manifest.json` binding every file to its byte size, SHA-256, source
commit, package version, candidate channel, platform signing state, public signing
identities/fingerprint/key ID, and GitHub Actions provenance.

The native build jobs install `.[desktop]` before PyInstaller runs. Dynamic imports
for `desktop_notifier`, `pystray`, Pillow, the selected pystray backend, and the
macOS Rubicon event-loop backend are explicit PyInstaller inputs, including package
data and distribution metadata. Missing wheels, failed imports, or an unsupported
native backend fail the build instead of silently producing a foreground-only
package.

Stable and beta packages contain the same generated `data/update-trust.json`. The
file contains fixed URLs for both channels and is created independently on every
native runner from the protected Ed25519 public keyring, then validated by the
packaged import smoke. Runtime config selects exactly one URL; prerelease builds
default to beta and final builds default to stable, while an explicit user choice
remains honored. The private seed is never available to a native build. An unsigned
`candidate` build must not contain this resource and its updater is explicitly
unavailable.

| Surface | Supported and verified runtime |
| --- | --- |
| Source/core CI | CPython 3.10 and 3.14 on Windows, macOS, and Linux |
| Release packaging and publication verification | CPython 3.13.13 on the native runners listed above |
| Desktop dependency contract | `desktop-notifier` 6.2.x (declared through Python 3.13), `pystray` 0.19.5.x, Pillow 10.4+ |
| Linux tray | Xorg backend is packaged; a graphical session plus desktop DBus is required, otherwise the documented foreground fail-closed mode is used |

Linux AppImage tooling is downloaded through GitHub's asset API by immutable asset
ID. `packaging/linux/toolchain.json` pins the upstream commit, expected byte size,
and SHA-256 for both `appimagetool` and its runtime. A mismatch fails the build
before either binary runs.

## Supply-chain and quality evidence

Ordinary CI installs the desktop dependency set on Python 3.13.13, runs the
dependency vulnerability audit, emits a CycloneDX SBOM and fail-closed license
inventory, and executes both the full synthetic 365-day/100-project performance
scenario and the accelerated 72-hour simulated soak. This is deterministic gate
evidence; it is not a substitute for the native clean-machine observation period.

The candidate workflow repeats these release gates and uploads versioned dependency
audit, license inventory, and quality evidence. After all native artifacts are
merged, the aggregate job installs ClamAV, requires a successful definition refresh
whose daily database is no more than 48 hours old, records the engine/signature
database version,
and scans every immutable distributable, platform SBOM, detached signature, update
manifest, and quality/license/audit evidence file. Scanner absence, definition
refresh failure, non-zero scanner error, a detection, or incomplete file/hash
coverage fails closed. The versioned malware report is then included in the signed
artifact manifest and `SHA256SUMS.txt`; the final metadata files are generated only
after the scan to avoid changing any scanned byte.

The tag workflow downloads this exact aggregate from a successful beta or stable candidate
for the tag commit. It verifies the evidence coverage, hashes, signatures, source
commit, version, and tag, but deliberately does not rebuild, regenerate evidence,
or rescan into a different artifact set.

## Candidate before tag

1. Run **Build release candidate** manually for the intended commit with channel
   `candidate`. This proves unsigned packaging mechanics but is never publishable.
2. Configure every secret in the protected `release-stable` GitHub environment and
   rerun the same commit with channel `beta` for a prerelease version such as
   `1.0.0-rc.1`, or `stable` for a final version. Both signed modes fail closed if
   any platform or update-manifest signing
   group is absent or incomplete. Windows portable and installer executables are
   Authenticode SHA-256 signed and timestamped, then checked by both `signtool` and
   `Get-AuthenticodeSignature`. Both the macOS `.app` and DMG are Developer ID
   signed, notarized, stapled, and assessed; CI also mounts the final DMG and reruns
   packaged checks against its contained app. Linux tarball and AppImage receive
   armored detached OpenPGP signatures that are immediately verified.
3. Download the aggregate candidate, independently inspect it, and verify it with:

   ```text
   python scripts/release_metadata.py verify --directory <candidate> --version <version> --source-commit <40-char-sha> --channel <beta-or-stable>
   ```

4. Only after that candidate succeeds, create and push `v<package-version>` at the
   exact same commit. **Publish verified release** downloads that exact candidate,
   rechecks all hashes/version/source/channel/tag bindings, then creates the GitHub
   prerelease or final Release. A missing or mismatched candidate stops before
   publication. No artifact, evidence file, checksum, or signature is regenerated.
   The publish job is retry-safe: a new Release starts as a draft, and every
   existing draft asset must match the candidate before only missing filenames are
   uploaded without `--clobber`. The draft becomes public only after a complete
   filename, size, and SHA-256 comparison passes. An already-published Release is
   never mutated and must compare exactly. This also allows a transient beta-pointer
   failure to be retried without replacing Release bytes.

Required protected macOS secrets are `MACOS_CERTIFICATE_P12` (base64 PKCS#12),
`MACOS_CERTIFICATE_PASSWORD`, `MACOS_SIGNING_IDENTITY`, `APPLE_API_KEY_P8`,
`APPLE_API_KEY_ID`, and `APPLE_API_ISSUER`. They are written only to an ephemeral
runner keychain/files and removed in an always-run cleanup step.

Windows requires `WINDOWS_CERTIFICATE_PFX` (base64 PKCS#12),
`WINDOWS_CERTIFICATE_PASSWORD`, `WINDOWS_SIGNING_IDENTITY`, and an HTTPS
`WINDOWS_TIMESTAMP_URL`. The certificate is imported only into the ephemeral
runner's current-user store and removed after packaging. Linux requires
`LINUX_GPG_PRIVATE_KEY_B64`, `LINUX_GPG_PASSPHRASE`,
`LINUX_GPG_PUBLIC_KEY_B64`, and `LINUX_GPG_KEY_FINGERPRINT`; the passphrase is
provided to GnuPG through stdin, and the full public fingerprint is recorded in
`artifact-manifest.json`, not hard-coded in source. Its temporary GnuPG home is
removed after signing or verification.

The updater manifest requires a raw 32-byte Ed25519 seed encoded in
`UPDATE_SIGNING_KEY_B64`, plus `UPDATE_SIGNING_KEY_ID` and the matching raw public
key in `UPDATE_SIGNING_PUBLIC_KEY_B64`. The private seed is mapped to the offline
signer's protected environment input and never appears in argv, logs, artifacts,
or repository files. Beta and stable candidates sign and immediately verify the
formal update manifest; the tag workflow independently verifies it again. An
ordinary candidate contains a clearly named `.unsigned.json` draft and signing
metadata with `unsigned` status, so it cannot pass stable publication.
The same Ed25519 key also signs `artifact-manifest.json` after its hashes,
platform signing evidence, and provenance are finalized; publication independently
verifies that signature before trusting the manifest.

For rotation, optional protected `UPDATE_TRUST_PUBLIC_KEYS_JSON` contains a strict
object of one to four raw-public-key base64 values keyed by key ID. It must include
`UPDATE_SIGNING_KEY_ID` with bytes equal to `UPDATE_SIGNING_PUBLIC_KEY_B64`. Bundle
the next public key for at least one supported release before switching the active
private key, and retain the previous public key for at least one supported release
afterward. Duplicate, invalid, empty, oversized, or active-key-mismatched keyrings
fail every signed native build; the active private seed remains singular/offline.

Stable packages use the fixed GitHub `releases/latest/download/` URL for the
byte-identical stable channel manifest asset. Beta packages use the fixed,
HTTPS-only signed pointer
`raw.githubusercontent.com/kejamestw/openai-free-credit-tracker/update-channels/beta.json`.
After a beta GitHub prerelease is published, the tag workflow verifies the prior
pointer and candidate signatures, requires the new SemVer to increase, and commits
the exact candidate manifest bytes to the dedicated `update-channels` branch with
the prior blob SHA as a compare-and-swap guard. Pointer history is retained in Git;
versioned prerelease assets are never replaced. This lets RC1 authenticate and
discover RC2 without treating mutable bytes as trusted by themselves.
If the pointer write succeeded but the runner lost its response, a rerun treats an
equal-version, byte-identical signed manifest as a successful no-op. An equal version
with different bytes and every lower version remain rejected.

For `1.0.0-rc.1`, release filenames and the product version remain the full SemVer.
Native metadata is normalized to each platform's grammar: Windows PE uses numeric
`1.0.0.0`, Inno Setup retains the display version but uses that numeric file
version, and macOS uses marketing version `1.0.0` plus bundle version `1.0.0fc1`.

## Local native commands

Install `requirements-build.txt` and the package desktop extra first. Use CPython
3.13.13 for release-equivalent local checks. These commands deliberately refuse to
cross-compile:

```text
python -m pip install -e ".[desktop]" -r requirements-build.txt
python scripts/build_release.py --platform windows --arch x86_64 --with-installer
python scripts/build_release.py --platform macos --arch arm64 --channel candidate
python scripts/build_release.py --platform linux --arch x86_64 --appimagetool <verified-tool> --appimage-runtime <verified-runtime>
```

The repository's automated contract tests validate the scripts and workflows on
all development platforms. Actual macOS signing/notarization and Linux AppImage
execution still require their native CI runners; a Windows-only local run is not
evidence that those native validations passed.
