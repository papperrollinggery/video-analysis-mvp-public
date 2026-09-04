# T18 security, privacy, resource, and performance hardening

Date: 2026-09-01 (Asia/Shanghai). Status: implementation, real-runtime checks,
full regression, and independent security review complete.

## Result

The existing path, text, formula, HTML, request-body, artifact, and temporary
file boundaries were retained. T18 closed the remaining high-confidence resource
and lifecycle gaps without adding a scheduler, database, system-wide process
killer, or new runtime dependency.

### Workspace run admission

- A workspace admits at most one active background analysis run. Admission is
  serialized by a cross-process lock and counts durable queued/running/cancelling
  records whose owner is still active.
- New and retried runs must have at least `source file size + 256 MiB` available
  on the workspace volume before their durable state changes to queued.
- Capacity and disk refusal are observable as HTTP 429 for both start and retry;
  no second project or run record is created on refusal.
- This is intentionally a small local permit, not a persistent job queue.

### Cancellation and subprocess bounds

- The existing bounded `run_command` process-group path now accepts an explicit
  or run-scoped cancellation callback and checks it every 100 ms while waiting
  for output or process exit.
- Current analysis subprocesses inherit the run cancellation scope. Cancelling a
  running ffmpeg/tool stage terminates and reaps its isolated process group before
  the run is recorded as cancelled.
- Playwright PDF rendering now uses the same bounded path: 180-second hard
  timeout, 2 MiB combined stdout/stderr limit, isolated process group, minimal
  environment, and cooperative export cancellation. No browser is downloaded.
- If process-group cleanup cannot be verified, analysis records a retriable
  `ProcessCleanupUnverified` failure and PDF/export records failure; neither path
  falsely claims `cancelled`.

### Lock and storage lifecycle

- The in-process path-lock map now reference-counts holders and waiters. The last
  user removes its exact entry; interrupted acquisition also decrements safely.
  A live waiter prevents premature eviction, so per-path serialization remains
  intact without an unbounded cache.
- Existing export staging and saved/current journals remain private and bounded:
  directories use 0700, files use 0600, failures remove staging, recovery checks
  generation/registry bindings, and explicit saved versions are never silently
  deleted.

## Enforced budgets

| Boundary | Current enforced value |
|---|---:|
| Active background analysis runs per workspace | 1 |
| Workspace free-space reserve at start/retry | source bytes + 256 MiB |
| Local source input | 2 GiB |
| Client export dataset | 64 MiB |
| Client output file | 512 MiB per selected format |
| PDF renderer combined logs | 2 MiB |
| PDF renderer wall time | 180 s |
| Audio adapter request / response | 16 MiB / 16 MiB |
| Audio adapter wall time | configured 1–600 s |
| ASR output / model file | 4 MiB / 4 GiB |

The doctor and runtime-settings API expose the active-run, free-space, process
cancellation, and renderer-log budgets. CPU and memory are bounded indirectly by
single-run admission, input/output limits, and hard process timeouts; this local
product does not claim an OS container or hostile-executable sandbox.

## Security evidence

The reviewed existing controls and regression tests cover:

- workspace/project path traversal, prefix siblings, symlinked roots, safe
  descriptor-relative writes/removes, and malicious version/logo/frame paths;
- HTML escaping and self-contained output with no active external URLs;
- CSV/XLSX formula neutralization, no workbook formulas/macros/external links,
  control-code handling, bounded cells/rows/images, and private-path rejection;
- bounded JSON/body/config/run receipts, strict duplicate-key rejection, CSRF,
  loopback host/origin policy, credential redaction, and generic provider errors;
- timeout, output overflow, descendant process-group cleanup, operator
  cancellation, cleanup-unverified state, staging rollback, and crash recovery.

Fresh evidence:

- Security/resource/export regression: **178 passed, 23 skipped, 93 subtests**.
- Focused final regression: **93 passed, 7 skipped, 23 subtests**.
- Bundled Python real PDF runtime: A4/searchable/font/image/metadata render plus
  verified and unverified cancellation paths, **3/3 passed**.
- Changed-test Ruff and scoped changed-product Ruff passed; Python compile checks
  passed.
- Core full suite: **486 passed, 27 skipped, 358 subtests, 1 failed**. The only
  failure is the intentionally stale T22 UI acceptance receipt (`113 != 195`
  candidate files), not a security or T18 regression.
- Independent security delta review: `CLEAN`; no HIGH, MEDIUM, LOW, admission
  bypass, cancellation misclassification, path-lock race, or over-optimization.

## Residual boundary

Optional local adapters remain trusted-operator executables rather than hostile
code sandboxes. Cross-platform enforcement, large-fixture performance numbers,
failure-retention policy, and clean-install matrices belong to T19/T20. New
candidate screenshots and the one file-set digest remain T22. No DeepSeek
Harness was used or required.
