# T20 installation, migration, Codex and BridgeDeck review

Status: completed local candidate. This record covers T20 only; it is not a
public-release or cross-platform provider claim.

## Result

- The installed `analyze-video migrate PROJECT` command performs a read-only
  inspection. `--apply` is the only mutation path.
- Supported legacy readiness/report schemas are prepared for the existing
  review and Finalize flow. Migration does not regenerate media, assert human
  review, run a model, or create XLSX/PDF.
- The transaction reuses the existing project, shots and client-export locks,
  takes a private `0700` metadata backup, restores handled failures, recovers an
  interrupted transaction on the next explicit apply, verifies that report and
  current client records are stale, and preserves saved client versions.
- Missing, malformed, mixed legacy/future and future schemas fail before
  writes. Dry-run, apply, retry, rollback, interruption and concurrent Save
  version behavior have regression coverage.
- The install smoke reconstructs the Git candidate, creates a fresh temporary
  virtual environment, installs the candidate wheel plus declared base
  dependencies, exercises the installed migration command on a real synthetic
  pipeline project, verifies frontend byte parity, serves the installed UI and
  removes the temporary environment on exit.
- README/FAQ now separate the legacy overview PDF from requested professional
  XLSX/PDF, document optional render dependencies, macOS/Linux limits,
  migration/recovery, common admission/readiness failures, data dictionaries
  and the fixed export template.
- The current Codex task remains an analysis executor inside the existing
  `prepare → apply → human review → Finalize → requested export` workflow.
  BridgeDeck remains an optional explicit loopback/account/model adapter; it is
  not a credential fallback or prerequisite.

## Over-correction review

Retained because they close demonstrated failure modes:

- one installed migration entry point rather than a repository-only duplicate;
- existing lock reuse for saved/current registry safety;
- strict schema classification and a bounded metadata rollback receipt;
- a fresh temporary install environment rather than mutating the maintainer
  environment.

Rejected as unnecessary or harmful:

- automatic migration, Finalize, export or provider fallback;
- a second artifact registry or general-purpose migration framework;
- bundling Chromium, fonts, LibreOffice or optional provider runtimes into the
  base install;
- account discovery, environment credential copying, or unverified model
  identity claims;
- refreshing final UI screenshots before the frozen-candidate task.

## Fresh verification

- `uvx --offline ruff check src/video_analysis_mvp/migration.py tests/test_migration.py`
  → clean.
- Migration suite → 11 passed plus 6 malformed/future subcases.
- T20 focused Codex, BridgeDeck, doctor and migration suite → 35 passed plus 35
  subtests.
- `sh scripts/install-smoke-test.sh` → fresh venv/wheel install, installed
  migration dry-run/apply, frontend mirror and served-byte parity all passed.
- Client export gate → 69 tests passed with 28 optional-runtime skips; frontend
  build, browser export-center E2E and artifact cleanup passed.
- Full Python suite → 501 passed, 28 skipped, 363 subtests, one expected T22
  failure: the old UI acceptance receipt records 113 files while the current
  candidate contains 199 files excluding that receipt.
- Local Markdown links resolve.
- One disposable four-second synthetic project was prepared, its exact 720×406
  requested frame was inspected by the current Codex task, and one structured
  result was applied. The receipt remained `model_identity_verified=false`,
  the shot remained blocked for human review, manifest state became
  `review_pending`, no XLSX/client PDF was created, and the temporary project
  was removed.
- Independent read-only code review ended `CLEAN` after repairs for mixed/future
  schema classification, client-export serialization and malformed nested
  report-generation receipts.

## Remaining boundaries

- Live BridgeDeck authentication/upstream inference is unverified; only the
  synthetic loopback contract is proven.
- External provider accuracy, optional audio semantic accuracy and model
  identity are unverified.
- Remote CI has not run against this uncommitted local candidate.
- Final candidate screenshots, candidate digest and the last all-green release
  receipt remain T22 work.
- No DeepSeek Harness call or dependency was used for T20.
