# Artifact registry schema v1

Status: implemented for report and explicit current/saved client-export lifecycle coordination.

The registry is stored at `data/artifact_registry.json`. It is project-local metadata; it is not a replacement for the report-generation receipt in `project_manifest.json`. A report is current only when the existing manifest verification passes.

## Root object

The object contains exactly:

| Field | Type | Contract |
| --- | --- | --- |
| `schema_version` | integer | exactly `1` |
| `project_id` | string | must equal the project directory name |
| `revision` | integer | non-negative optimistic revision; boolean is rejected |
| `artifacts` | array | strict records, unique by artifact ID and relative path |

Unknown keys, invalid UTF-8, non-finite JSON constants, unsafe paths, duplicate IDs, duplicate paths, and unsupported enum values fail closed.

## Artifact record

Every record contains exactly:

| Field | Values / type |
| --- | --- |
| `artifact_id` | bounded lowercase identifier |
| `scope` | `analysis`, `review`, `report`, `client_export` |
| `kind` | `file`, `directory`, `manifest`, `package` |
| `relative_path` | canonical project-relative POSIX path |
| `state` | `staging`, `current`, `stale`, `saved`, `failed`, `cancelled`, `superseded` |
| `retention` | `project`, `current`, `saved`, `transient` |
| `generation_id` | bounded string or `null` |
| `source_generation_id` | bounded string or `null` |
| `digest` | strict SHA-256 receipt or `null` |
| `stale_reason` | bounded string only when state is `stale`; otherwise `null` |

Durable states (`current`, `stale`, `saved`, and `superseded`) require a digest and generation ID. Durable report and client-export entries also require a source-generation binding. Client report files are confined below `reports/client/`.

## State transitions

```text
staging → current → stale → superseded
    │         └──────────→ superseded
    ├→ failed
    └→ cancelled
```

Terminal entries cannot return to `current`. **Save as version** copies a verified current package into a new, uniquely identified `saved` record below `reports/client/saved/<version-id>/`; it does not mutate the current record or its path. An idempotent retry is accepted only when its transition payload matches the stored record. A revision mismatch is a conflict.

## Publication behavior

- A missing registry is accepted as a legacy empty state and is not created by a read.
- Before a review mutation, an existing registry must validate. Corruption blocks the mutation before the previous report manifest is invalidated.
- A review mutation marks current report and client-export records stale. It never renders or schedules an export.
- Report generation writes the manifest `publishing` marker before updating lifecycle state.
- The registry records the candidate committed report receipts before `project_manifest.json` is written last as the authoritative commit marker.
- If a secondary write fails, manifest verification remains fail closed; registry state alone never authorizes a deliverable.

The static artifact catalog in `src/video_analysis_mvp/artifacts.py` is the canonical source for artifact identity, path, profile eligibility, workspace display metadata, and professional-export classification. Runtime existence, readiness, digest verification, and file-download authorization are still recomputed from current project evidence.
