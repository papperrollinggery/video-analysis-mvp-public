# Video Evidence Workbench current-state audit

Status: historical T00 execution baseline, not current product status or a
release claim. Use [release readiness](release-readiness.md) for the latest
candidate evidence; the snapshot below is preserved to show the starting gap.

Audit date: 2026-07-22 (Asia/Shanghai)

## Executive finding

The repository is a usable local alpha with a functioning browser build, smoke workflow, synchronous API workflow, and a broad Python regression suite. It is not yet a reproducible release candidate: the repository has no Git `HEAD` or remote, all source files are untracked, the current UI acceptance receipt is intentionally stale after the latest plan and documentation changes, and the system Python does not provide the project test environment.

The authoritative source baseline for this execution is `docs/evidence/candidate-baseline.json`. It excludes ignored runtime data, `.agency/` operational state, and the manifest itself. Until a user-authorized first commit exists, code-writing Tasks must either run serially in this workspace or explicitly refresh the file-set digest after each integration point.

## Git and candidate state

- Branch name: `codex/professional-shot-breakdown-workbench`.
- `git rev-parse --verify HEAD`: failed with `fatal: Needed a single revision`.
- Git remotes: none configured.
- `git status --short`: every project source surface is untracked.
- Commit-, diff-, worktree-, PR-, release-, and remote-CI-level claims are therefore unavailable.
- `.agency/` contains the live execution ledger and is intentionally excluded from the product candidate digest so progress events do not invalidate product artifacts.
- Ignored runtime data is excluded by `git ls-files --cached --others --exclude-standard`.

## Toolchain readback

- Python: `3.14.5` for the system interpreter; Python 3.11 and 3.12 executables are also present locally.
- Node.js: `v26.0.0`.
- npm: `11.12.1`.
- FFmpeg / ffprobe: `8.1.1`.
- `wkhtmltopdf`: not installed.
- Existing `.venv`: present but does not contain pytest.
- Homebrew pytest: `9.0.3`; tests require `PYTHONPATH=src` in the current uninstalled workspace.

## Fresh verification results

| Surface | Command | Current result |
|---|---|---|
| Frontend production build | `cd frontend && npm run build` | PASS; 1,699 modules transformed |
| Frontend origin integration | `cd frontend && npm run test:integration` | PASS |
| Packaged frontend asset parity | `sh scripts/verify-frontend-assets.sh frontend/dist src/video_analysis_mvp/frontend_dist` | PASS; four files match |
| Synthetic delivery smoke | `sh scripts/smoke-test.sh` | PASS |
| Synchronous and persistent API smoke | `sh scripts/api-smoke-test.sh` | PASS |
| Python regression suite | `PYTHONPATH=src /opt/homebrew/bin/pytest -q` | 247 passed, 8 skipped, 1 failed |

The single Python failure is `FrontendContractTest.test_ui_acceptance_receipt_binds_current_assets_and_screenshots`. The receipt records 113 candidate files while the current candidate has additional planning and documentation files. This is a real stale-evidence failure, not a functional regression, and must remain visible until T22 refreshes the screenshot and candidate receipt against the finished product.

The following commands are environment failures and are not counted as product verification:

- `python3 -m pytest -q`: system Python has no pytest.
- `.venv/bin/python -m pytest -q`: the existing virtual environment has no pytest.
- `/opt/homebrew/bin/pytest -q` without `PYTHONPATH=src`: the package is not installed in that interpreter.

## Privacy and generated-artifact boundary

Ignored local runtime directories currently contain video/audio/report artifacts:

- `analysis-projects/`
- `demo-workspace/`
- `output/`
- `test-results/`
- `tmp/`
- `frontend/dist/`

They are not part of the product candidate. Their presence proves local operation only; it does not authorize publication or use as open-source fixtures. Private reference media remains outside the repository and may only be used for local, non-committed human acceptance.

A filename-only secret-pattern scan found one match in `tests/test_config.py`. Inspection confirms it is the deliberately synthetic string `sk-sensitive-first-and-last` used to test masking behavior; no live credential was identified by this bounded scan. This is not a comprehensive secret audit.

## Current architecture and delivery gaps

1. Audio analysis provides transcript segments, beats, and a coarse whole-video music profile, but not a reviewed event timeline for VO roles, sound effects, music sections, silence, energy, and mixed/unknown evidence.
2. The core product has no professional XLSX exporter. The existing PDF path depends on `wkhtmltopdf`, which is unavailable in the current environment.
3. Normal analysis, Finalize, and future client export must remain separate lifecycle operations. Export must be explicit, generation-bound, atomic, cancellable, and non-accumulating by default.
4. `synthesis.py`, readiness, delivery, and workspace API are high-coupling surfaces. Artifact registration and versioned schemas must be introduced before adding UI buttons.
5. A first-party development/test extra and a clean test command are missing; onboarding cannot rely on whichever pytest happens to be installed globally.
6. Current responsive screenshots and UI acceptance evidence describe the prior local alpha and must not be reused as proof for the redesigned audio/export UI.

## Execution controls

- T01 and other read-only research may run independently.
- Parallel code writers that require isolated worktrees cannot start until a real Git baseline is explicitly authorized.
- Without that authorization, implementation proceeds serially and refreshes the exact candidate digest at integration gates.
- No commit, push, remote creation, publication, private-media copy, or automatic client export is authorized by this baseline.
- Final release readiness requires current tests, install smoke, responsive browser evidence, migration checks, security/performance checks, and an independent cold review bound to one final candidate digest.

## Rollback

T00 adds only this audit and its machine-readable manifest. No runtime data was deleted or migrated. Rollback consists of removing those two files; `.agency/` remains the separate execution ledger.
