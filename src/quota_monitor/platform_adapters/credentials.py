"""Native secure credential backends with injectable operating-system boundaries."""

from __future__ import annotations

import ctypes
import hashlib
import os
import re
import shutil
import sys
from ctypes import wintypes
from typing import Protocol, runtime_checkable

from .command_runner import CommandResult, CommandRunner, SubprocessCommandRunner
from .contracts import (
    AdapterUnavailableError,
    CredentialNotFoundError,
    CredentialReference,
    CredentialStore,
    PlatformFamily,
)
from .fallback import UnavailableCredentialStore


DEFAULT_CREDENTIAL_SERVICE = "com.openai.free-credit-tracker"
_ACCOUNT_PATTERN = re.compile(r"prof_[0-9a-f]{32}\Z", re.ASCII)
_SERVICE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{2,99}\Z", re.ASCII)


def _validate_account(account_id: str) -> str:
    if not isinstance(account_id, str) or not _ACCOUNT_PATTERN.fullmatch(account_id):
        raise ValueError("account_id must be an opaque profile identifier")
    return account_id


def _validate_secret(secret: str) -> str:
    if not isinstance(secret, str) or not secret:
        raise ValueError("secret must be a non-empty string")
    if len(secret) > 8192 or "\x00" in secret or "\r" in secret or "\n" in secret:
        raise ValueError("secret contains an unsupported control character")
    return secret


def _validate_service(service: str) -> str:
    if not isinstance(service, str) or not _SERVICE_PATTERN.fullmatch(service):
        raise ValueError("credential service name is invalid")
    return service


def _credential_reference(backend: str, service: str, account_id: str) -> CredentialReference:
    digest = hashlib.sha256(f"{backend}\0{service}\0{account_id}".encode("utf-8")).hexdigest()
    return CredentialReference(f"{backend}_{digest[:32]}", account_id)


def _assert_reference(
    reference: CredentialReference,
    *,
    backend: str,
    service: str,
) -> None:
    expected = _credential_reference(backend, service, _validate_account(reference.account_id))
    if reference != expected:
        raise CredentialNotFoundError(reference.credential_id)


@runtime_checkable
class WindowsCredentialApi(Protocol):
    def write(self, target: str, username: str, secret: bytes | bytearray) -> None: ...

    def read(self, target: str) -> bytes: ...

    def delete(self, target: str) -> None: ...


class CtypesWindowsCredentialApi:
    """Minimal Unicode wrapper around CredWriteW/CredReadW/CredDeleteW."""

    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_LOCAL_MACHINE = 2

    class _CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise OSError("Windows Credential Manager is unavailable")
        self._advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        credential_pointer = ctypes.POINTER(self._CREDENTIALW)
        self._advapi32.CredWriteW.argtypes = [credential_pointer, wintypes.DWORD]
        self._advapi32.CredWriteW.restype = wintypes.BOOL
        self._advapi32.CredReadW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(credential_pointer),
        ]
        self._advapi32.CredReadW.restype = wintypes.BOOL
        self._advapi32.CredDeleteW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        self._advapi32.CredDeleteW.restype = wintypes.BOOL
        self._advapi32.CredFree.argtypes = [ctypes.c_void_p]
        self._advapi32.CredFree.restype = None

    @staticmethod
    def _raise_last_error() -> None:
        error_code = ctypes.get_last_error()
        raise OSError(error_code, "Windows Credential Manager operation failed")

    def write(self, target: str, username: str, secret: bytes | bytearray) -> None:
        blob = (ctypes.c_ubyte * len(secret)).from_buffer_copy(secret)
        credential = self._CREDENTIALW()
        credential.Type = self.CRED_TYPE_GENERIC
        credential.TargetName = target
        credential.CredentialBlobSize = len(secret)
        credential.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
        credential.Persist = self.CRED_PERSIST_LOCAL_MACHINE
        credential.UserName = username
        try:
            if not self._advapi32.CredWriteW(ctypes.byref(credential), 0):
                self._raise_last_error()
        finally:
            ctypes.memset(ctypes.addressof(blob), 0, len(secret))

    def read(self, target: str) -> bytes:
        pointer = ctypes.POINTER(self._CREDENTIALW)()
        if not self._advapi32.CredReadW(
            target,
            self.CRED_TYPE_GENERIC,
            0,
            ctypes.byref(pointer),
        ):
            self._raise_last_error()
        try:
            credential = pointer.contents
            return ctypes.string_at(
                credential.CredentialBlob,
                credential.CredentialBlobSize,
            )
        finally:
            self._advapi32.CredFree(pointer)

    def delete(self, target: str) -> None:
        if not self._advapi32.CredDeleteW(target, self.CRED_TYPE_GENERIC, 0):
            self._raise_last_error()


class WindowsCredentialStore:
    available = True
    _backend = "wincred"

    def __init__(
        self,
        api: WindowsCredentialApi | None = None,
        *,
        service: str = DEFAULT_CREDENTIAL_SERVICE,
    ) -> None:
        self._api = api or CtypesWindowsCredentialApi()
        self._service = _validate_service(service)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(service={self._service!r})"

    def _target(self, account_id: str) -> str:
        return f"{self._service}/{_validate_account(account_id)}"

    @staticmethod
    def _map_error(error: OSError, *, missing_ok: bool = False) -> None:
        code = getattr(error, "winerror", None) or getattr(error, "errno", None)
        if missing_ok and code == 1168:
            raise CredentialNotFoundError("credential is missing") from None
        raise AdapterUnavailableError("Windows Credential Manager operation failed") from None

    def put(self, account_id: str, secret: str) -> CredentialReference:
        target = self._target(account_id)
        secret_bytes = bytearray(_validate_secret(secret).encode("utf-16-le"))
        if len(secret_bytes) > 2560:
            secret_bytes[:] = b"\x00" * len(secret_bytes)
            raise ValueError("secret exceeds Windows Credential Manager limits")
        try:
            self._api.write(target, account_id, secret_bytes)
        except OSError as exc:
            self._map_error(exc)
        finally:
            secret_bytes[:] = b"\x00" * len(secret_bytes)
        return _credential_reference(self._backend, self._service, account_id)

    def get(self, reference: CredentialReference) -> str:
        _assert_reference(reference, backend=self._backend, service=self._service)
        try:
            payload = self._api.read(self._target(reference.account_id))
            secret = payload.decode("utf-16-le")
            if not secret:
                raise CredentialNotFoundError("credential is missing")
            return secret
        except UnicodeError:
            raise AdapterUnavailableError("Windows Credential Manager returned invalid data") from None
        except OSError as exc:
            self._map_error(exc, missing_ok=True)

    def delete(self, reference: CredentialReference) -> bool:
        _assert_reference(reference, backend=self._backend, service=self._service)
        try:
            self._api.delete(self._target(reference.account_id))
        except OSError as exc:
            code = getattr(exc, "winerror", None) or getattr(exc, "errno", None)
            if code == 1168:
                return False
            self._map_error(exc)
        return True


def _security_quote(value: str) -> str:
    if any(character in value for character in ("\x00", "\r", "\n")):
        raise ValueError("security command value contains an invalid character")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class MacOSKeychainCredentialStore:
    available = True
    _backend = "keychain"

    def __init__(
        self,
        runner: CommandRunner,
        *,
        executable: str = "/usr/bin/security",
        service: str = DEFAULT_CREDENTIAL_SERVICE,
    ) -> None:
        self._runner = runner
        self._executable = executable
        self._service = _validate_service(service)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(service={self._service!r})"

    @staticmethod
    def _failed(result: CommandResult, operation: str) -> None:
        if result.returncode == 44 or "-25300" in result.stderr:
            raise CredentialNotFoundError("credential is missing")
        raise AdapterUnavailableError(f"macOS Keychain {operation} failed")

    def put(self, account_id: str, secret: str) -> CredentialReference:
        account = _validate_account(account_id)
        password = _validate_secret(secret)
        command = " ".join(
            (
                "add-generic-password",
                "-U",
                "-a",
                _security_quote(account),
                "-s",
                _security_quote(self._service),
                "-l",
                _security_quote("OpenAI Free Credit Tracker"),
                "-w",
                _security_quote(password),
            )
        )
        result = self._runner.run((self._executable, "-q"), input_text=command + "\n")
        if result.returncode != 0:
            self._failed(result, "write")
        return _credential_reference(self._backend, self._service, account)

    def get(self, reference: CredentialReference) -> str:
        _assert_reference(reference, backend=self._backend, service=self._service)
        result = self._runner.run(
            (
                self._executable,
                "find-generic-password",
                "-a",
                reference.account_id,
                "-s",
                self._service,
                "-w",
            )
        )
        if result.returncode != 0:
            self._failed(result, "read")
        secret = result.stdout.rstrip("\r\n")
        if not secret:
            raise CredentialNotFoundError("credential is missing")
        return secret

    def delete(self, reference: CredentialReference) -> bool:
        _assert_reference(reference, backend=self._backend, service=self._service)
        result = self._runner.run(
            (
                self._executable,
                "delete-generic-password",
                "-a",
                reference.account_id,
                "-s",
                self._service,
            )
        )
        if result.returncode == 44 or "-25300" in result.stderr:
            return False
        if result.returncode != 0:
            self._failed(result, "delete")
        return True


class LinuxSecretServiceCredentialStore:
    available = True
    _backend = "secretservice"

    def __init__(
        self,
        runner: CommandRunner,
        *,
        executable: str = "secret-tool",
        service: str = DEFAULT_CREDENTIAL_SERVICE,
    ) -> None:
        self._runner = runner
        self._executable = executable
        self._service = _validate_service(service)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(service={self._service!r})"

    @staticmethod
    def _service_unavailable(result: CommandResult) -> bool:
        error = result.stderr.lower()
        return any(
            marker in error
            for marker in (
                "dbus",
                "org.freedesktop.secrets",
                "serviceunknown",
                "no such secret collection",
            )
        )

    def put(self, account_id: str, secret: str) -> CredentialReference:
        account = _validate_account(account_id)
        password = _validate_secret(secret)
        result = self._runner.run(
            (
                self._executable,
                "store",
                "--label=OpenAI Free Credit Tracker",
                "service",
                self._service,
                "account",
                account,
            ),
            input_text=password,
        )
        if result.returncode != 0:
            raise AdapterUnavailableError("Linux Secret Service write failed")
        return _credential_reference(self._backend, self._service, account)

    def get(self, reference: CredentialReference) -> str:
        _assert_reference(reference, backend=self._backend, service=self._service)
        result = self._runner.run(
            (
                self._executable,
                "lookup",
                "service",
                self._service,
                "account",
                reference.account_id,
            )
        )
        if result.returncode != 0:
            if self._service_unavailable(result):
                raise AdapterUnavailableError("Linux Secret Service is unavailable")
            raise CredentialNotFoundError("credential is missing")
        secret = result.stdout.rstrip("\r\n")
        if not secret:
            raise CredentialNotFoundError("credential is missing")
        return secret

    def delete(self, reference: CredentialReference) -> bool:
        _assert_reference(reference, backend=self._backend, service=self._service)
        result = self._runner.run(
            (
                self._executable,
                "clear",
                "service",
                self._service,
                "account",
                reference.account_id,
            )
        )
        if result.returncode != 0:
            if self._service_unavailable(result):
                raise AdapterUnavailableError("Linux Secret Service is unavailable")
            return False
        return True


def create_native_credential_store(
    family: PlatformFamily,
    *,
    runner: CommandRunner | None = None,
) -> CredentialStore:
    """Select a native backend, returning a fail-closed adapter when absent."""

    if family is PlatformFamily.WINDOWS:
        try:
            return WindowsCredentialStore()
        except OSError:
            return UnavailableCredentialStore("Windows Credential Manager is unavailable")
    if family is PlatformFamily.MACOS:
        executable = "/usr/bin/security"
        if runner is not None:
            return MacOSKeychainCredentialStore(runner, executable=executable)
        if sys.platform == "darwin" and os.path.isfile(executable):
            return MacOSKeychainCredentialStore(SubprocessCommandRunner(), executable=executable)
        return UnavailableCredentialStore("macOS Keychain is unavailable")
    if family is PlatformFamily.LINUX:
        executable = shutil.which("secret-tool")
        if runner is not None:
            return LinuxSecretServiceCredentialStore(runner)
        if sys.platform.startswith("linux") and executable:
            return LinuxSecretServiceCredentialStore(
                SubprocessCommandRunner(),
                executable=executable,
            )
        return UnavailableCredentialStore("Linux Secret Service is unavailable")
    return UnavailableCredentialStore("secure credential storage is unsupported")
