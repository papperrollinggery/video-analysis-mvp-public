# T12 explicit client-export transaction review

Date: 2026-09-01. Status: implementation and native cold review complete.
DeepSeek Harness is not used or required.

## Delivered

- One explicit synchronous transaction service in `export_service.py`; analysis,
  review saves and Finalize do not call it.
- XLSX and PDF consume one locked `client-export-dataset/v1` snapshot. A single
  template preflight binds normalized settings, and optional logo bytes are
  copied once into private staging so every renderer sees the same asset.
- Project-level serialization plus generation-scoped idempotency prevents
  double-click duplication. The ledger keeps at most 64 keys per finalized
  report generation and fails closed when full; a new finalized generation is
  a new verifiable namespace.
- Renderer output is validated before publication. The package receipt binds
  source generation, dataset, normalized settings, formats, renderer receipts,
  file digests and sizes without private absolute paths.
- `current` publication uses private staging, previous-package recovery journal,
  full artifact-registry replacement and registry/file readback. Failures and
  cooperative cancellation leave the previous valid current package unchanged.
- Cancellation has an explicit commit point: `rendering` may accept a cancel
  marker; `publishing` reports that publication can no longer be cancelled.
  Recovery converts abandoned rendering/publishing state to a visible failed
  state and clears stale markers.
- Save and delete are separate explicit operations. Saved packages are copied
  from verified current bytes only, use immutable version IDs, maintain saved
  registry records and have their own save/delete crash journal. No saved
  version is created or removed automatically.
- Descriptor-relative directory removal/rename protects saved/current cleanup
  from symlink-parent swaps.
- Consistent interfaces:
  - CLI: `analyze-video export generate|status|cancel|save|delete|recover`
  - built-in workspace API under `/api/projects/{project}/exports`
  - optional FastAPI routes delegate to the same workspace API and retain
    loopback, Host, Origin, JSON body, CSRF and body-size gates.

## Cold-review findings closed

Independent review reproduced and drove fixes for:

- package publication succeeding before registry commit;
- journal crashes before and after directory publication;
- historical idempotency-key rebinding and bounded-ledger eviction;
- current receipt/output registry digest tampering;
- valid PDF/XLSX packages using different mutable logo bytes;
- saved-parent symlink deletion outside the project;
- saved save/delete interruption and tombstone leaks;
- cancel requests racing the publication commit point;
- abandoned state falsely remaining `rendering`;
- API DELETE silently ignoring a request body;
- CLI export creating ghost projects;
- same key being incorrectly blocked across a new finalized generation.

Final reviewer verdicts for the core transaction, cancel/CLI/workspace API,
FastAPI delegation and generation-scoped idempotency found no remaining
HIGH/MEDIUM in their reviewed scopes.

## Verification

- Required real-runtime T12/T11/T10/T09/T08/API boundary: 74 tests passed.
- T12 service: 25 collected; 24 passed in the base venv with the real-runtime
  case skipped, and all 25 passed with the bundled XLSX/PDF runtime.
- CLI/workspace API/FastAPI focused suites passed in their applicable runtimes.
- Targeted Ruff and Python compileall passed. Existing repository-wide lint and
  the deliberately stale UI candidate receipt remain later quality-gate work.
- Core full suite: 463 passed, 24 skipped, 358 subtests passed; the sole failure
  is the intentionally stale UI candidate receipt (`113 != 190`) reserved for
  T22 candidate freeze.

## Explicit residuals

- Rendering currently holds the project shots lock; a PDF timeout can delay
  review writes. Moving immutable rendering outside the lock requires a new
  source/asset snapshot contract and belongs to T18, not a speculative T12
  cache or queue.
- Cancellation is cooperative between renderer boundaries. Once the service
  enters `publishing`, it reports that the commit point has passed rather than
  making a false cancellation claim.
- The generic service does not install openpyxl, Playwright, Chromium or fonts.
  Optional runtime dependencies remain explicit setup and doctor work for T20.
- T16 owns the visual export center and saved-version controls. T12 provides the
  verified service/API surface only.
