"""Build and verify one native platform's release artifacts."""

from __future__ import annotations

import argparse
import gzip
import importlib.util
import os
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

try:
    from scripts.generate_update_trust import (
        DEFAULT_REPOSITORY,
        build_document as build_update_trust_document,
        load_and_validate as load_update_trust,
        public_keyring_from_environment,
    )
except ModuleNotFoundError:  # Direct ``python scripts/...`` execution.
    from generate_update_trust import (  # type: ignore[no-redef]
        DEFAULT_REPOSITORY,
        build_document as build_update_trust_document,
        load_and_validate as load_update_trust,
        public_keyring_from_environment,
    )


ROOT = Path(__file__).resolve().parents[1]
APP_NAME = "OpenAI-Free-Credit-Tracker"
ENTRY_POINT = ROOT / "scripts" / "release_entry.py"
UPDATE_TRUST_PATH = ROOT / "data" / "update-trust.json"
SEMVER_PATTERN = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<prerelease>alpha|beta|rc)\.(?P<prerelease_number>[1-9]\d*))?"
    r"(?:\+[0-9A-Za-z.-]+)?$"
)


def package_version() -> str:
    sys.path.insert(0, str(ROOT / "src"))
    from quota_monitor.version import __version__

    return __version__


def normalized_host() -> tuple[str, str]:
    system = platform.system().lower()
    host_os = {"windows": "windows", "darwin": "macos", "linux": "linux"}.get(system)
    if host_os is None:
        raise RuntimeError(f"unsupported build host: {system}")
    machine = platform.machine().lower()
    host_arch = {
        "amd64": "x86_64",
        "x86_64": "x86_64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }.get(machine)
    if host_arch is None:
        raise RuntimeError(f"unsupported build architecture: {machine}")
    return host_os, host_arch


def require_native(target_os: str, target_arch: str) -> None:
    host = normalized_host()
    requested = (target_os, target_arch)
    if host != requested:
        raise RuntimeError(f"native build requires {requested[0]}/{requested[1]} runner; got {host[0]}/{host[1]}")


def run(command: list[str], *, env: dict[str, str] | None = None) -> None:
    subprocess.run(command, cwd=ROOT, env=env, check=True)


def _reset_directory(path: Path) -> None:
    resolved = path.resolve()
    build_root = (ROOT / "build" / "release").resolve()
    if build_root not in resolved.parents:
        raise RuntimeError(f"refusing to clean path outside release build root: {resolved}")
    shutil.rmtree(resolved, ignore_errors=True)
    resolved.mkdir(parents=True)


def _native_versions(version: str) -> tuple[str, str, str]:
    """Return Windows numeric, macOS marketing, and macOS build versions."""

    match = SEMVER_PATTERN.fullmatch(version)
    if match is None:
        raise ValueError("package version cannot be rendered as native version metadata")
    numbers = [int(match.group(name)) for name in ("major", "minor", "patch")]
    if any(number > 65_535 for number in numbers):
        raise ValueError("native version components cannot exceed 65535")
    windows_numeric = ".".join(str(number) for number in (*numbers, 0))
    macos_marketing = ".".join(str(number) for number in numbers)
    prerelease = match.group("prerelease")
    prerelease_number = match.group("prerelease_number")
    suffix = {"alpha": "a", "beta": "b", "rc": "fc"}.get(prerelease, "")
    macos_build = f"{macos_marketing}{suffix}{prerelease_number or ''}"
    return windows_numeric, macos_marketing, macos_build


def _windows_version_file(build_root: Path, version: str) -> Path:
    windows_numeric, _macos_marketing, _macos_build = _native_versions(version)
    version_tuple = tuple(int(part) for part in windows_numeric.split("."))
    target = build_root / "windows-version.txt"
    target.write_text(
        "VSVersionInfo(ffi=FixedFileInfo(filevers="
        f"{version_tuple}, prodvers={version_tuple}, mask=0x3f, flags=0x0, OS=0x40004, "
        "fileType=0x1, subtype=0x0, date=(0, 0)), kids=[StringFileInfo([StringTable("
        "'040904B0', [StringStruct('CompanyName', 'kejamestw'), "
        "StringStruct('FileDescription', 'OpenAI Free Credit Tracker'), "
        f"StringStruct('FileVersion', '{version}'), StringStruct('ProductVersion', '{version}'), "
        "StringStruct('ProductName', 'OpenAI Free Credit Tracker')])]), "
        "VarFileInfo([VarStruct('Translation', [1033, 1200])])])\n",
        encoding="utf-8",
        newline="\n",
    )
    return target


def _set_macos_bundle_metadata(bundle: Path, version: str) -> None:
    _windows_numeric, marketing_version, bundle_version = _native_versions(version)
    info_path = bundle / "Contents" / "Info.plist"
    with info_path.open("rb") as handle:
        document = plistlib.load(handle)
    document.update(
        {
            "CFBundleDisplayName": "OpenAI Free Credit Tracker",
            "CFBundleIdentifier": "tw.kejames.openai-free-credit-tracker",
            "CFBundleShortVersionString": marketing_version,
            "CFBundleVersion": bundle_version,
        }
    )
    with info_path.open("wb") as handle:
        plistlib.dump(document, handle, sort_keys=True)


def _macos_dependencies(binary: Path) -> list[Path]:
    """Return the literal Mach-O dependency names reported by ``otool``."""

    result = subprocess.run(
        ["otool", "-L", str(binary)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return [
        Path(line.strip().split(" (", 1)[0])
        for line in result.stdout.splitlines()[1:]
        if line.strip()
    ]


def _macos_rpaths(binary: Path) -> list[str]:
    result = subprocess.run(
        ["otool", "-l", str(binary)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    lines = iter(result.stdout.splitlines())
    paths: list[str] = []
    for line in lines:
        if line.strip() != "cmd LC_RPATH":
            continue
        for detail in lines:
            stripped = detail.strip()
            if stripped.startswith("path "):
                paths.append(stripped[5:].split(" (offset ", 1)[0])
                break
    return paths


def _resolve_macos_dependency(binary: Path, dependency: Path) -> Path | None:
    install_name = dependency.as_posix()
    if dependency.is_absolute():
        return dependency if dependency.is_file() else None
    suffix = install_name.split("/", 1)[1] if "/" in install_name else dependency.name
    candidates: list[Path] = []
    if install_name.startswith("@loader_path/"):
        candidates.append(binary.parent / suffix)
    elif install_name.startswith("@rpath/"):
        # Homebrew libraries commonly colocate libssl and libcrypto. Also
        # honor literal LC_RPATH values when the bottle embeds them.
        candidates.append(binary.parent / suffix)
        for rpath in _macos_rpaths(binary):
            if rpath == "@loader_path":
                candidates.append(binary.parent / suffix)
            elif rpath.startswith("@loader_path/"):
                candidates.append(binary.parent / rpath.split("/", 1)[1] / suffix)
            elif Path(rpath).is_absolute():
                candidates.append(Path(rpath) / suffix)
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def _linked_cryptography_openssl() -> dict[str, Path]:
    spec = importlib.util.find_spec("cryptography.hazmat.bindings._rust")
    if spec is None or spec.origin is None:
        raise RuntimeError("cryptography native binding could not be located")
    binding = Path(spec.origin).resolve()
    ssl_install_name = next(
        (
            dependency
            for dependency in _macos_dependencies(binding)
            if dependency.name == "libssl.3.dylib"
        ),
        None,
    )
    ssl = (
        _resolve_macos_dependency(binding, ssl_install_name)
        if ssl_install_name is not None
        else None
    )
    if ssl is None:
        raise RuntimeError("cryptography Intel libssl dependency could not be located")
    crypto_install_name = next(
        (
            dependency
            for dependency in _macos_dependencies(ssl)
            if dependency.name == "libcrypto.3.dylib"
        ),
        None,
    )
    crypto = (
        _resolve_macos_dependency(ssl, crypto_install_name)
        if crypto_install_name is not None
        else None
    )
    if crypto is None:
        raise RuntimeError("cryptography Intel OpenSSL dependencies are incomplete")
    # Keep the literal install names rather than resolving Homebrew symlinks:
    # install_name_tool must replace the exact string embedded in libssl.
    return {"libssl.3.dylib": ssl, "libcrypto.3.dylib": crypto}


def _bundle_intel_macos_openssl(
    bundle: Path,
    *,
    libraries: dict[str, Path] | None = None,
) -> None:
    """Replace PyInstaller's older Python OpenSSL with cryptography's ABI."""

    discovered = libraries is None
    sources = _linked_cryptography_openssl() if discovered else libraries
    required = {"libssl.3.dylib", "libcrypto.3.dylib"}
    if sources.keys() != required or any(not path.is_file() for path in sources.values()):
        raise RuntimeError("cryptography Intel OpenSSL dependencies are incomplete")
    frameworks = bundle / "Contents" / "Frameworks"
    if not frameworks.is_dir():
        raise RuntimeError("macOS bundle Frameworks directory is missing")
    destinations = {name: frameworks / name for name in required}
    for name, source in sources.items():
        shutil.copy2(source, destinations[name])
        run(["install_name_tool", "-id", f"@rpath/{name}", str(destinations[name])])
    crypto_install_name = sources["libcrypto.3.dylib"].as_posix()
    if discovered:
        crypto_install_name = next(
            (
                dependency.as_posix()
                for dependency in _macos_dependencies(sources["libssl.3.dylib"])
                if dependency.name == "libcrypto.3.dylib"
            ),
            "",
        )
        if not crypto_install_name:
            raise RuntimeError("libssl does not declare its libcrypto dependency")
    run(
        [
            "install_name_tool",
            "-change",
            crypto_install_name,
            "@loader_path/libcrypto.3.dylib",
            str(destinations["libssl.3.dylib"]),
        ]
    )
    for destination in destinations.values():
        run(["lipo", str(destination), "-verify_arch", "x86_64"])
    repaired_dependencies = _macos_dependencies(destinations["libssl.3.dylib"])
    if not any(
        dependency.as_posix() == "@loader_path/libcrypto.3.dylib"
        for dependency in repaired_dependencies
    ):
        raise RuntimeError("bundled libssl does not use its colocated libcrypto")
    allowed_openssl_names = {
        "libssl.3.dylib": {
            "@rpath/libssl.3.dylib",
            "@loader_path/libcrypto.3.dylib",
        },
        "libcrypto.3.dylib": {"@rpath/libcrypto.3.dylib"},
    }
    for name, destination in destinations.items():
        if any(
            dependency.name in required
            and dependency.as_posix() not in allowed_openssl_names[name]
            for dependency in _macos_dependencies(destination)
        ):
            raise RuntimeError("bundled OpenSSL retains an external OpenSSL dependency")
    run(["otool", "-L", str(destinations["libssl.3.dylib"])])


def _pyinstaller(target_os: str, target_arch: str, build_root: Path, version: str) -> Path:
    spec_dir = build_root / "spec"
    work_dir = build_root / "work"
    dist_dir = build_root / "dist"
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--name",
        APP_NAME,
        "--paths",
        str(ROOT / "src"),
        "--specpath",
        str(spec_dir),
        "--workpath",
        str(work_dir),
        "--distpath",
        str(dist_dir),
    ]
    for source, destination in (("web", "web"), ("data", "data"), ("locales", "locales")):
        command += ["--add-data", f"{ROOT / source}{os.pathsep}{destination}"]
    # The desktop libraries are imported only when a graphical session is
    # available, so PyInstaller cannot discover them through static analysis.
    # Keep their distribution metadata and platform backend explicit; the
    # packaged self-test imports these modules without starting a tray or
    # requesting notification permission.
    for distribution in ("desktop-notifier", "pystray", "Pillow"):
        command += ["--copy-metadata", distribution]
    for package in ("desktop_notifier", "pystray", "PIL"):
        command += ["--collect-data", package]
    hidden_imports = [
        "desktop_notifier",
        "desktop_notifier.main",
        "pystray",
        "PIL",
        "PIL.Image",
        "PIL.ImageDraw",
    ]
    hidden_imports.extend(
        {
            "windows": ["desktop_notifier.backends.winrt", "pystray._win32"],
            "macos": [
                "desktop_notifier.backends.macos",
                "desktop_notifier.backends.macos_support",
                "pystray._darwin",
                "rubicon.objc",
                "rubicon.objc.eventloop",
            ],
            "linux": ["desktop_notifier.backends.dbus", "pystray._xorg", "dbus_fast"],
        }[target_os]
    )
    for module in hidden_imports:
        command += ["--hidden-import", module]
    if target_os == "windows":
        command += ["--onefile", "--version-file", str(_windows_version_file(build_root, version))]
    else:
        command.append("--onedir")
    if target_os == "macos":
        command += [
            "--windowed",
            "--osx-bundle-identifier",
            "tw.kejames.openai-free-credit-tracker",
            "--target-architecture",
            "arm64" if target_arch == "arm64" else "x86_64",
        ]
    command.append(str(ENTRY_POINT))
    environment = os.environ.copy()
    environment.setdefault("PYTHONHASHSEED", "0")
    environment.setdefault("SOURCE_DATE_EPOCH", "0")
    run(command, env=environment)
    if target_os == "windows":
        return dist_dir / f"{APP_NAME}.exe"
    if target_os == "macos":
        return dist_dir / f"{APP_NAME}.app"
    return dist_dir / APP_NAME


def _verify(
    executable: Path,
    version: str,
    *,
    appimage: bool = False,
    expect_update_trust: bool,
) -> None:
    command = [
        sys.executable,
        str(ROOT / "scripts" / "verify_packaged_artifact.py"),
        "--executable",
        str(executable),
        "--expected-version",
        version,
    ]
    if appimage:
        command.append("--appimage")
    if expect_update_trust:
        command.append("--expect-update-trust")
    run(command)


def _validate_update_trust_policy(channel: str) -> None:
    if channel == "candidate":
        if UPDATE_TRUST_PATH.exists():
            raise RuntimeError("non-stable packages must not bundle the stable update trust root")
        return
    if channel not in {"beta", "stable"}:
        raise ValueError("channel must be candidate, beta, or stable")
    key_id = os.environ.get("UPDATE_SIGNING_KEY_ID", "")
    public_key = os.environ.get("UPDATE_SIGNING_PUBLIC_KEY_B64", "")
    if not key_id or not public_key:
        raise RuntimeError("stable packages require the protected update public key and key ID")
    repository = os.environ.get("GITHUB_REPOSITORY", DEFAULT_REPOSITORY)
    try:
        actual = load_update_trust(UPDATE_TRUST_PATH, repository=repository)
    except ValueError as error:
        raise RuntimeError("signed packages require a valid immutable update trust resource") from error
    expected = build_update_trust_document(
        key_id=key_id,
        public_key_b64=public_key,
        repository=repository,
        public_keys_b64=public_keyring_from_environment(),
    )
    if actual != expected:
        raise RuntimeError("bundled update trust does not match the protected stable key")


def _windows_signing(channel: str) -> tuple[str, str, str, str] | None:
    if channel == "candidate":
        return None
    values = tuple(
        os.environ.get(name, "")
        for name in (
            "SIGNTOOL_EXE",
            "WINDOWS_SIGNING_CERT_SHA1",
            "WINDOWS_SIGNING_IDENTITY",
            "WINDOWS_TIMESTAMP_URL",
        )
    )
    if any(values) and not all(values):
        raise RuntimeError("Windows Authenticode configuration is incomplete")
    if not all(values):
        raise RuntimeError("signed Windows candidates require Authenticode credentials")
    return values if all(values) else None


def _sign_windows(path: Path, signing: tuple[str, str, str, str]) -> None:
    signtool, thumbprint, _identity, timestamp_url = signing
    run(
        [
            signtool,
            "sign",
            "/sha1",
            thumbprint,
            "/fd",
            "SHA256",
            "/tr",
            timestamp_url,
            "/td",
            "SHA256",
            str(path),
        ]
    )
    run([signtool, "verify", "/pa", "/all", "/v", str(path)])


def _build_windows(
    bundle: Path,
    output: Path,
    version: str,
    channel: str,
    *,
    installer: bool,
) -> list[Path]:
    executable = output / f"{APP_NAME}-{version}-windows-x86_64-portable.exe"
    shutil.copy2(bundle, executable)
    signing = _windows_signing(channel)
    if signing:
        _sign_windows(executable, signing)
    _verify(executable, version, expect_update_trust=channel in {"beta", "stable"})
    artifacts = [executable]
    if installer:
        compiler = os.environ.get("ISCC_EXE")
        candidates = [
            Path(compiler) if compiler else None,
            Path(os.environ.get("ProgramFiles(x86)", "")) / "Inno Setup 6" / "ISCC.exe",
            Path(os.environ.get("ProgramFiles", "")) / "Inno Setup 6" / "ISCC.exe",
        ]
        iscc = next((candidate for candidate in candidates if candidate and candidate.is_file()), None)
        if iscc is None:
            raise RuntimeError("Inno Setup 6 is required for the Windows installer")
        run(
            [
                str(iscc),
                f"/DAppVersion={version}",
                f"/DNumericVersion={_native_versions(version)[0]}",
                f"/DSourceExe={executable}",
                f"/DOutputDirectory={output}",
                str(ROOT / "installer" / "windows" / "OpenAI-Free-Credit-Tracker.iss"),
            ]
        )
        setup = output / f"{APP_NAME}-{version}-windows-x86_64-setup.exe"
        if not setup.is_file():
            raise RuntimeError("Inno Setup did not produce the contracted installer name")
        if signing:
            _sign_windows(setup, signing)
        install_root = (ROOT / "build" / "release" / "windows-installer-smoke").resolve()
        release_build_root = (ROOT / "build" / "release").resolve()
        if release_build_root not in install_root.parents:
            raise RuntimeError("installer smoke path escaped the release build root")
        shutil.rmtree(install_root, ignore_errors=True)
        run(
            [
                str(setup),
                "/VERYSILENT",
                "/SUPPRESSMSGBOXES",
                "/NORESTART",
                f"/DIR={install_root}",
            ]
        )
        installed_executable = install_root / f"{APP_NAME}.exe"
        _verify(installed_executable, version, expect_update_trust=channel in {"beta", "stable"})
        uninstaller = install_root / "unins000.exe"
        run([str(uninstaller), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"])
        artifacts.append(setup)
    return artifacts


def _mac_credentials(channel: str) -> tuple[str, str, str, str] | None:
    if channel == "candidate":
        return None
    values = tuple(
        os.environ.get(name, "")
        for name in (
            "MACOS_SIGNING_IDENTITY",
            "APPLE_API_KEY_PATH",
            "APPLE_API_KEY_ID",
            "APPLE_API_ISSUER",
        )
    )
    if any(values) and not all(values):
        raise RuntimeError("macOS signing/notarization credentials are incomplete")
    if not all(values):
        raise RuntimeError("signed macOS candidates require signing and notarization credentials")
    return values if all(values) else None


def _sign_macos_bundle(bundle: Path, identity: str | None) -> None:
    if identity is None:
        # PyInstaller ad-hoc signs the bundle before we replace Info.plist.
        # Re-sign after native metadata is finalized so the bundle remains
        # executable on both Intel and Apple Silicon release runners.
        run(["codesign", "--force", "--deep", "--sign", "-", str(bundle)])
    else:
        run(
            [
                "codesign",
                "--force",
                "--deep",
                "--options",
                "runtime",
                "--timestamp",
                "--entitlements",
                str(ROOT / "packaging" / "macos" / "entitlements.plist"),
                "--sign",
                identity,
                str(bundle),
            ]
        )
    run(["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(bundle)])


def _build_macos(bundle: Path, output: Path, version: str, arch: str, channel: str) -> list[Path]:
    executable = bundle / "Contents" / "MacOS" / APP_NAME
    credentials = _mac_credentials(channel)
    _sign_macos_bundle(bundle, credentials[0] if credentials else None)
    run(["file", str(executable)])
    _verify(executable, version, expect_update_trust=channel in {"beta", "stable"})
    if credentials:
        notary_archive = bundle.parent / f".{APP_NAME}-notary.zip"
        run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(bundle), str(notary_archive)])
        _identity, key_path, key_id, issuer = credentials
        run(
            [
                "xcrun",
                "notarytool",
                "submit",
                str(notary_archive),
                "--key",
                key_path,
                "--key-id",
                key_id,
                "--issuer",
                issuer,
                "--wait",
            ]
        )
        notary_archive.unlink()
        run(["xcrun", "stapler", "staple", str(bundle)])
        run(["xcrun", "stapler", "validate", str(bundle)])
        run(["spctl", "--assess", "--type", "execute", "--verbose=2", str(bundle)])

    archive = output / f"{APP_NAME}-{version}-macos-{arch}.app.zip"
    run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(bundle), str(archive)])
    dmg = output / f"{APP_NAME}-{version}-macos-{arch}.dmg"
    dmg_root = bundle.parent / "dmg-root"
    shutil.rmtree(dmg_root, ignore_errors=True)
    dmg_root.mkdir()
    run(["ditto", str(bundle), str(dmg_root / bundle.name)])
    (dmg_root / "Applications").symlink_to("/Applications", target_is_directory=True)
    run(
        [
            "hdiutil",
            "create",
            "-volname",
            "OpenAI Free Credit Tracker",
            "-srcfolder",
            str(dmg_root),
            "-ov",
            "-format",
            "UDZO",
            str(dmg),
        ]
    )
    run(["hdiutil", "verify", str(dmg)])
    if credentials:
        identity, key_path, key_id, issuer = credentials
        run(["codesign", "--force", "--timestamp", "--sign", identity, str(dmg)])
        run(
            [
                "xcrun",
                "notarytool",
                "submit",
                str(dmg),
                "--key",
                key_path,
                "--key-id",
                key_id,
                "--issuer",
                issuer,
                "--wait",
            ]
        )
        run(["xcrun", "stapler", "staple", str(dmg)])
        run(["xcrun", "stapler", "validate", str(dmg)])
        run(["spctl", "--assess", "--type", "open", "--context", "context:primary-signature", str(dmg)])
    mountpoint = bundle.parent / "dmg-mount"
    shutil.rmtree(mountpoint, ignore_errors=True)
    mountpoint.mkdir()
    run(["hdiutil", "attach", "-readonly", "-nobrowse", "-mountpoint", str(mountpoint), str(dmg)])
    try:
        mounted_bundle = mountpoint / bundle.name
        _verify(
            mounted_bundle / "Contents" / "MacOS" / APP_NAME,
            version,
            expect_update_trust=channel in {"beta", "stable"},
        )
        if credentials:
            run(["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(mounted_bundle)])
            run(["spctl", "--assess", "--type", "execute", "--verbose=2", str(mounted_bundle)])
    finally:
        run(["hdiutil", "detach", str(mountpoint)])
    return [archive, dmg]


def _deterministic_tar(source: Path, output: Path, arcname: str) -> None:
    with output.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as archive:
                paths = [source, *sorted(source.rglob("*"))]
                for path in paths:
                    relative = Path(arcname) / path.relative_to(source)
                    info = archive.gettarinfo(str(path), str(relative))
                    info.uid = info.gid = 0
                    info.uname = info.gname = "root"
                    info.mtime = 0
                    if path.is_file():
                        with path.open("rb") as handle:
                            archive.addfile(info, handle)
                    else:
                        archive.addfile(info)


def _build_linux(
    bundle: Path,
    output: Path,
    build_root: Path,
    version: str,
    arch: str,
    appimagetool: Path,
    runtime: Path,
    channel: str,
) -> list[Path]:
    executable = bundle / APP_NAME
    _verify(executable, version, expect_update_trust=channel in {"beta", "stable"})
    tarball = output / f"{APP_NAME}-{version}-linux-{arch}.tar.gz"
    _deterministic_tar(bundle, tarball, APP_NAME)

    appdir = build_root / f"{APP_NAME}.AppDir"
    shutil.copytree(bundle, appdir / "usr" / "bin")
    shutil.copy2(ROOT / "packaging" / "linux" / "AppRun", appdir / "AppRun")
    shutil.copy2(
        ROOT / "packaging" / "linux" / "openai-free-credit-tracker.desktop",
        appdir / "openai-free-credit-tracker.desktop",
    )
    shutil.copy2(
        ROOT / "packaging" / "linux" / "openai-free-credit-tracker.svg",
        appdir / "openai-free-credit-tracker.svg",
    )
    (appdir / "AppRun").chmod(0o755)
    appimage = output / f"{APP_NAME}-{version}-linux-{arch}.AppImage"
    environment = os.environ.copy()
    environment.update(
        {"ARCH": "x86_64", "APPIMAGE_EXTRACT_AND_RUN": "1", "VERSION": version}
    )
    run(
        [str(appimagetool.resolve()), "--runtime-file", str(runtime.resolve()), str(appdir), str(appimage)],
        env=environment,
    )
    appimage.chmod(0o755)
    _verify(
        appimage,
        version,
        appimage=True,
        expect_update_trust=channel in {"beta", "stable"},
    )
    return [tarball, appimage]


def build(args: argparse.Namespace) -> list[Path]:
    require_native(args.platform, args.arch)
    if args.platform in {"windows", "linux"} and args.arch != "x86_64":
        raise RuntimeError(f"{args.platform} release currently supports x86_64 only")
    version = package_version()
    _validate_update_trust_policy(args.channel)
    build_root = ROOT / "build" / "release" / f"{args.platform}-{args.arch}"
    _reset_directory(build_root)
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    bundle = _pyinstaller(args.platform, args.arch, build_root, version)
    if args.platform == "macos":
        _set_macos_bundle_metadata(bundle, version)
        if args.arch == "x86_64":
            _bundle_intel_macos_openssl(bundle)
    if args.platform == "windows":
        artifacts = _build_windows(
            bundle, output, version, args.channel, installer=args.with_installer
        )
    elif args.platform == "macos":
        artifacts = _build_macos(bundle, output, version, args.arch, args.channel)
    else:
        if args.appimagetool is None or args.appimage_runtime is None:
            raise RuntimeError("Linux AppImage builds require pinned appimagetool and runtime paths")
        artifacts = _build_linux(
            bundle,
            output,
            build_root,
            version,
            args.arch,
            args.appimagetool,
            args.appimage_runtime,
            args.channel,
        )
    for artifact in artifacts:
        if not artifact.is_file() or artifact.stat().st_size < 1:
            raise RuntimeError(f"expected release artifact was not produced: {artifact}")
    return artifacts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=("windows", "macos", "linux"), required=True)
    parser.add_argument("--arch", choices=("x86_64", "arm64"), required=True)
    parser.add_argument("--channel", choices=("candidate", "beta", "stable"), default="candidate")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist" / "release-artifacts")
    parser.add_argument("--with-installer", action="store_true")
    parser.add_argument("--appimagetool", type=Path)
    parser.add_argument("--appimage-runtime", type=Path)
    args = parser.parse_args()
    for artifact in build(args):
        print(f"Built release artifact: {artifact}")


if __name__ == "__main__":
    main()
