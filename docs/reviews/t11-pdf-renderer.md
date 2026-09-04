# T11 professional HTML/PDF renderer review

Date: 2026-09-01. Status: implementation, host verification and native repair
review complete. The user cancelled DeepSeek Harness as a project dependency;
no DeepSeek result is required for T11 and no further call or recharge is
expected.

## Delivered

- Self-contained, escaped print HTML generated only from
  `client-export-dataset/v1` and the fixed template preflight.
- Explicit Node Playwright driver with a hash pin, blocked network requests,
  temporary HOME/TMPDIR, no runtime browser/font download, browser watchdog and
  Python process-group timeout backstop.
- A4 landscape tagged PDF with page numbers, metadata, searchable text,
  embedded image/logo bytes and a caller-supplied, SHA-bound local CJK font.
  The font must decode as a real OTF/TTF, cover every CJK character required by
  the current dataset and fixed template labels, load as the named browser
  FontFace, and remain embedded in the final PDF before identity is reported as
  verified.
- Seven fixed content sections, 2x2 storyboard primary pages, explicit
  continuation cards with repeated shot identity, VO/on-screen timeline,
  audio/rhythm table and evidence/limitations continuation.
- HTML/XLSX marker parity across summary, dialogue, on-screen text, camera,
  music, SFX, rhythm and transition fields.

## Verification

- Dedicated PDF runtime (`VEW_REQUIRE_PDF_RUNTIME=1`): **11/11 passed**.
- T11/T10/T09/T08 export boundary: **45/45 passed**.
- Targeted Ruff and compileall: PASS. Repository-wide Ruff remains a later
  quality-gate task and currently reports pre-existing findings outside T11.
- Real Playwright 1.60 + Chrome 152 output: A4 landscape, tagged, searchable,
  image-bearing, metadata-bound and font-embedded.
- 205-shot real PDF: at most 65 pages; no shot truncation.
- Long-text PDF: first/end markers retained and shot identity repeated.
- Core full suite: **427 passed, 20 skipped, 345 subtests passed; 1 failure**.
  The only failure remains the intentionally stale UI acceptance receipt
  (`113 != 182` files at that readback).

## Review disposition

Native cold review initially found missing client fields and an invalid external
font-verification dependency as HIGH. Follow-up rounds closed those findings,
plus logo rendering, BCP47 language, continuation identity, chronological
VO/on-screen ordering, empty transitions and mixed embedded/unembedded font
validation. A later review reproduced two false identity paths: corrupt font
bytes and a valid Symbol font without CJK glyphs. Both now fail before final
output publication. The final native repair verdict found no remaining
HIGH/MEDIUM and no over-optimization or compatibility regression in this scope.

DeepSeek session `session-8b2abc83-34be-4862-b0de-d45ca8518f5e` completed its
first three turns. It found and verified fixes for raw PDF text comparison,
metadata leakage, footer size, 4-up geometry, runtime enforcement and caption
regression. Final historical delta RPC `8501f3ac-8d04-4205-a827-b9944a7c8517` was accepted
but ended with `completionReason=error`, `QUOTA 402`; the immediately preceding
turn ended for the same reason. The latest font, cross-format and timeout changes
therefore did not produce a completed DeepSeek verdict. This remains historical
evidence only and no longer blocks T11.

The repository CI does not yet install and require the real PDF runtime, so a
fresh GitHub runner can skip browser-backed PDF checks. That integration belongs
to the later cross-platform quality-gate work; it is an explicit residual, not a
T11 success claim and not a reason to expand this renderer into a runtime
installer.

Temporary PDF/HTML/PNG artifacts are removed after visual inspection. T12 still
owns locks, idempotency, staging recovery, atomic current publication and saved
versions; normal analysis and Finalize do not call this renderer.
