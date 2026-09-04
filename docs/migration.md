# Legacy project migration and rollback

Current target: readiness schema v3 and report-generation schema v4.

Migration is explicit and two-stage. It never regenerates media, invents review,
or creates client XLSX/PDF.

## 1. Inspect

```bash
.venv/bin/analyze-video --workspace ./analysis-projects \
  migrate example-video
```

Possible states:

| Status | Meaning | Action |
|---|---|---|
| `current` | Current schema and report bindings verify | None |
| `finalize_required` | Schema is current but evidence/report is stale | Resolve readiness and Finalize |
| `migration_required` | Supported older readiness/report schema detected | Re-run with `--apply` |
| `prepared` | Legacy publication is stale and a migration receipt requires Finalize | Use the normal review/Finalize path |
| `recovery_required` | A previous apply stopped after creating its private backup | Re-run with `--apply`; inspection itself does not change files |

Unsupported, malformed, symlinked, or incomplete project metadata fails before
any write.

## 2. Prepare explicitly

```bash
.venv/bin/analyze-video --workspace ./analysis-projects \
  migrate example-video --apply
```

The apply transaction:

1. takes the existing project, shots, then client-export locks in that order;
2. writes a private 0700 backup of the exact manifest, artifact registry, and
   previous migration receipt;
3. commits a backup-state record before any project mutation;
4. changes the manifest to `review_pending`, removes the legacy report
   generation commit, and marks current report/client-export registry records
   stale while preserving saved versions;
5. writes `data/migration_receipt.json` with only schema versions, project ID,
   target versions, and `requires_finalize=true`;
6. validates that no report/client artifact remains current;
7. removes the private backup only after success.

If a handled write fails, the original metadata bytes are restored. If the
process stops after the backup-state commit, a read-only inspection reports
`recovery_required`; the next explicit `--apply` restores the incomplete
transaction before continuing. Multiple or unsafe backup directories fail
closed for operator inspection.

Re-running `--apply` on `prepared` or `current` is a no-op.

## 3. Re-Finalize and export

Review unresolved shots and audio first, then choose **Finalize package** or run:

```bash
.venv/bin/analyze-video --workspace ./analysis-projects report example-video
```

Verify readiness/report generation, then explicitly create a new client package
if needed. Migration never reuses an old current export as current and never
deletes an explicitly saved version.

## What is deliberately not migrated

- ingest/master/review media bytes;
- keyframes or deterministic audio measurements;
- human review decisions;
- provider credentials or raw responses;
- saved client versions;
- unsupported future schema versions.

Pin the repository revision and retain your own project backup before migrating
consequential or irreplaceable work. The built-in backup is a short-lived
transaction rollback mechanism, not long-term archival storage.
