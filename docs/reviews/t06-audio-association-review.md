# T06: audio association implementation and review

Date: 2026-09-01 (Asia/Shanghai). Result: the shot/narrative audio-association layer is implemented and its functional review findings are resolved. This is not a full-product, professional-template or release acceptance.

## Delivered

- A single read-only `shot-audio-associations/v1` projection, embedded once in the existing Codex/visualization dataset.
- Half-open shot/scene links, union coverage, original proposal/review preservation, effective text, explicit unknown/excluded states, and source/geometry/dataset digests.
- No invented word-level timing: cross-shot sentences reference full source events.
- Report generation v4 binds the audio timeline. v3 remains readable without a timeline; adding one requires explicit regeneration. Input changes during rendering cannot bind new audio to old output.
- All effective report/profile/storyboard text comes from the same reviewed event view. Original legacy transcript/SRT remains unchanged source evidence.
- Project-language summaries retain the separate beat-density label and expose linked RMS/pulse estimates without claiming music/SFX/VO identity.
- HTML is explicitly a chronological 240-link preview; complete links and text remain in JSON for future client renderers. Normal analysis still creates no Excel/PDF.

Contract and limits: [audio-associations-v1](../schemas/audio-associations-v1.md).

## Independent findings and disposition

| Source | Finding | Disposition |
| --- | --- | --- |
| Native cold review | Millisecond-rounded tail ranges rejected | Compatible with the existing 0.5 ms quantization only; original times unchanged. Up/down rounding and true overflow tests pass. |
| Native cold review | Profile analysis reused cleared raw ASR | Effective event text/music now feeds every delivery renderer; original SRT is preserved, not presented as the effective transcript. |
| DeepSeek | English summary overwrote rhythm labels and leaked into Chinese output | Separate translatable rhythm label retained; summary follows project language. |
| DeepSeek | Full per-shot link table serialized twice | Replaced the extra copy with a same-document JSON Pointer, including an order-mismatch regression/readback check. |
| DeepSeek | Unbounded HTML link table | Bounded to 240 rows with explicit chronological displayed/total notice and full-data reference. |
| DeepSeek | Honest unknown identity looked like no useful measurement | Linked RMS/pulse values are shown separately; no synthetic unidentified MusicProfile is reintroduced. |
| DeepSeek | Provider rhythm note absent from audit projection | Included in protected annotation and association digest. |
| DeepSeek follow-up | Missing/regional/capitalized language used a different rule | Reused existing `_delivery_lang`; six aliases/default cases plus independent native checks pass. |

No interval tree, module-splitting framework, new renderer framework or runtime dependency was added. The small legacy `build_report` fallback remains for direct callers. Cosmetic spacing and alternative preview sampling were not promoted into blocking refactors; the preview now states its chronological scope. Complete operator review/navigation belongs to T13/T15, not an ever-growing projection layer.

## Review receipts

- DeepSeek session: `session-4d3ea2d9-18f6-4f8d-b020-48620569861f`.
- Recorded provider/model/effort: `deepseek-official`, `deepseek-v4-pro`, `high`; `code` + `proposal-only`.
- CWD: `tmp/.t06-deepseek-audit-20260901` — local ignored snapshot, no client media/credentials copied.
- Initial delegate and follow-up RPC `20178707-777f-423f-9ab9-79d88971a7a7` both returned `completionReason=completed`; live state was read back as not running.
- DeepSeek independently ran 31 tests initially and 22 in repair verification; these overlap and are not a cumulative unique count. Original reports remain unaltered. Its last language-normalization finding was fixed afterward and verified by the native reviewer; the old DeepSeek verdict was not rewritten as a clean final approval.
- Final native check: the alias test (`1 passed, 6 subtests`), Chinese summary test (`1 passed`), six helper-equivalence probes, and preview wording check. Native runtime-model/cold-context isolation was not separately attested.
- All 70 DeepSeek-reviewed file hashes were checked. The final local patch against that snapshot was dry-run verified; current-file hashes and the native review are retained beside one compressed snapshot (~316 KB).

## Current verification

- `.venv/bin/python -m pytest tests -q`: **372 passed, 8 skipped, 297 subtests passed; 1 failure**. The only failure remains the old final UI screenshot receipt's candidate binding. T22 must capture new visual evidence; it was not overwritten to manufacture a pass.
- New module/test Ruff and Python compile checks pass; no repository-wide strict-lint claim.
- Real FFmpeg six-case benchmark: 6/6 functional and 5/5 accuracy-gated cases pass; fade/dissolve remains observational. Each generated report has valid v4/audio associations, with zero client exports.
- Final 60-second onset-rich synthetic sample: 720 events and complete shot links retained; HTML previews 240, Chinese/RMS/rhythm-label checks pass; v4 binding valid; zero PDF/XLSX. Removing the duplicate representation avoids 271,726 bytes in this fixture.
- Final pure-join scale check: 600 shots, 150 scenes, 1,201 events, 3,150 links → 0.520 s including JSON encoding and tracemalloc, ~15.6 MB Python peak, 2.36 MB JSON. Full long VO occurs only in the two audit representations, not once per shot. Host observation, not a universal throughput guarantee.
- Limit injection rejects excessive links without mutating source events or shots. Synthetic workspaces were temporary and cleaned.

Overall verification: **partial**. Full browser/layout acceptance, real-model semantics/accuracy, professional Excel/PDF templates, optional platform coverage and release acceptance remain pending. T06 is ready for the next dependent audio-review API/UI work; the overall mature-product goal is still active.
