import json

from quota_monitor.safe_logging import SafeEventLogger, redact_text, safe_event


SECRET = "sk-" + "admin-" + "this-must-never-be-written"


def test_redaction_removes_admin_keys_and_bearer_values():
    result = redact_text(f"key={SECRET} Authorization: Bearer abc.def-123")

    assert SECRET not in result
    assert "abc.def-123" not in result
    assert "[REDACTED_ADMIN_KEY]" in result


def test_event_logger_only_persists_allowlisted_sanitized_fields(tmp_path):
    path = tmp_path / "events.jsonl"
    logger = SafeEventLogger(path)

    logger.emit(
        "collection_failed",
        code=f"upstream_{SECRET}",
        profile_ref="profile-private-identifier",
        body={"secret": SECRET},
        url="https://example.invalid/private",
    )
    stored = path.read_text(encoding="utf-8")
    record = json.loads(stored)

    assert SECRET not in stored
    assert "body" not in record
    assert "url" not in record
    assert record["code"] == "upstream_[REDACTED_ADMIN_KEY]"
    assert record["profile_ref"] == "profil…fier"


def test_log_rotation_is_bounded(tmp_path):
    path = tmp_path / "events.jsonl"
    logger = SafeEventLogger(path, maximum_bytes=1024, backup_count=2)

    for index in range(40):
        logger.emit("event", code=f"code-{index}-" + ("x" * 100))

    assert path.exists()
    assert path.with_name("events.jsonl.1").exists()
    assert not path.with_name("events.jsonl.3").exists()


def test_safe_event_rejects_unbounded_event_names():
    try:
        safe_event("x" * 81)
    except ValueError as error:
        assert "event name" in str(error)
    else:
        raise AssertionError("expected invalid event name")
