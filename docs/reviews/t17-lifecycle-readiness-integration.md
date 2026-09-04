# T17 lifecycle, readiness, provenance, and stale integration

Date: 2026-09-01 (Asia/Shanghai). Status: implementation, lifecycle tests,
HTTP smoke, and native cold review complete.

## Integrated contract

- The existing `event_requires_review()` predicate is now part of readiness.
  A present, valid audio timeline reports its event count, unresolved-review
  count, completion state, and current audio-intelligence binding in readiness
  schema v3 / binding v2.0.
- Machine/model audio proposals and `needs_work` reviews remain blocked until an
  operator explicitly reviews or rejects them. Existing measured-event behavior
  is preserved. A missing timeline stays neutral `unknown, not silence`; this
  T17 repair does not retroactively block legacy projects merely because the
  structured timeline is absent.
- The workspace Finalize endpoint now preflights professional readiness. It holds
  the established project write lock through preflight, report generation, and
  final workspace readback, then verifies the final gate before returning 200.
  Concurrent in-process review writes serialize after Finalize; a cross-process
  change observed by the final readback returns 409 rather than a false success.
- The client-export transaction re-evaluates current persisted readiness while
  holding the shots transaction lock. UI, API, and CLI therefore cannot bypass
  an unresolved audio event, stale report generation, or forged readiness.
- Audio review mutation continues to invalidate report and current client-export
  registry entries before changing evidence. Explicitly saved versions remain
  immutable and downloadable; the old current package becomes visibly stale.
  A second explicit Finalize and a new export request are required for a new
  current package.
- The React workspace shows audio-timeline availability and audio-review
  completion in the same readiness strip and provenance panel used by shots,
  media, report generation, and export eligibility.

## Regression and acceptance evidence

- A professional-ready fixture with one unresolved machine VO proves both
  Finalize and client export fail closed. No XLSX/current package is created.
- The complete service lifecycle passes:
  `review audio → Finalize → export → save approved-v1 → mutate audio → current
  stale / saved retained → export blocked → re-Finalize → new current export`.
- A concurrency test pauses report execution, starts an audio mutation, and
  proves the mutation remains blocked until Finalize returns a ready snapshot;
  it then runs and correctly changes the manifest to `review_pending`.
- The real local HTTP review smoke now reviews each current `needs_review` audio
  event one at a time, reloading its generation/proposal digest after every
  compare-and-swap write. It passes:
  `blocked → shot/audio reviewed → finalized ready → mutation blocked →
  byte-identical restore still blocked → refinalized ready`.
- The CLI negative path runs through CLI → shared export service and proves an
  unresolved audio event returns failure without a current package. In the
  bundled Python with openpyxl 3.1.5, all four CLI export tests passed.
- Related service/API/UI regression: **91 passed, 4 skipped, 44 subtests**.
- Final core suite: **476 passed, 25 skipped, 358 subtests, 1 failed**. The sole
  failure is the intentionally stale T22 UI acceptance receipt (`113 != 194`
  candidate files); it was not refreshed without new frozen screenshots.
- Production Vite build, packaged frontend byte parity, proxy integration,
  frontend asset-gate self-test, changed-test Ruff, and Python compile checks
  passed.
- Native cold review ended `CLEAN`: no HIGH, MEDIUM, LOW, fake gate, or removable
  over-optimization remained.

## Over-optimization and compatibility check

No queue, database, workflow framework, new runtime dependency, or duplicate
readiness store was added. The change reuses the existing audio predicate,
artifact-stale transition, project/shots locks, report source receipts, and
single export service. Direct dataset/renderers retain draft rendering support;
only the professional client-export transaction requires a passing gate.

The readiness schema bump deliberately makes older persisted readiness/report
bindings require an explicit re-Finalize instead of silently inheriting the new
audio policy. Full migration UX belongs to T21. Resource limits, performance,
committed cross-platform automation, renewed screenshots, and frozen-candidate
acceptance remain T18–T22 work. No DeepSeek Harness was used or required.
