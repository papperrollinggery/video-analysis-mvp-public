# T13 audio review CLI/API: implementation and independent review

Date: 2026-09-01 (Asia/Shanghai). Scope: shared audio query/operator-review service, CLI, built-in HTTP and optional FastAPI. This is not UI, model-accuracy, customer-template or release acceptance.

## Delivered

- One `audio-review/v1` service: event/shot/status filtering, bounded pages, current generation, original/effective proposal and capability status.
- Explicit operator confirmation, generation/proposal compare-and-swap, preserved analysis parameters/input bindings, sparse review overrides and explicit blank semantics.
- Existing descriptor-based timeline transaction and report/export invalidation reused. Identical requests with a current digest are no-ops; failed writes never claim a confirmed save.
- CLI `audio-review list/show/apply`; old `audio` remains available. Both HTTP adapters use the same service; optional adapter does blocking work off the event loop and publishes an explicit OpenAPI request schema.
- [Operator/Codex guide](../audio-review.md), including missing-capability distinctions, concurrency recovery, human-review boundaries and explicit Finalize. Reading, reviewing and Finalize create no PDF/XLSX.

## Findings and disposition

| Source | Finding | Disposition |
| --- | --- | --- |
| Local red/green regression | Omitting overrides/notes discarded prior operator edits | PATCH now preserves omitted fields; explicit `{}`/`""` resets them. Rejection clears omitted overrides by contract. |
| Native independent review | Valid colon event IDs rejected after URI escaping, especially FastAPI | Decode only the audio event path segment once and retain canonical ID validation. Real HTTP tests cover raw/encoded colon, encoded slash and double encoding. No global path rewrite. |
| DeepSeek F1 | FastAPI lacked `/api/session`, forcing an adapter-specific client branch | Added one alias to the existing `/session` function. Both names return the same token; both test servers now use the same readiness/session path. |
| DeepSeek F2 | `invalid_review_file` missing from the guide | Added the 400 row. Corrected the review's oversized-file example: the implementation already returns 413 `request_too_large`, not this 400 code. |
| DeepSeek F3 | Unavailable response omitted `counts_scope` and `data_trust` | Added the same two fields to the normal unavailable branch; unknown counts remain unknown, not fabricated zero coverage. |
| DeepSeek F4 | Read path loads/parses a receipt whose parameters only mutations need | Deferred the optional `with_receipt` branch: it would save a few KB but add a read/write branch, and current measurements do not justify it. This is not a zero-risk or necessary repair. All full-source freshness checks stay. |
| DeepSeek F5/F6 | Pre-existing duplicate digest and cleanup-state visibility | Recorded, not expanded into shared-receipt/report-binding changes. `audio-review list/show` does not expose recovery-directory details; this does not imply no read-only status function exists elsewhere. |
| DeepSeek F7 | Failed writes can invalidate reports without a machine-readable invalidation flag in the error | Existing fail-closed ordering and disclosed reload requirement retained. T15 must refresh state after a failed save; no automatic Finalize or retry-as-overwrite. |

DeepSeek accepted the reviewed snapshot for this internal local-operator capability, with only minor/informational suggestions. Its recommendation to avoid speculative caching was adopted; its characterization of every suggested edit as “zero-risk” was not. Project-ID URI handling, shared validation/digest refactors, and extra architecture were not changed merely to satisfy a reviewer preference.

## Independent receipts

- DeepSeek session `session-689ca1f1-b5f8-453b-902c-f3571253a894`; RPC `361beb21-7698-4ae0-9328-4e82c5e84ec3`.
- Recorded provider/model/effort: `deepseek-official`, `deepseek-v4-pro`, `high`; preset `code`, scope `proposal-only`.
- Scoped local snapshot: `tmp/.t13-deepseek-audit-20260901`; no customer media or credentials copied. `RESULT.md`, `OPINION.md`, `STATUS.json` and live state were read back: `done`, `completionReason=completed`, `running=false`.
- DeepSeek ran its permitted two-file subset twice: **17 passed, 15 subtests**, exit 0 each; these are overlapping runs, not 34 unique tests. The then-current four-file host target had 28 tests; the later additive legacy-CLI test makes the current target 29. It did not run the full suite or the host's performance fixtures.
- Its final review did not cover the subsequent alias/payload-key patch. That patch has explicit red/green regressions and a separate native final-delta review; do not represent it as another DeepSeek-approved revision.
- Original review prose is retained unchanged. The source snapshot is one ~260 KB archive, with a 50-file source/test/schema hash manifest and a dry-run-verified final local delta. Archive SHA-256: `d1b6284e899e4499a0ff781377ce5bd49f7520c7566619930ef74b9f3ddd7f76`.

## Current verification

- Full suite including the additive legacy-CLI test: `401 passed, 319 subtests passed; 1 failure`, exit 1. The only failure is the already-stale UI candidate screenshot receipt (`113 != 163` files). It has not been updated without new browser evidence.
- Current targeted both-adapter tests after the DeepSeek follow-up fixes: **29 passed, 22 subtests passed**, exit 0. Alias/payload assertions failed before the patch and passed after it. Live OpenAPI checks now verify list query/path parameter merging as well as the mutation request body.
- Final native delta review: **18 passed, 15 subtests passed**, exit 0; no CRITICAL/HIGH/MEDIUM/LOW finding on the small post-DeepSeek patch. This overlaps the host target and is not added to its count.
- New/modified small-module Ruff checks and Python compile pass. Broader lint on the three existing entry modules is not clean: after fixing one new import-format finding, the 24 remaining code/message signatures match the T06 snapshot exactly (API 2, CLI 3, workspace API 19). No unrelated formatting sweep was performed. Git is unborn/untracked, so `git diff --check` does not constitute source coverage or a historical-diff review.
- Real HTTP tests check both servers, CSRF/origin/duplicate JSON/size guards, compatible event lookup and cross-process concurrent mutation: one save, one 409. Explicit Finalize and identical-review no-op preserve correct report state. Zero automatic client exports.
- Synthetic 1,000-event query: 50-event page, 42,743 response bytes; three-read median 0.257 s **with tracemalloc**, ~68.2 MB peak traced Python allocation.
- Synthetic one-hour mono16k WAV (115,200,044 bytes), 1,000 events and a middle page: three-read median 0.098 s **without tracemalloc**, 43,286 response bytes, zero exports. These two timings are not directly comparable; both are host observations, not throughput guarantees or real audio accuracy.
- Temporary fixture media/server children are cleaned by the test contexts. No customer media, provider credentials or actual audio model is used in this stage.

## Over-optimization and residual scope

Keep the existing file schema/transaction/lock machinery and one shared service; no database, cache, queue, auth framework, new renderer or extra runtime dependency was added. The optional API packages installed for verification were already declared project extras.

Pagination bounds transfer size, not all computation: each query still verifies current input bytes and projects the timeline. The guide forbids frame-rate polling as a client pattern. Large simultaneous reads, worst-case 64 MiB timeline memory, waveform navigation, real-model semantics and professional exports remain separate acceptance items. The existing bounded-file reader can transiently allocate near its read cap; resource hardening belongs to T18, not an unmeasured caching rewrite here.

Overall product verification remains partial because UI receipt, broader lint, semantic accuracy and later product Tasks are not complete. The mature-product Goal remains active; T14/T15 provide the next visible audio-review workflow.
