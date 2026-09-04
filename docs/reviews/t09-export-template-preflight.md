# T09 fixed client template and layout preflight review

Date: 2026-09-01. Scope: the fixed `client-storyboard` template package and
deterministic layout preflight. This task does not render XLSX, HTML or PDF,
does not publish a client package and does not change Finalize semantics.

## Result

- One versioned, brand-neutral template package at
  `src/video_analysis_mvp/templates/client/`, with a manifest, design tokens,
  layout limits and an assets policy.
- `load_client_template()` reads only three bounded regular files, verifies the
  pinned SHA-256 for every asset and returns a deterministic template digest.
- `preflight_client_layout()` revalidates `client-export-dataset/v1`, accepts
  only the documented language/density/format/branding settings and produces a
  deterministic plan without writing files.
- Logo input is optional, project-confined, byte-bounded, image-validated and
  digest-bound. Subtitle text uses the same formula-neutralized client text
  contract as the dataset.
- CJK requirements cover language, renderer-visible dataset text and the
  project subtitle. PDF fails without a declared CJK font; XLSX records an
  explicit substitution warning. Arial is not treated as a CJK font.
- Every renderer-visible TextCell is continuation-checked. CR, LF, CRLF and
  empty logical lines consume layout capacity; impossible content fails instead
  of shrinking below 8.5 pt.
- Missing frames use the fixed `missing-frame` variant. Per-image and aggregate
  image budgets are checked before rendering.

Contract: [client export template specification](../client-export-template-spec.md).

## Independent findings and disposition

Native cold review initially found HIGH gaps in CJK font selection, non-shot
text coverage and newline estimation, plus MEDIUM gaps in per-image limits,
template validation, safe asset reads and settings error handling. A second
pass found the subtitle outside continuation planning, a falsy accent bypass
and incomplete continuation metrics. These were fixed with focused validation
and regression tests. Final native verdict: no remaining HIGH/MEDIUM finding.

DeepSeek Harness independently reviewed the copied candidate specifically for
correctness, over-optimization, negative optimization, performance regressions
and damage to the existing workflow. It found two MEDIUM issues: subtitle CJK
was outside the PDF font gate, and template/logo/subtitle failures could escape
the public error contract or expose host paths. Both were fixed. Its final delta
review found no new HIGH/MEDIUM issue.

Adopted recommendations were limited to reproduced boundary defects. Deferred
or rejected work includes template caching, a template DSL/factory, a second
template version, new dependencies, unreachable v2 schema branching, speculative
Unicode expansion, renderer scaffolding and a premature accent-policy rewrite.
Re-reading and re-hashing the three small assets remains the integrity property;
there is no reproduced performance reason to cache it. Renderer/preflight
agreement and surface-specific contrast belong to T10/T11, where real outputs
exist.

DeepSeek session `session-5ea0a367-9d25-4177-8867-c10d1e742edd` completed
initial RPC `81f56ddd-e3b9-4481-9806-a0dcd9b902cf` and final delta RPC
`db186348-1843-4a16-88ae-30a2e60950a7` with `deepseek-v4-pro`, high effort,
`code` + `proposal-only`. Both turns reported `completionReason=completed`.
Harness Ruff was blocked by the delegated cache boundary; host Ruff passed.

## Verification

- Template + dataset boundary: **18 passed, 15 subtests passed**.
- Template + dataset + report boundary: **30 passed, 17 subtests passed**.
- Host Ruff and Python compileall: PASS.
- Full repository suite: **420 passed, 343 subtests passed; 1 failure**. The
  only failure is the intentionally stale UI acceptance receipt
  (`113 != 176` files).
- All tests use temporary project roots; this task produced zero XLSX/PDF.

Overall product verification remains partial. T10/T11 must prove the Excel and
PDF renderers agree with this plan; T12 must prove explicit transactional
generation and stable current/saved lifecycle. The prior UI acceptance receipt
remains intentionally stale until T22 and is not rewritten to manufacture a
pass.

Local audit record: `tmp/.t09-deepseek-audit-20260901`. Original DeepSeek
outputs, final-turn receipt, a compact source snapshot and a final dry-run-clean
delta are retained locally. Snapshot SHA-256:
`d0878305fe6035dcf0c0fb7ba43125eb52cf79802ca3efaba249a9d222eb06e8`;
delta SHA-256:
`474bb323b6ed15d7f426c3f80fc6303755f5c69798a69767b952d5b9907f4214`.
