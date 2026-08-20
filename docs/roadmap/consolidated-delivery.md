# Consolidated delivery decision

The v0.2.0 through v1.0.0 roadmap items are implemented as one forward-moving v1
release-candidate line. The project will not publish synthetic intermediate tags
whose artifacts contain later-version features: doing so would create misleading
historical binaries and untested downgrade paths.

Each version document remains an acceptance inventory. Implemented checklist items
may move to `待驗收`, while external gates remain open and visible. The consolidated
candidate must preserve the config, database, export, catalog, API, updater, and
platform compatibility contracts documented under `docs/contracts/`.

## Release sequence

1. Produce `v1.0.0-rc.1` from the complete implementation and attach immutable,
   checksummed candidate artifacts and automated evidence.
2. Run native clean-machine, signing/notarization, credential/tray/notification,
   update/rollback, accessibility, real-API, independent security/documentation,
   and 72-hour soak gates.
3. Correct findings and produce at least `v1.0.0-rc.2` from a new commit. Exercise
   the signed RC1-to-RC2 update and rollback path without rebuilding either RC.
4. After the observation period and all P0 gates pass, create the protected signed
   `v1.0.0` tag and stable Release from the already verified stable candidate bytes.

Unreleased v0.x snapshots are migration fixtures, not supported public releases.
The stable support promise starts only when the v1.0.0 gate is approved.
