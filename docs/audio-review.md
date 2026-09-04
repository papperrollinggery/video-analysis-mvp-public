# Audio review: one workflow for CLI, HTTP and Codex

The audio review service reads the existing input-bound timeline and saves explicit operator decisions. It does not run a second analyzer, transcribe audio, call a provider, or generate customer files. Both HTTP servers and the CLI use this same service and `audio-review/v1` response.

The workflow is: **audio evidence → inspect original and effective events → operator review → explicit Finalize → explicit customer export**. The audio review UI and professional Excel/PDF export surfaces are implemented as separate explicit actions; the CLI remains available without opening the UI.

## Inspect with the CLI

Use an existing project created by the tool. If its audio timeline has not been produced, run the [audio stage](audio-baseline.md) first; a missing timeline is not silence.

```bash
analyze-video --workspace ./analysis-projects audio-review list PROJECT_ID \
  --kind voice --review-status needs_review --limit 50

analyze-video --workspace ./analysis-projects audio-review show PROJECT_ID EVENT_ID

analyze-video --workspace ./analysis-projects audio-review list PROJECT_ID \
  --shot-id shot_0001
```

Every event includes its original `proposal`, `proposal_sha256`, current `review`, and `effective_proposal`. Original machine text is never overwritten. Reviewed overrides affect the effective view; rejected or `needs_work` events have no effective proposal. An explicit empty text override remains empty, rather than falling back to the old transcript.

List filters are optional: `kind` (`voice`, `music`, `sfx`, `silence`, `mixed`), `review_status` (`unreviewed`, `reviewed`, `rejected`, `needs_work`, `needs_review`), and `shot_id`. `needs_review` uses the same predicate as the report's association layer; it is not synonymous with every unreviewed baseline measurement.

Pagination uses `offset` (default 0) and `limit` (default 50, maximum 200). `page.total` is the filtered event count; `next_offset: null` means the end. For later pages, pass the first response's `generation_id` as `--expected-generation-id` to prevent mixing revisions. On conflict, reload from the first page. Counts explicitly marked `counts_scope: "all audio events"` are computed before filters and pagination. A shot summary describes all overlapping events, not only the displayed page; links use the [same half-open associations](schemas/audio-associations-v1.md) as reports.

Pagination bounds response size, not validation work: each request verifies current source bindings and constructs the event view. Clients should load on open/filter/page/review actions, not poll this endpoint on every playback frame. Large-media concurrent throughput is a separate performance acceptance item; no persistent cache or database is introduced here.

## Save an operator decision

Inspect/listen to the relevant evidence first. Copy the current response's `generation_id` and event's `proposal_sha256` into a local `review.json` file. This is a request example: the placeholders must be replaced with actual 64-character lowercase SHA-256 values.

```json
{
  "expected_generation_id": "COPY_CURRENT_GENERATION_ID",
  "expected_proposal_sha256": "COPY_CURRENT_PROPOSAL_SHA256",
  "status": "reviewed",
  "overrides": {"text": "Corrected VO text"},
  "review_notes": "Checked against the original audio at this event interval.",
  "confirm_operator_review": true
}
```

```bash
analyze-video --workspace ./analysis-projects audio-review apply PROJECT_ID EVENT_ID \
  --request ./review.json
```

The JSON file must be a regular, non-symlink file of at most 1 MiB. Unknown fields, duplicate keys, non-finite values, invalid types and oversized field content are rejected. Override fields follow [audio-timeline/v1](schemas/audio-intelligence-v1.md); event identity, source and interval cannot be changed through this review endpoint. The server assigns review verification, so callers cannot mark model output as human-verified by overriding that field.

| Request | Meaning |
| --- | --- |
| `status: "reviewed"` | Operator accepts the proposal plus valid overrides. |
| `status: "rejected"` | Exclude the event's proposal from effective output; overrides must be empty. |
| `status: "needs_work"` | Retain a draft decision without treating it as accepted evidence. |
| Omit `overrides` / `review_notes` | Preserve the prior review value, or use an empty value on first review. When rejecting, omitted overrides become empty. |
| `overrides: {}` / `review_notes: ""` | Explicitly reset that review field. |
| `overrides: {"text": ""}` | Explicitly clear effective text. |

`confirm_operator_review` is an assertion of a real operator decision, **not authentication or proof of human identity**. Codex must not set it merely because it generated an analysis. Native model analysis belongs to the existing prepare/apply evidence workflow; the model must not bypass it, claim unheard audio was inspected, or silently approve its own proposal. Audio semantic adapters are still pending; baseline energy does not establish music, SFX or VO identity.

## Finalize is separate from review

A successful mutation returns `review_saved: true`, the new `generation_id`, `report_regeneration_required: true`, and `exports_generated: false`. It invalidates old report/export readiness before publishing the review. Refresh the event after saving; the old expected generation cannot be reused for a different edit.

After all intended reviews, explicitly run:

```bash
analyze-video --workspace ./analysis-projects report PROJECT_ID
```

Finalize rebuilds the normal bound report package. **It does not generate PDF or Excel.** Saving an identical review is a no-op and does not invalidate an already current report. A stale retry is a conflict, not an automatic merge. If a commit fails, the response does not claim it was saved; reload the timeline before retrying. The report may remain pending after a failed write as a conservative safety measure.

## HTTP contract

Built-in server:

```bash
analyze-video --workspace ./analysis-projects serve --port 8765
```

| Method | Route | Purpose |
| --- | --- | --- |
| GET | `/api/projects/{project_id}/audio` | Same filtered, paginated view as CLI `list`. |
| GET | `/api/projects/{project_id}/audio/events/{event_id}` | Same single-event view as CLI `show`; accepts only optional `expected_generation_id`. |
| PATCH | `/api/projects/{project_id}/audio/events/{event_id}/review` | Same strict JSON request as CLI `apply`; no query options. |

Use URI encoding for individual path segments (`voice:1` may be `voice%3A1`), not the entire route. Event IDs are decoded once then validated; encoded slashes and double-encoded IDs are not alternate identifiers.

Mutations require `Content-Type: application/json` and `X-VEW-CSRF` with the local `/api/session` token. Browser requests must be same-origin. These protections and loopback host/client checks are for a trusted local operator; this is not a public multi-user authorization API. Do not expose the service to untrusted networks. Session tokens are local mutation guards, never provider API credentials.

The optional FastAPI adapter also exposes these routes with or without the `/api` prefix, serves the same token at `/api/session` and its legacy `/session` alias, and accepts its existing `workspace` query selector. The selector is removed before domain validation; no other PATCH query options are accepted. Its `/openapi.json` documents the review request and list filters. Raw request bodies preserve duplicate-key detection; because direct request access bypasses automatic validation/documentation, the shared validator and explicit schema remain authoritative ([FastAPI documentation](https://fastapi.tiangolo.com/advanced/using-request-directly/)). Blocking filesystem/lock operations run in the adapter's thread pool.

HTTP domain failures and CLI domain failures share:

```json
{"error":{"message":"Audio changed; refresh before continuing","details":{"code":"stale_generation"},"status":409}}
```

| Status / code | Action |
| --- | --- |
| 400 `invalid_query`, `invalid_event_id`, `invalid_review`, `operator_confirmation_required` | Correct the request; nothing is applied. |
| 400 `invalid_review_file` | Use an existing regular, non-symlink request file containing strict JSON. Oversized files return 413 instead. |
| 404 `audio_unavailable`, `event_not_found`, `shot_not_found` | Check the project/event or produce evidence first. A list without any timeline returns `available: false` and unknown capabilities. |
| 409 `stale_generation`, `stale_proposal`, `audio_state_changed` | Reload; do not overwrite another revision blindly. |
| 409 `audio_invalid`, `visual_invalid` | Repair/regenerate the affected evidence before review. |
| 413 `request_too_large` | Reduce the request/file size. |
| 500 `audio_commit_failed` | Save is unconfirmed; reload and inspect project state. |

Transport guards can return their existing server-specific error format (for example, forbidden origin or an oversized body); clients must check HTTP status before parsing the domain envelope. The CLI exits 0 on success and 1 on domain failure; argument parsing retains its normal usage errors.

## Verification boundary

```bash
.venv/bin/python -m pytest tests/test_audio_review.py tests/test_audio_review_http.py \
  tests/test_fastapi_api.py tests/test_fastapi_body_limit.py -q
```

Tests use temporary synthetic projects. They cover shared output, real HTTP on both installed adapters, cross-process compare-and-swap, escaped IDs, request guards, explicit clearing, failed commits, no-op behavior and absence of automatic customer exports. They do not establish real transcription/music/SFX accuracy, human listening, browser layout acceptance, or final-product release readiness.
