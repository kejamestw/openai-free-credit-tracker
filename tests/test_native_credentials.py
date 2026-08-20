from collections import deque

import pytest

from quota_monitor.platform_adapters import (
    AdapterUnavailableError,
    CommandResult,
    CredentialNotFoundError,
    CredentialStore,
    LinuxSecretServiceCredentialStore,
    MacOSKeychainCredentialStore,
    PlatformFamily,
    UnavailableCredentialStore,
    WindowsCredentialStore,
    create_native_credential_store,
)


ACCOUNT = "prof_" + "a" * 32
SECRET = "test-secret-value"


class FakeCommandRunner:
    def __init__(self, *results):
        self.results = deque(results)
        self.calls = []

    def run(self, argv, *, input_text=None):
        self.calls.append((tuple(argv), input_text))
        return self.results.popleft() if self.results else CommandResult(0)


class FakeWindowsCredentialApi:
    def __init__(self):
        self.items = {}
        self.calls = []

    def write(self, target, username, secret):
        self.calls.append(("write", target, username))
        self.items[target] = bytes(secret)

    def read(self, target):
        self.calls.append(("read", target))
        if target not in self.items:
            raise OSError(1168, "not found")
        return self.items[target]

    def delete(self, target):
        self.calls.append(("delete", target))
        if target not in self.items:
            raise OSError(1168, "not found")
        del self.items[target]


def test_windows_credential_store_uses_ctypes_boundary_without_secret_metadata():
    api = FakeWindowsCredentialApi()
    store = WindowsCredentialStore(api)

    reference = store.put(ACCOUNT, SECRET)

    assert isinstance(store, CredentialStore)
    assert store.get(reference) == SECRET
    assert SECRET not in repr(store)
    assert all(SECRET not in repr(call) for call in api.calls)
    assert store.delete(reference) is True
    assert store.delete(reference) is False


def test_windows_backend_maps_os_errors_without_leaking_details():
    class DeniedApi(FakeWindowsCredentialApi):
        def write(self, target, username, secret):
            raise OSError(5, "failure containing " + SECRET)

    with pytest.raises(AdapterUnavailableError) as caught:
        WindowsCredentialStore(DeniedApi()).put(ACCOUNT, SECRET)

    assert SECRET not in str(caught.value)


def test_windows_backend_treats_empty_payload_as_missing():
    api = FakeWindowsCredentialApi()
    store = WindowsCredentialStore(api)
    reference = store.put(ACCOUNT, SECRET)
    api.items[next(iter(api.items))] = b""

    with pytest.raises(CredentialNotFoundError):
        store.get(reference)


def test_macos_keychain_writes_secret_through_stdin_not_argv():
    runner = FakeCommandRunner(CommandResult(0), CommandResult(0, stdout=SECRET + "\n"))
    store = MacOSKeychainCredentialStore(runner)

    reference = store.put(ACCOUNT, SECRET)

    write_argv, write_stdin = runner.calls[0]
    assert SECRET not in repr(write_argv)
    assert SECRET in write_stdin
    assert store.get(reference) == SECRET
    assert SECRET not in repr(CommandResult(0, stdout=SECRET))


def test_macos_keychain_errors_are_sanitized_and_missing_is_distinct():
    runner = FakeCommandRunner(CommandResult(9, stderr="failure " + SECRET))
    with pytest.raises(AdapterUnavailableError) as caught:
        MacOSKeychainCredentialStore(runner).put(ACCOUNT, SECRET)
    assert SECRET not in str(caught.value)

    seed = FakeCommandRunner(CommandResult(0))
    expected = MacOSKeychainCredentialStore(seed).put(ACCOUNT, SECRET)
    missing = FakeCommandRunner(CommandResult(44, stderr="item absent"))
    store = MacOSKeychainCredentialStore(missing)
    with pytest.raises(CredentialNotFoundError):
        store.get(expected)


def test_linux_secret_service_passes_secret_only_via_stdin():
    runner = FakeCommandRunner(CommandResult(0), CommandResult(0, stdout=SECRET + "\n"))
    store = LinuxSecretServiceCredentialStore(runner)

    reference = store.put(ACCOUNT, SECRET)

    write_argv, write_stdin = runner.calls[0]
    assert SECRET not in repr(write_argv)
    assert write_stdin == SECRET
    assert store.get(reference) == SECRET
    assert all(SECRET not in repr(argv) for argv, _stdin in runner.calls)


def test_linux_backend_distinguishes_unavailable_service_from_missing_item():
    unavailable = FakeCommandRunner(
        CommandResult(1, stderr="org.freedesktop.secrets ServiceUnknown " + SECRET)
    )
    missing = FakeCommandRunner(CommandResult(1, stderr="not found"))
    seed = FakeCommandRunner(CommandResult(0))
    reference = LinuxSecretServiceCredentialStore(seed).put(ACCOUNT, SECRET)
    with pytest.raises(AdapterUnavailableError) as caught:
        LinuxSecretServiceCredentialStore(unavailable).get(reference)
    assert SECRET not in str(caught.value)
    with pytest.raises(CredentialNotFoundError):
        LinuxSecretServiceCredentialStore(missing).get(reference)


def test_native_factory_fails_closed_when_platform_has_no_backend():
    store = create_native_credential_store(PlatformFamily.UNKNOWN)

    assert isinstance(store, UnavailableCredentialStore)
    assert store.available is False


def test_native_stores_reject_unsafe_service_labels_and_empty_reads():
    with pytest.raises(ValueError, match="service"):
        LinuxSecretServiceCredentialStore(FakeCommandRunner(), service="unsafe service")

    empty = FakeCommandRunner(CommandResult(0), CommandResult(0, stdout=""))
    store = LinuxSecretServiceCredentialStore(empty)
    reference = store.put(ACCOUNT, SECRET)
    with pytest.raises(CredentialNotFoundError):
        store.get(reference)
