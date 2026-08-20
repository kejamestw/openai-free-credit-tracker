import subprocess
from pathlib import Path

import pytest

from scripts.scan_artifacts import (
    MalwareDetectedError,
    MalwareScanError,
    Scanner,
    scan_artifacts,
)


def fake_result(returncode: int):
    def run(command, **kwargs):
        assert kwargs["check"] is False
        if "--version" in command or "-?" in command:
            assert kwargs["timeout"] == 30
            return subprocess.CompletedProcess(
                command, 0, stdout=b"ClamAV 1.4.3/27844/Tue Aug 19 00:00:00 2026\n", stderr=b""
            )
        assert kwargs["timeout"] == 600
        return subprocess.CompletedProcess(command, returncode, stdout=b"", stderr=b"")

    return run


def test_clean_scan_records_hash_without_local_path(tmp_path):
    artifact = tmp_path / "release.bin"
    artifact.write_bytes(b"immutable release bytes")
    report = scan_artifacts(
        [artifact],
        Scanner("clamscan", Path("clamscan")),
        runner=fake_result(0),
    )
    assert report["passed"] is True
    assert report["scanner_version"].startswith("ClamAV 1.4.3/27844/")
    assert report["artifacts"][0]["name"] == "release.bin"
    assert report["artifacts"][0]["sha256"] == (
        "5396515749878bd28c5dae110040b4fbae1c33f59318b9c23f446319d68e236a"
    )
    assert str(tmp_path) not in str(report)


def test_infected_and_scanner_error_both_fail_closed(tmp_path):
    artifact = tmp_path / "release.bin"
    artifact.write_bytes(b"sample")
    scanner = Scanner("defender", Path("MpCmdRun.exe"))
    with pytest.raises(MalwareDetectedError, match="flagged"):
        scan_artifacts([artifact], scanner, runner=fake_result(1))
    with pytest.raises(MalwareScanError, match="exit code 2"):
        scan_artifacts([artifact], scanner, runner=fake_result(2))


def test_empty_artifact_set_is_rejected():
    with pytest.raises(ValueError, match="at least one"):
        scan_artifacts([], Scanner("clamscan", Path("clamscan")), runner=fake_result(0))
