# Client export dataset v1

`client-export-dataset/v1` is the only structured input allowed for professional Excel, print HTML and PDF renderers. It is a deterministic projection of one current committed report generation. Renderers must not reopen mutable project evidence after receiving it.

## Lifecycle boundary

- `build_client_export_dataset()` is read-only and returns an in-memory value.
- `write_client_export_dataset()` is an explicit operation that replaces only `data/client_export_dataset.json` atomically.
- Neither operation renders, Finalizes, publishes, versions, downloads or opens an XLSX/PDF.
- Analysis, audio review and Finalize never call this builder automatically.
- A missing/stale/invalid report generation fails before the existing stable dataset is replaced.
- A committed but blocked report produces `delivery_status.state=draft_only`; it is honest preview data, not professional-export permission. The export transaction still enforces current readiness before publishing client files.

## Top-level contract

| Field | Meaning |
| --- | --- |
| `schema_id` | Exactly `client-export-dataset/v1`. |
| `digest_algorithm` | Exactly `sha256`. |
| `dataset_id`, `dataset_digest` | Equal SHA-256 of canonical JSON with these two fields omitted. No timestamp enters the digest. |
| `source_bindings` | Current report generation ID/schema, manifest digest, complete report source receipts, readiness and shot digests. |
| `project` | Client-safe project title, profile, language, measured media timing/resolution. No source URL or absolute file path. |
| `delivery_status` | `professional` or `draft_only`, current readiness, professional-export boolean and reasons. |
| `field_semantics` | Evidence/interpretation boundary, text-cell rule, untrusted-data rule and `[start,end)` timing. |
| `scenes` | Ordered narrative ranges and exact shot membership. |
| `shots` | Every current shot exactly once, ordered by shot number/start/ID. |
| `audio` | Current source binding, capability status, client-safe source metadata, all events and kind indexes. |
| `limitations` | Deduplicated client-safe readiness/audio limitations, including the final-mix/stems boundary. |
| `unresolved_items` | Bound review gaps with only project-relative evidence references. |

The validator uses exact top-level keys, finite JSON, bounded collections/depth/bytes, unique shot/event IDs, safe frame paths and digest recomputation. A 64 MiB dataset, 20,000 shots or 100,000 audio events is rejected; later layout/render limits may be lower.

## Text cell

Every client-authored, machine-authored or provider-authored string uses:

```json
{
  "text": "=example",
  "spreadsheet_text": "'=example",
  "is_blank": false,
  "formula_neutralized": true
}
```

- HTML/PDF uses `text` and must HTML-escape it.
- Excel uses `spreadsheet_text` and must write it as a string cell.
- Leading whitespace is retained. Formula detection uses the trimmed first character and covers `=`, `+`, `-`, `@`, tab and carriage return.
- Explicit blank remains blank; renderers must not fall back to machine text.
- Private absolute-path tokens (`file:`, home-relative, drive-letter, UNC, and bare POSIX paths with at least two segments), NUL, credential-shaped values and strings above 256 KiB fail the dataset instead of leaking or silently truncating. Unicode-aware NFKC token checks keep ordinary production language such as `/pricing`, `室内/室外`, `日/夜`, `A / B` and `16:9 / 9:16` legal while rejecting `/Users/name/file`, `/etc/passwd` and fullwidth equivalents.

This dual encoding preserves renderer-neutral wording without making an Excel-safety apostrophe visible in PDF.

## Shot record

Each shot includes:

- strict ID, number, exact scene IDs, seconds, duration and a formula-safe timecode text cell;
- one primary-frame receipt: project-relative path, presence, digest, size, type, dimensions and a text-cell failure explanation;
- text cells for story beat, visual description, summaries, subject/action, screen text, dialogue, sound/music/rhythm, transitions and review notes;
- camera text cells;
- complete event links and coverage from the same `shot-audio-associations/v1` projection embedded in the committed visualization dataset;
- annotation source/verification, confidence, readiness state/reasons and project-relative shot evidence reference.

A missing frame is legal as `present=false`, with null file metadata and a visible failure text. It does not become an invented placeholder image. Renderers choose the versioned `missing-frame` layout.

## Audio record

Every event retains:

- ID, source, kind, `[start,end)`, identity status, review requirement and proposal digest;
- original proposal;
- effective proposal or explicit `null` for rejected/needs-work decisions;
- review status/verification and client-safe notes;
- project-relative evidence reference.

Proposal label, text and language are text cells. Speaker ID remains anonymous; role, numeric acoustic features, confidence and verification remain separately typed. Events are also indexed into `voice`, `music`, `sfx`, `silence` and `mixed` ID lists without duplicating content.

Unknown/failed/skipped capabilities are not silence. Music/SFX/VO identity is never derived from energy alone. Final-mix events are not represented as factual stems.

## Determinism and concurrency

The builder holds the existing shots lock, verifies the committed report, captures media/shots/scenes/visual-receipt/visualization bytes, and validates those exact captured values against the first manifest's readiness, visual-generation and report-artifact receipts. It then builds and validates the dataset, compares the final manifest bytes to the first snapshot, and verifies the report again. Cooperative mutations cannot interleave; short-lived direct-file substitutions cannot enter the result merely by restoring the manifest before the last check.

No generated time, hostname, username, temporary directory or absolute project path is serialized. Repeated builds over the same generation return byte-equivalent JSON and the same digest.

## Verification

```bash
.venv/bin/python -m pytest tests/test_client_export_dataset.py -q
```

The schema tests cover deterministic no-write builds, explicit stable-slot writing, no PDF/XLSX side effects, stale/transient generation failure without replacement, nested runtime-schema rejection after digest recomputation, formula-safe timecode/proposal language, path privacy without slash-language false positives, long CJK text, explicit rejected VO, mixed audio, event-link continuation/kind, bidirectional scene membership and strict present/missing-frame states. Renderer layout, font embedding, export transactions and browser behavior have separate tests; none of these establish real-model accuracy or a public release.
