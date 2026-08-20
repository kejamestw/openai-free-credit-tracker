PRAGMA foreign_keys = ON;
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
INSERT INTO schema_migrations VALUES (1, '2026-01-01T00:00:00Z');
CREATE TABLE projects (
    project_key TEXT PRIMARY KEY NOT NULL,
    project_id_private TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE usage_buckets (
    bucket_start_utc INTEGER NOT NULL,
    bucket_end_utc INTEGER NOT NULL,
    project_key TEXT NOT NULL REFERENCES projects(project_key),
    model TEXT NOT NULL,
    service_tier TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    cached_input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    request_count INTEGER NOT NULL,
    catalog_version TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    UNIQUE (bucket_start_utc, bucket_end_utc, project_key, model, service_tier)
);
CREATE TABLE collection_runs (
    run_id TEXT PRIMARY KEY NOT NULL,
    requested_start_utc INTEGER NOT NULL,
    requested_end_utc INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    pages_fetched INTEGER NOT NULL,
    error_code TEXT
);
CREATE TABLE collection_slices (
    run_id TEXT NOT NULL REFERENCES collection_runs(run_id) ON DELETE CASCADE,
    slice_start_utc INTEGER NOT NULL,
    slice_end_utc INTEGER NOT NULL,
    status TEXT NOT NULL,
    pages_fetched INTEGER NOT NULL,
    checkpoint TEXT,
    error_code TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (run_id, slice_start_utc, slice_end_utc)
);
CREATE INDEX usage_buckets_time_idx ON usage_buckets(bucket_start_utc, bucket_end_utc);
CREATE INDEX usage_buckets_project_time_idx ON usage_buckets(project_key, bucket_start_utc);
CREATE INDEX collection_slices_time_idx ON collection_slices(slice_start_utc, slice_end_utc, status);
INSERT INTO projects VALUES (
    'project-111111111111111111111111', 'proj_legacy_private', 'Legacy project',
    '2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z'
);
INSERT INTO usage_buckets VALUES (
    1767225600, 1767229200, 'project-111111111111111111111111',
    'gpt-5.6-terra', 'incentivized-tier', 100, 20, 5, 2,
    'catalog-v1', '2026-01-02T00:00:00Z'
);
INSERT INTO collection_runs VALUES (
    'legacy-run', 1767225600, 1767312000, '2026-01-02T00:00:00Z',
    '2026-01-02T00:01:00Z', 'completed', 1, NULL
);
INSERT INTO collection_slices VALUES (
    'legacy-run', 1767225600, 1767312000, 'completed', 1, NULL, NULL,
    '2026-01-02T00:01:00Z'
);
