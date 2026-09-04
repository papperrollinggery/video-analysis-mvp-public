# Quality metrics and verification matrix

Status: T19 verification contract. Thresholds below are release gates unless
explicitly labelled observational or `not_run`. A green unit suite cannot
substitute for an applicable render, browser, install, or benchmark gate.

## Verification layers

| Layer | Command / job | Proves | Does not prove |
|---|---|---|---|
| Deterministic unit | `python -m unittest discover -v` | schemas, time ranges, review CAS, readiness, path/text safety, transaction recovery | browser layout, optional renderer availability, model accuracy |
| Service integration | `scripts/review-workflow-smoke-test.sh` | real loopback API: blocked → shot/audio review → Finalize → mutation stale → re-Finalize | cloud/provider behavior, client visual quality |
| Frontend contract | `npm --prefix frontend run build` and `test:integration` | TypeScript production build and same-origin proxy boundary | user interaction |
| Browser E2E | `npm --prefix frontend run test:e2e` | explicit export, double-click, cancel, stale/history, save/delete, keyboard focus, 1440/900/390 layouts | real XLSX/PDF renderer bytes |
| Real render | `scripts/test-client-exports.sh` with required runtime env | openpyxl, Playwright Chromium, CJK font, searchable A4 PDF, LibreOffice XLSX conversion | every office version or pixel-identical rendering |
| Install | `scripts/install-smoke-test.sh` | clean Git candidate, fresh temporary venv, wheel/source asset parity, and declared base dependencies | optional render stacks and unsupported platforms |
| Product benchmark | `analyze-video benchmark --output DIR` | six generated video cases, five deterministic PCM cases, artifact completeness, readiness, performance | ASR WER/CER or semantic VO/music/SFX identity accuracy |
| Security/resource | CI `security` plus targeted tests | dependency/source audit, request/path/formula/privacy limits, process/disk/cancellation states | hostile executable sandboxing |
| Candidate freeze | T22 only | current screenshots, candidate digest, full gate receipt, independent cold review | publication or adoption |

## Video boundary metrics

| Metric | Gate | Rationale |
|---|---:|---|
| Match tolerance | ≤ 0.600 s, inclusive with 1 ns numeric guard | Accommodates the synthetic detector sampling scale without accepting a different editorial beat. Exact-edge behavior has a regression test. |
| Hard-cut/vertical precision | ≥ 0.80 | One false positive is material in these small reviewed fixtures. |
| Hard-cut/vertical recall | ≥ 0.50 | The v0.2 detector must find at least one expected cut; this is a floor, not a mature editorial target. |
| No-cut false positives | 0 | Animation, VFR, and dense-audio surrogate clips must not invent editorial cuts. |
| Fade/dissolve | observational | It cannot add to the pass count until a reviewed transition-specific threshold exists. |
| Per-case elapsed | ≤ 60 s | Wide enough for clean CI while still detecting hangs or major regression on 3–4 s fixtures. |
| Six-case total elapsed | ≤ 240 s | Four times the measured case ceiling; failure is a performance regression, not an accuracy waiver. |
| Python-process peak RSS | ≤ 1.5 GiB | Guards accidental in-process materialization. Child RSS is explicitly outside this metric and controlled by T18 process limits. |

Thresholds are deliberately conservative v0.2 floors. They may change only with
a reviewed benchmark receipt and documented before/after errors; do not tune the
detector merely to make one failing receipt green.

## Deterministic audio metrics

The core baseline measures PCM; it does not identify speech, music, SFX,
speakers, emotion, or musical meter.

| Case | Gate |
|---|---|
| Full silence | RMS exactly 0 and one measured `[0, 1.13)` silence range |
| Regular pulses | 9/9 onsets, maximum timing error ≤ 21 ms, tempo error ≤ 1 BPM around 120 BPM |
| Stereo phase | two channels retained and RMS within 0.0001 of 0.5 |
| Irregular transients | no confident BPM claim |
| Corrupt WAV | deterministic rejection, no partial timeline |
| Five-case elapsed | ≤ 5 s |

`scripts/benchmark-audio.sh` runs these cases in a process-scoped temporary
directory and retains no WAV. ASR WER/CER and semantic event precision/recall
remain `not_run` until a redistributable speech/music/SFX fixture and explicit
model/runtime are available. `not_run` never counts as PASS.

## Export and UI metrics

- XLSX, HTML, and PDF bind the same canonical dataset digest and template digest.
- Required shared shot/audio text is structurally checked across formats.
- XLSX contains no formulas, macros, external links, unsafe control codes, or
  private absolute paths; row/cell/image limits fail before publication.
- PDF is searchable A4 landscape with embedded CJK-capable fonts, images,
  metadata, and bounded renderer logs.
- LibreOffice converts the generated workbook into 1–20 searchable landscape
  pages containing the project title, shot id, and VO marker.
- Browser E2E permits no horizontal overflow or visible export control under
  44 px at 1440×900, 900×1000, or 390×844; keyboard focus remains visible.
- The prepared-machine client-package target remains <30 s. The current
  candidate receipt records the actual XLSX+PDF gate and environment; it does
  not generalize that measurement to other machines.

## Fixtures, privacy, and retention

- [`tests/fixtures/manifest.json`](../tests/fixtures/manifest.json) is the
  authoritative fixture source/license record. Current public media cases are
  generated from FFmpeg filters or PCM samples and retain no media bytes.
- Private reference media is outside the repository and may be used only for an
  optional local human check. No media bytes, absolute path, or content hash may
  enter the candidate, issue, CI artifact, or benchmark receipt.
- Passing tests retain no generated XLSX, PDF, MP4, WAV, checkpoint, model, or
  screenshot. Browser E2E creates one screenshot only on failure.
- `scripts/audit-test-artifacts.sh` rejects generated/private candidate files and
  limits `test-results/` to 12 files, 8 MiB each, 32 MiB total.
- CI uploads `test-results/` only on failure and retains it for three days.

## Current local evidence

On the 2026-09-04 maintainer machine, the frozen local-candidate benchmark
completed:

- video functional 6/6;
- video accuracy-gated 5/5, one fade observational;
- deterministic audio 5/5;
- all configured video/audio time and memory ceilings passed;
- ASR and semantic identity accuracy `not_run`;
- all receipt fixture paths project-relative; the temporary directory was removed.

Exact per-case values, elapsed times and environment belong to the current
machine-readable benchmark and mature-candidate receipts. This is a scoped
local measurement, not a cross-platform claim; the GitHub checks page is the
authority for remote execution of any published revision.
