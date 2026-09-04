# T16 explicit export center and version-management UI

Date: 2026-09-01 (Asia/Shanghai). Status: implementation, targeted verification,
headless browser acceptance, and native cold review complete.

## Delivered behavior

- The workspace and deliverables page share one typed `ExportCenter` component.
  Opening, refreshing, reviewing, or finalizing never calls the generation API.
- XLSX, PDF, or both are generated only by an explicit button. A synchronous
  in-flight guard and a service idempotency key prevent duplicate-click requests.
- A lightweight atomic `/exports/state` read does not acquire the long renderer
  lock. The UI polls it serially with `setTimeout`, exposes rendering/publishing,
  and can submit cooperative cancellation before publication without accumulating
  blocked status requests.
- The current package is labelled `current` or `stale`. Historical/stale bytes
  remain inspectable with an explicit warning and are never presented as current.
  Saved immutable versions expose registered downloads and an explicit two-step
  delete action; no automatic history is created or removed.
- Export-center data, mutations, messages, and parent refresh callbacks are bound
  to the active project. Late responses cannot display or mutate another project.
- Initial loading, backend errors, generation failure, recovery, cancellation,
  success, empty state, and blocked readiness remain visible. Format cards have a
  visible keyboard focus ring and primary download/action targets are at least
  44 px high at the accepted viewports.
- The final production build is mirrored byte-for-byte into the packaged Python
  frontend directory.

## Findings repaired during cold review

The first independent native review found two HIGH and two MEDIUM defects:

1. the full export-center status read waited on the renderer lock, so rendering
   could not become visible and interval polling accumulated blocked requests;
2. a project switch could leave an old payload visible and use its version id
   against the new project;
3. large saved-package copies inherited the generic 12-second client timeout;
4. visually hidden format radios had no visible keyboard-focus treatment.

After those repairs, a second review found one remaining HIGH: a completed old
project operation could still call the old parent `onChanged` callback and write
project A's workspace into project B's page. The operation-project and mounted
guards close that path. The final delta review reported no remaining HIGH or
MEDIUM findings and no design that should be removed as over-optimization.

Browser interaction then exposed one additional real UI defect: the deliverables
page unmounted the export center during an automatic post-save refresh, erasing
the success message. Automatic refresh now preserves the component; initial and
manual refreshes retain the full loading state.

## Fresh verification

- Production TypeScript/Vite build passed: 1,703 modules, 320.28 kB JS and
  50.14 kB CSS before gzip.
- Fresh build and packaged frontend: 4 files, byte-for-byte asset gate passed.
- Targeted Python/API/UI boundary: **47 passed, 4 skipped, 7 subtests passed**.
  Skips are optional runtime cases, not converted to PASS.
- Frontend sibling-origin proxy integration passed.
- Offline Ruff on the changed test files passed; Python `compileall` passed.
- A concurrency regression proves `/exports/state` returns `rendering` in under
  one second while the main export lock is held.
- One Headless Chromium run, with no screenshots or real renderer invocation,
  proved:
  - opening caused zero generation requests;
  - a synthetic double click caused one generation request;
  - cancellation, save, and delete each caused exactly one request;
  - progress-poll maximum concurrency was one;
  - stale current and saved downloads remained available with truthful labels;
  - 1440×900, 900×1000, and 390×844 had no horizontal overflow and no visible
    export action shorter than 44 px;
  - keyboard focus rendered as a 3 px solid outline;
  - the expected cancellation response produced one HTTP 422 console entry;
    after isolating that expected failure, there were no unexpected console
    warnings, errors, or page errors.

All temporary Mock API, Vite, and Chromium processes were explicitly stopped;
ports 4175, 4176, and 8787 had no listener afterward. No browser screenshot,
XLSX, PDF, or test project was retained.

## Scope and residuals

This completes T16, not the mature-product goal. The browser run validates the
UI contract against a concurrent local Mock API; T12 separately owns and has
real-runtime renderer/transaction evidence. T17 must still bind review,
finalization, provenance, readiness, and stale invalidation across the complete
UI lifecycle. T19/T22 own committed browser automation, renewed screenshots,
the currently stale candidate receipt, full installation/platform coverage, and
the frozen-candidate review. No DeepSeek Harness was used or required.
