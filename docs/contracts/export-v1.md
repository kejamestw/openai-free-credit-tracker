# Export schemas v1

CSV is UTF-8 with a header and `\n` line endings. Its fixed v1 columns are:

`bucket_start_utc,bucket_end_utc,profile_id,project_key,model,service_tier,input_tokens,cached_input_tokens,output_tokens,request_count,catalog_version,completeness`

Integer fields use base-10 digits and timestamps use RFC 3339 UTC. Text beginning
with `=`, `+`, `-`, or `@` is prefixed with a single quote to prevent spreadsheet
formula execution. Project identifiers default to masked; users may select masked,
excluded, or explicit inclusion before export.

JSON uses an envelope containing `schema_version`, `generated_at`, `filters`,
`time_zone` (`UTC` for stored values), and full-resolution `records`. UI chart
downsampling never affects exports. Both formats use atomic writes; cancellation or
failure removes the temporary file and never leaves a plausible partial export.
