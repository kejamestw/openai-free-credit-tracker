from quota_monitor.quality_harness import (
    PerformanceThresholds,
    run_performance_harness,
    run_simulated_soak,
)


def test_small_performance_harness_proves_counts_integrity_and_artifacts(tmp_path):
    generous = PerformanceThresholds(
        populate_seconds=30,
        startup_seconds=10,
        query_30_days_seconds=10,
        query_365_days_one_project_seconds=10,
        export_seconds=30,
        backup_seconds=30,
    )
    report = run_performance_harness(
        tmp_path,
        days=30,
        projects=4,
        thresholds=generous,
    )
    assert report["passed"] is True
    assert report["scenario"] == {"days": 30, "projects": 4, "records": 120}
    assert all(report["checks"].values())


def test_accelerated_soak_is_explicitly_not_native_release_evidence():
    report = run_simulated_soak(hours=24)
    assert report["passed"] is True
    assert report["release_gate_equivalent"] is False
    assert report["statuses"]["retryable_error"] > 0
    assert report["statuses"]["partial"] > 0
    assert report["checks"]["sleep_resume_single_catchup"] is True
