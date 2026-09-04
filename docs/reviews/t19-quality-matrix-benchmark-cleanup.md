# T19 test matrix, licensed fixtures, benchmarks, and cleanup

Date: 2026-09-01 (Asia/Shanghai). Status: local implementation and applicable
verification complete; remote CI execution remains unverified until pushed.

## Layered test matrix

- Ordinary Python unit/integration jobs remain dependency-light and do not
  download Chromium, run LibreOffice, or require client render extras.
- A separate `export-render` CI job explicitly installs `.[export,pdf]`,
  Playwright 1.60 Chromium, Noto CJK fonts, and LibreOffice Calc. Required-runtime
  environment flags turn missing runtimes into failures instead of skips.
- The committed Playwright E2E hosts the production build and bounded mock API
  in one process. It covers no-action, double-click, cooperative cancellation,
  successful generation → current downloads, stale/history, save/delete,
  keyboard focus, and 1440×900 / 900×1000 / 390×844 layouts. It never claims
  that mock responses prove real renderer bytes.
- The real render layer checks shared dataset/template bindings, XLSX safety,
  Playwright PDF structure, CJK fonts, images, metadata, cancellation, a real
  both-format transaction, CLI/API lifecycle, and LibreOffice XLSX→PDF
  openability/searchable landscape pages.
- HTTP review/finalization smoke, clean candidate install smoke, security jobs,
  artifact cleanup, benchmark, and T22 candidate freeze remain separate gates.

CI build/E2E and render commands use `pipefail`; a failed producer cannot be
masked by `tee`. Failure diagnostics use a pinned official upload action, upload
only on failure, and expire after three days.

## Benchmarks and honest scope

The product benchmark now has two generated, redistributable parts:

1. six 3–4 second video cases for hard cuts, fade observation, animation,
   portrait, VFR, and dense-audio false-positive resistance;
2. five deterministic PCM cases for silence range, pulse timing/BPM, stereo
   energy, irregular-transient non-claim, and corrupt-WAV rejection.

Thresholds, inclusive tolerance handling, per-case/total time, Python-process
peak RSS, and audio error tolerances are implemented in the receipt and explained
in [quality-metrics.md](../quality-metrics.md). ASR WER/CER and semantic
VO/music/SFX accuracy remain `not_run` because no redistributable speech/music
fixture and explicit model runtime are currently supplied; they never increase
the pass count.

One fresh process-scoped local run passed:

- video functional 6/6;
- video accuracy-gated 5/5, fade observational;
- deterministic PCM 5/5 in 0.073 s;
- total 5.740 s, Python-process peak RSS 50,495,488 bytes;
- all receipt fixture paths relative;
- all generated benchmark media and workspace bytes removed with the temporary
  directory after readback.

## Fixture and retention policy

[`tests/fixtures/manifest.json`](../../tests/fixtures/manifest.json) records the
generator, version, reproducible command, source-assets statement, rights holder,
CC0 output declaration, coverage, and retained-media state for every public
fixture family. Current cases use only project-authored FFmpeg filters or PCM
samples; they incorporate no external media.

Private reference media remains outside the repository. Its bytes, absolute
path, and content hash are not candidate assets. Local `.agency/` task prompts
are also ignored because they may contain private coordination context.

`scripts/audit-test-artifacts.sh` checks the Git candidate for generated media,
documents, checkpoints, current-home paths, and optional externally supplied
private markers. It bounds `test-results/` to 12 files, 8 MiB per file, and
32 MiB total. Passing browser tests create no screenshot; failure creates at
most one. User-owned ignored analysis/demo workspaces and historical local UI
scratch directories are not deleted or reclassified as release assets.

## Fresh verification

- Benchmark contract and cleanup tests: **12 passed, 1 skipped** in the base
  environment; the skip is the explicitly optional LibreOffice runtime.
- Real export matrix through `scripts/test-client-exports.sh`: **69/69 passed**,
  followed by production build, browser E2E, and artifact audit PASS.
- LibreOffice XLSX visual/openability acceptance passed in the bundled runtime.
- Playwright E2E PASS: generate 2 (one cancelled, one successful), cancel 1,
  save 1, delete 1, status max concurrency 1, no unexpected console errors.
- Clean Git-candidate wheel/install/served-asset smoke passed after providing the
  declared Pillow, pydantic, yt-dlp, and setuptools prerequisites. Two earlier
  local attempts correctly failed for missing pip/Pillow and are not counted.
- Frontend dependency audit: zero known vulnerabilities after compatible
  lockfile updates (`react-router-dom` 7.18.3, `postcss` 8.5.26).
- CI YAML parsed locally; the new remote CI job has not run because nothing has
  been pushed.
- Core full suite: **490 passed, 28 skipped, 358 subtests, 1 failed**. The only
  failure is the intentionally stale T22 UI receipt (`113 != 195` candidate
  files excluding the receipt itself).
- Independent T19 delta review ended `CLEAN`; no remaining HIGH, MEDIUM, LOW,
  fake green, or removable over-optimization was found.

## Residual boundary

T19 establishes the matrix; T21 still owns executable migration/rollback
fixtures, and T22 owns current screenshots, candidate digest, full gate receipt,
and final cold review. Remote CI, Windows, real optional ASR, and semantic sound
classification are not claimed. No DeepSeek Harness was used or required.
