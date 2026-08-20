import json
import os

from quota_monitor.platform_adapters import FileInstanceLock


def test_file_instance_lock_excludes_second_owner_and_releases(tmp_path):
    path = (tmp_path / "app.lock").resolve()
    first = FileInstanceLock(path, pid=100, process_alive=lambda pid: pid == 100)
    second = FileInstanceLock(path, pid=200, process_alive=lambda pid: pid == 100)

    assert first.acquire() is True
    assert second.acquire() is False
    first.release()
    assert not path.exists()
    assert second.acquire() is True
    second.release()


def test_file_instance_lock_recovers_dead_process_lock(tmp_path):
    path = (tmp_path / "app.lock").resolve()
    crashed = FileInstanceLock(path, pid=100, process_alive=lambda _pid: False)
    replacement = FileInstanceLock(path, pid=200, process_alive=lambda _pid: False)

    assert crashed.acquire() is True
    assert replacement.acquire() is True
    assert replacement.stale_recovered is True
    metadata = json.loads(path.read_text(encoding="utf-8"))
    assert metadata["pid"] == 200
    replacement.release()


def test_file_instance_lock_does_not_recover_a_live_owner(tmp_path):
    path = (tmp_path / "app.lock").resolve()
    first = FileInstanceLock(path, pid=100, process_alive=lambda _pid: True)
    second = FileInstanceLock(path, pid=200, process_alive=lambda _pid: True)

    assert first.acquire() is True
    assert second.acquire() is False
    assert second.stale_recovered is False
    first.release()


def test_release_never_removes_a_replaced_owner_token(tmp_path):
    path = (tmp_path / "app.lock").resolve()
    lock = FileInstanceLock(path, pid=100, process_alive=lambda _pid: True)
    assert lock.acquire() is True
    metadata = json.loads(path.read_text(encoding="utf-8"))
    metadata["owner_token"] = "f" * 32
    path.write_text(json.dumps(metadata), encoding="utf-8")

    lock.release()

    assert path.exists()


def test_malformed_lock_is_recovered_only_after_grace_period(tmp_path):
    path = (tmp_path / "app.lock").resolve()
    path.write_text("not-json", encoding="utf-8")
    current = path.stat().st_mtime
    fresh = FileInstanceLock(
        path,
        pid=200,
        clock=lambda: current + 5,
        process_alive=lambda _pid: False,
        invalid_lock_grace_seconds=10,
    )
    stale = FileInstanceLock(
        path,
        pid=200,
        clock=lambda: current + 11,
        process_alive=lambda _pid: False,
        invalid_lock_grace_seconds=10,
    )

    assert fresh.acquire() is False
    assert stale.acquire() is True
    stale.release()


def test_lock_file_is_private_on_posix(tmp_path):
    path = (tmp_path / "app.lock").resolve()
    lock = FileInstanceLock(path)
    assert lock.acquire() is True
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    lock.release()
