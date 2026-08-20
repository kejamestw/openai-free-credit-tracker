from pathlib import Path

from scripts.audit_repository import ROOT, audit_tracked_text, repository_text_files


def test_repository_scan_includes_nonignored_untracked_source_files():
    relative_paths = {path.relative_to(ROOT) for path, _content in repository_text_files()}
    assert Path("src/quota_monitor/database.py") in relative_paths
    assert Path("docs/threat-model.md") in relative_paths


def test_text_audit_finds_secret_shapes_and_control_characters_without_echoing_secret():
    secret = b"sk-" + b"admin-" + b"x" * 16
    findings = audit_tracked_text([(ROOT / "synthetic.txt", secret + b"\x00")])

    assert any("potential API key" in finding for finding in findings)
    assert any("unexpected control character" in finding for finding in findings)
    assert all(secret.decode("ascii") not in finding for finding in findings)
