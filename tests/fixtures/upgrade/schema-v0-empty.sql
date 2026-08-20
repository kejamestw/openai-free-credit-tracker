-- The v0.2 contract predates persistent history.  This materializes a valid
-- SQLite schema-0 database so the initialization path is exercised.
PRAGMA user_version = 0;
