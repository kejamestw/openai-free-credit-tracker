PRAGMA foreign_keys = ON;
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
CREATE TABLE profiles (
    profile_id TEXT PRIMARY KEY NOT NULL,
    display_name TEXT NOT NULL,
    credential_id TEXT NOT NULL UNIQUE,
    organization_ref_private TEXT,
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    created_at TEXT NOT NULL,
    last_validated_at TEXT
);
CREATE TABLE projects (
    profile_id TEXT NOT NULL REFERENCES profiles(profile_id) ON DELETE RESTRICT,
    project_key TEXT NOT NULL,
    project_id_private TEXT NOT NULL,
    display_name TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (profile_id, project_key),
    UNIQUE (profile_id, project_id_private)
);
CREATE TABLE usage_buckets (
    profile_id TEXT NOT NULL,
    bucket_start_utc INTEGER NOT NULL,
    bucket_end_utc INTEGER NOT NULL,
    project_key TEXT NOT NULL,
    model TEXT NOT NULL,
    service_tier TEXT NOT NULL,
    input_tokens INTEGER NOT NULL CHECK (input_tokens >= 0),
    cached_input_tokens INTEGER NOT NULL CHECK (
        cached_input_tokens >= 0 AND cached_input_tokens <= input_tokens
    ),
    output_tokens INTEGER NOT NULL CHECK (output_tokens >= 0),
    request_count INTEGER NOT NULL CHECK (request_count >= 0),
    catalog_version TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    CHECK (bucket_end_utc > bucket_start_utc),
    FOREIGN KEY (profile_id, project_key)
        REFERENCES projects(profile_id, project_key),
    UNIQUE (
        profile_id, bucket_start_utc, bucket_end_utc,
        project_key, model, service_tier
    )
);
CREATE TABLE collection_runs (
    profile_id TEXT NOT NULL REFERENCES profiles(profile_id) ON DELETE RESTRICT,
    run_id TEXT NOT NULL,
    requested_start_utc INTEGER NOT NULL,
    requested_end_utc INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL CHECK (
        status IN ('running', 'completed', 'partial', 'failed', 'cancelled')
    ),
    pages_fetched INTEGER NOT NULL DEFAULT 0 CHECK (pages_fetched >= 0),
    error_code TEXT,
    CHECK (requested_end_utc > requested_start_utc),
    PRIMARY KEY (profile_id, run_id)
);
CREATE TABLE collection_slices (
    profile_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    slice_start_utc INTEGER NOT NULL,
    slice_end_utc INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'in_progress', 'completed', 'failed')
    ),
    pages_fetched INTEGER NOT NULL DEFAULT 0 CHECK (pages_fetched >= 0),
    checkpoint TEXT,
    error_code TEXT,
    updated_at TEXT NOT NULL,
    CHECK (slice_end_utc > slice_start_utc),
    FOREIGN KEY (profile_id, run_id)
        REFERENCES collection_runs(profile_id, run_id) ON DELETE CASCADE,
    PRIMARY KEY (profile_id, run_id, slice_start_utc, slice_end_utc)
);
CREATE TABLE alert_rules (
    profile_id TEXT NOT NULL REFERENCES profiles(profile_id) ON DELETE RESTRICT,
    rule_id TEXT NOT NULL,
    group_id TEXT NOT NULL,
    project_key TEXT NOT NULL DEFAULT 'all',
    threshold_percent REAL NOT NULL CHECK (
        threshold_percent > 0 AND threshold_percent <= 100
    ),
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (profile_id, rule_id)
);
CREATE TABLE alert_dedup (
    profile_id TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    utc_day TEXT NOT NULL,
    group_id TEXT NOT NULL,
    project_key TEXT NOT NULL,
    previous_percent REAL NOT NULL CHECK (previous_percent >= 0),
    sent_at TEXT,
    PRIMARY KEY (profile_id, rule_id, utc_day),
    FOREIGN KEY (profile_id, rule_id)
        REFERENCES alert_rules(profile_id, rule_id) ON DELETE CASCADE
);
CREATE TABLE notification_history (
    profile_id TEXT NOT NULL REFERENCES profiles(profile_id) ON DELETE RESTRICT,
    notification_id TEXT NOT NULL,
    rule_id TEXT,
    event_kind TEXT NOT NULL,
    group_id TEXT NOT NULL,
    project_key TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    delivery_status TEXT NOT NULL CHECK (
        delivery_status IN ('sent', 'failed', 'suppressed', 'test')
    ),
    error_code TEXT,
    is_test INTEGER NOT NULL CHECK (is_test IN (0, 1)),
    PRIMARY KEY (profile_id, notification_id)
);
CREATE INDEX usage_buckets_time_idx
ON usage_buckets(profile_id, bucket_start_utc, bucket_end_utc);
CREATE INDEX usage_buckets_project_time_idx
ON usage_buckets(profile_id, project_key, bucket_start_utc);
CREATE INDEX collection_slices_time_idx
ON collection_slices(profile_id, slice_start_utc, slice_end_utc, status);
CREATE INDEX notification_history_time_idx
ON notification_history(profile_id, occurred_at);

INSERT INTO schema_migrations VALUES (1, '2026-04-01T00:00:00Z');
INSERT INTO schema_migrations VALUES (2, '2026-05-01T00:00:00Z');
INSERT INTO profiles VALUES (
    'prof_22222222222222222222222222222222', 'v0.5 profile',
    'cred_v05_default', NULL, 1, '2026-05-01T00:00:00Z',
    '2026-05-01T00:05:00Z'
);
INSERT INTO projects VALUES (
    'prof_22222222222222222222222222222222',
    'project-222222222222222222222222', 'proj_v05_private', 'v0.5 project',
    '2026-05-01T00:00:00Z', '2026-05-02T00:00:00Z'
);
INSERT INTO usage_buckets VALUES (
    'prof_22222222222222222222222222222222',
    1777593600, 1777597200, 'project-222222222222222222222222',
    'gpt-5.6-terra', 'incentivized-tier', 250, 50, 20, 3,
    'catalog-v1', '2026-05-02T00:00:00Z'
);
INSERT INTO collection_runs VALUES (
    'prof_22222222222222222222222222222222', 'v05-run',
    1777593600, 1777680000, '2026-05-02T00:00:00Z',
    '2026-05-02T00:01:00Z', 'completed', 1, NULL
);
INSERT INTO collection_slices VALUES (
    'prof_22222222222222222222222222222222', 'v05-run',
    1777593600, 1777680000, 'completed', 1, NULL, NULL,
    '2026-05-02T00:01:00Z'
);
INSERT INTO alert_rules VALUES (
    'prof_22222222222222222222222222222222', 'alert_v05fixture',
    'standard', 'all', 80.0, 1,
    '2026-05-02T00:00:00Z', '2026-05-02T00:00:00Z'
);
INSERT INTO alert_dedup VALUES (
    'prof_22222222222222222222222222222222', 'alert_v05fixture',
    '2026-05-02', 'standard', 'all', 81.0, '2026-05-02T00:02:00Z'
);
INSERT INTO notification_history VALUES (
    'prof_22222222222222222222222222222222', 'notice_v05fixture',
    'alert_v05fixture', 'threshold_crossed', 'standard', 'all',
    '2026-05-02T00:02:00Z', 'sent', NULL, 0
);
