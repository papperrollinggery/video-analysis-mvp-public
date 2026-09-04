# T08 client-export-dataset/v1 implementation review

Date: 2026-09-01. Scope: the single structured input for later XLSX/HTML/PDF renderers. No renderer, export transaction, CLI/API export command or customer file is included here.

## Result

- One deterministic `client-export-dataset/v1` builder and strict nested runtime validator.
- Read-only build and explicit stable `data/client_export_dataset.json` write remain separate. Normal analysis/Finalize do not call either path.
- A blocked committed report is represented as `draft_only`, never professional permission.
- The dataset binds the exact media/shots/scenes/visual-receipt/visualization bytes it actually read to the first committed report's receipts, then compares final manifest bytes and verifies the report again.
- Every shot and scene is covered exactly once with bidirectional membership. Every audio event is preserved once, indexed by kind and linked to shots with correct continuation/kind semantics.
- Original/effective/rejected/needs-work values remain separate. Missing frame is an explicit two-state record, not an invented image.
- Renderer-visible free text, including timecode and proposal language, uses raw `text` plus formula-neutralized `spreadsheet_text`. Private path/credential checks are Unicode-aware without rejecting common slash-based production wording.
- Repeated builds are stable; the explicit writer replaces one JSON slot and creates no PDF/XLSX.

Contract: [client-export-v1](../schemas/client-export-v1.md).

## Independent findings and disposition

Native review initially found four HIGH issues: naked formula fields, A/B/transient generation mixing, incomplete nested validation, and dropped audio-link fields. It also found slash-language path false positives. All were reproduced or regression-pinned and fixed. A follow-up found duplicate shot membership inside one scene; it now fails in both builder and validator. Final native verdict: no remaining HIGH/CRITICAL; one duplicate-membership MEDIUM was fixed afterward.

DeepSeek Harness independently verified the receipt mapping, digest/delivery logic and 56-test related boundary. Its first pass found no core digest/TOCTOU/coverage defect, but identified an over-broad `/pricing` path match, a fullwidth-path bypass, Windows title handling, a neutralization byte edge, readiness snapshot comparison and redundant full-data copies/scans. Adopted: narrower multi-segment POSIX detection, NFKC inspection, readiness equality, `PureWindowsPath`, formula-prefix budget, fail-closed malformed lists and removal of two full deep copies plus duplicate TextCell reconstruction. Rejected: cache, queue, database, schema framework, renderer scaffolding or splitting solely for line count.

DeepSeek session `session-1d0590f5-4ad5-47ab-abce-3d793331388b` completed initial RPC `86743191-a33f-4470-a321-1c36d2897cf3` and final delta RPC `911b5643-d651-4368-83e2-2b0dde8b79d6`; both returned `completionReason=completed`. The final delta review found no new HIGH/MEDIUM and explicitly confirmed digest, TOCTOU and nested-schema semantics remained intact. Harness Ruff was blocked by its cache policy; host Ruff passed.

## Verification

- T08: **11 passed, 10 subtests passed**.
- Report/audio/artifact boundary: **56 passed, 18 subtests passed**.
- Final full suite: **413 passed, 338 subtests passed; 1 failure**. The only failure remains the intentionally stale UI receipt (`113 != 169` files).
- One-shot/six-event host observation: 23,719 bytes, 0.076 s with tracemalloc, ~67 MB peak Python allocation, zero PDF/XLSX. This is not a scale guarantee; much of the transient allocation comes from existing bounded readers.

Overall product verification remains partial. T09–T12 still must prove template/layout, renderer consistency, explicit transaction and stable current/saved lifecycle; T19–T22 still own large-scale, platform, accessibility and frozen-candidate acceptance.

Local audit record: `tmp/.t08-deepseek-audit-20260901`; one 904 KB compressed snapshot (`sha256 7e0e4dbd1338ce78810256f62b145c668fc3a58d4b08ae663e17e5a2d8b5f08e`) plus the final dry-run-clean delta (`sha256 ad648596c7be7b0f7b1ce55a44e64500f87d8db145d249518f2c44e530206857`).
