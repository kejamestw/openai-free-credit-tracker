import json
import sys
from pathlib import Path

import pytest

from scripts import audit_dependencies


def _invoke(monkeypatch, tmp_path: Path, dependencies: list[dict]):
    output = tmp_path / "audit.json"
    cache = tmp_path / "cache"
    monkeypatch.setattr(audit_dependencies, "DEFAULT_CACHE_DIR", cache)
    monkeypatch.setattr(sys, "argv", ["audit_dependencies.py", "--output", str(output)])

    def run(command, **kwargs):
        assert "--local" in command
        assert "--skip-editable" in command
        assert "--cache-dir" in command
        assert "truststore.inject_into_ssl()" in command[2]
        assert Path(command[command.index("--cache-dir") + 1]) == cache
        destination = Path(command[command.index("--output") + 1])
        destination.write_text(
            json.dumps({"dependencies": dependencies, "fixes": []}),
            encoding="utf-8",
        )

    monkeypatch.setattr(audit_dependencies.subprocess, "run", run)
    audit_dependencies.main()
    return output


def test_dependency_audit_uses_project_cache_and_allows_only_local_editable_skip(
    monkeypatch, tmp_path
):
    output = _invoke(
        monkeypatch,
        tmp_path,
        [
            {
                "name": "openai-free-credit-tracker",
                "version": "1.0.0",
                "vulns": [],
                "skip_reason": "distribution marked as editable",
            },
            {"name": "cryptography", "version": "50.0.0", "vulns": []},
        ],
    )
    assert output.is_file()


def test_dependency_audit_fails_when_a_third_party_package_was_not_audited(
    monkeypatch, tmp_path
):
    with pytest.raises(RuntimeError, match="third-party dependencies.*mystery"):
        _invoke(
            monkeypatch,
            tmp_path,
            [{"name": "mystery", "version": "1", "skip_reason": "not on index"}],
        )
