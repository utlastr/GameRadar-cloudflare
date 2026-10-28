# Project Snapshot: 2026-10-28

- Repository: `utlastr/GameRadar` (private).
- Branch: `master`.
- Snapshot tag: `snapshot-20261028-142149`.
- Scope: current project source, tests, documentation, tools and verification
  outputs, including current tracked deletions.
- The remote default branch `main` is not replaced or merged by this backup.
- Excluded by existing ignore rules: `radar_data/` (database, credentials,
  settings, runtime caches and logs), Python bytecode, local backup and
  quarantine folders.

## Verification

The video homepage and related targeted suite passed all 60 tests before
packaging.

The complete suite was run with:

```powershell
python -X utf8 -m unittest discover -s tests
```

Result: 347 tests run, 17 errors, no assertion failures reported.

- 13 component-update tests fail during setup because `config.UPDATE_SCOPE`
  does not exist in this project snapshot.
- Four test modules cannot import removed social modules:
  `test_douyin_gate`, `test_publications`, `test_social_growth`,
  `test_social_schedule`.

These errors are recorded rather than changing application behavior during
the backup. This snapshot is not a claim that the complete test suite passes.
