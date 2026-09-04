# T05 independent review and optimization decisions

Date: 2026-09-01 (Asia/Shanghai). Scope: local audio baseline, integration boundaries and the product execution plan. This is a local candidate review, not a release approval or real-model accuracy certificate.

## Independent review receipt

- Harness session: `session-227d0aa7-0ec5-4e8a-b646-3b6d5e6acae7`.
- Recorded provider/model/reasoning: `deepseek-official` / `deepseek-v4-pro` / `high`.
- Preset/scope: `code` / `proposal-only`.
- Working directory: `tmp/t05-deepseek-audit-20260901` (private, ignored review snapshot; no client media or credentials copied).
- Initial delegate: `STATUS.json.status=done`, `completionReason=completed`; first review ran 99 targeted unittest tests successfully.
- Two follow-ups used the same session. Their exact RPCs were awaited separately: `ce0559f9-408b-450d-9b9c-25a988bad162` and `c955d2e9-68ff-4130-8ebd-042d46951d3e`, both `completed`. Follow-ups ran 70 and 13 tests respectively; these overlap the initial suite and must not be summed as unique tests.
- Final live state: `running=false`. The helper's `STATUS.json` remains bound to the initial delegate, not follow-up RPCs; `FINAL_TURN_RECEIPT.json` records the final completed turn separately.
- Main-agent readback verified all three review reports, 75 cumulative snapshot hashes, and equality of all 65 copied source/test files with the current workspace. No reviewed source was modified by Harness.

## Confirmed defects repaired

| Defect | Minimal repair and evidence |
| --- | --- |
| Silence/zero-confidence PCM summary described as music-led | Legacy consumers respect zero identity confidence; report says identity unknown; human/provider sound conclusions survive. Integration regression passes. |
| Explicit ASR failure hidden by full-pipeline success | Full pipeline returns warning and exposes ASR status/reason. Regression passes. |
| Huge numeric ASR timestamps escape error handling | Parsing/conversion failures remain ASR failed while baseline publishes. 400/5000-digit regression passes. |
| `link/../checkpoint` hashed different bytes from CLI lookup | Reject parent traversal before binding/execution. Independently reproduced and verified by DeepSeek. |
| ASR character limit diverged from timeline UTF-8 contract | Reuse existing `bounded_text`, including byte limit, NUL and surrogate checks. Chinese/NUL/surrogate integration regressions pass. |

## Over-optimization and negative-optimization decisions

Accepted: keep the evidence/integrity safeguards; expose the audio review loop before completing every export renderer; use incremental UI changes; use one fixed professional template; keep heavy diarization/source-separation optional; reuse final verification evidence for an unchanged candidate.

Not accepted: removing or indefinitely postponing professional PDF, treating ASR plus energy as complete music/SFX/VO semantics, renaming stable schema fields merely for wording, deleting compatibility functions solely for line count, or merging both transaction implementations during feature delivery. PDF and audio semantics are explicit user requirements. DeepSeek acknowledged these corrections and withdrew its unsupported no-consumer assumption.

The task plan preserves 25 separately trackable Tasks, with a valid acyclic dependency graph. T13 audio API/review no longer depends on exports; export endpoints belong to T12. T14 is a minimum incremental redesign in the existing shell, not a framework migration or whole-app rewrite. T07 semantics and T11 PDF remain required. See [the revised product plan](../audio-intelligence-client-export-plan.md).

## Workspace verification

- Final code suite: `.venv/bin/python -m pytest tests -q` → **353 passed, 8 skipped, 291 subtests passed; 1 failure**. The failure is the pre-existing stale final UI screenshot receipt (`test_ui_acceptance_receipt_binds_current_assets_and_screenshots`), reserved for fresh T22 visual acceptance. It was not rewritten to fake a pass.
- Ruff on the four new audio source/test files: passed. Python compile check: passed. This is not a claim that repository-wide strict lint is clean.
- Real FFmpeg synthetic pipeline: static tone and digital-silence samples both complete ingest/visual/audio/report with valid audio bindings; silence remains identity unknown, and no PDF/XLSX is generated.
- Six-case synthetic benchmark: 6/6 functional and 5/5 accuracy-gated cases pass; fade/dissolve remains observational. All six audio timelines bind correctly; zero customer documents generated.
- Host observation: 60-second 16 kHz mono sine PCM measurement plus timeline generation took 1.527 seconds, about 2.73 MB peak Python allocation. Not a cross-machine or real-ASR performance guarantee.
- All synthetic media/test workspaces were temporary and cleaned. Review source copies are retained as one compressed snapshot plus small review receipts, avoiding duplicate test discovery and unbounded generated-file accumulation.

Overall verification: **partial**. Real Whisper transcription accuracy, real provider inference, full audio review/export UI, professional Excel/PDF rendering and final release acceptance remain unverified or pending their Tasks. T05 baseline can advance; the overall mature-product goal is not complete.
