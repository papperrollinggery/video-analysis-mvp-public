# Audio intelligence and client-export decision log

Status: accepted architecture decisions. The core audio-review and client-export boundaries are implemented; later ecosystem/adoption decisions remain conditional.

## D001 — One reviewed evidence timeline, not free-form shot sound text

- Decision: model audio as versioned events with half-open time ranges, layers, source, confidence, machine proposal, human override, and effective value.
- Why: time-based tools consistently separate ranges from adapters and editing views; the current per-shot sound fields cannot represent overlaps or provenance.
- Applies to: T04, T05, T06, T15, T17.
- Revisit when: frame-accurate NLE interchange becomes a validated user need.

## D002 — Optional models enrich; they never define base-product availability

- Decision: deterministic local analysis is always available. Whisper, diarization, and source separation are optional adapters with explicit install, enablement, receipts, limits, and fallback.
- Why: Whisper and pyannote require sizeable model/runtime assets, pyannote community retrieval requires external model conditions/token, and source-separation maintenance/licensing varies.
- Applies to: T05, T07, T18, T20.
- Revisit when: a permissively licensed, bounded, cross-platform model can be bundled without violating download and privacy controls.

## D003 — Do not use Demucs or Essentia in the MIT core

- Decision: Demucs is rejected as a product-critical dependency because the official repository is unmaintained. Essentia is research-only because its AGPL/commercial/model licensing is incompatible with the intended permissive core without separate clearance.
- Why: product reliability and redistribution rights are release requirements, not cleanup work.
- Applies to: T07, T18, T20, T21.
- Revisit when: legal and maintenance evidence changes and an explicit adapter remains isolated from the core.

## D004 — Keep a native v1 schema; consider OTIO only as an adapter

- Decision: use OpenTimelineIO-inspired time semantics and adapter boundaries, but do not add OTIO to core v1.
- Why: this product needs confidence, evidence, review, text, audio layers, and client-export readiness that OTIO does not directly model.
- Applies to: T04, T06, T08.
- Revisit when: import/export with Resolve, Premiere, or another NLE is selected as a validated scenario.

## D005 — Generate all customer formats from one dataset

- Decision: XLSX, HTML, and PDF receive the same validated `client-export` dataset and must never compute their own shot/audio business logic.
- Why: professional delivery requires cross-format consistency and one provenance digest.
- Applies to: T08, T10, T11, T12, T17.
- Revisit when: never; new renderers must remain adapters.

## D006 — Explicit export is a separate transaction

- Decision: analysis, draft save, and Finalize never generate XLSX/PDF. A user action starts an export transaction with idempotency, progress, cancellation, staging, atomic publish, and stale state.
- Why: prevents file accumulation, long hidden work, partial output, and false readiness.
- Applies to: T03, T12, T13, T16, T17.
- Revisit when: only for an explicitly configured automation, never as an implicit default.

## D007 — openpyxl for XLSX generation; Playwright for preferred PDF rendering

- Decision: use openpyxl as a generation-only adapter and Playwright/Chromium as an explicitly installed PDF adapter. Keep legacy overview PDF behavior separate during migration.
- Why: openpyxl supports native OOXML structures and images; Playwright provides controllable print CSS, A4/landscape, background, outline, and tagged PDF options.
- Guardrails: no untrusted workbook ingestion, no macros/external links, no runtime browser download, versioned templates, structural validation, and visual acceptance fixtures.
- Applies to: T09, T10, T11, T18, T19, T20.
- Revisit when: another renderer proves equal layout control with a smaller, cross-platform dependency.

## D008 — Correction-first and accessible review UI

- Decision: combine waveform/timeline interaction with a DOM outliner, unresolved queue, keyboard controls, loop/zoom, split/merge, undo, and explicit save/finalize states.
- Why: mature annotation tools reduce work by correcting hypotheses, while canvas-only designs are inaccessible and hard to verify.
- Applies to: T14, T15, T17, T19.
- Revisit when: user testing identifies a simpler interaction with equal auditability.

## D009 — Candidate evidence is refreshed at freeze, not after every edit

- Decision: current stale screenshot/asset receipts remain visibly failing during implementation. T22 refreshes them once against the final candidate digest after all applicable tests pass.
- Why: continuously rewriting acceptance receipts turns evidence into self-approval and creates noisy false confidence.
- Applies to: T00, T19, T21, T22.
- Revisit when: CI can generate and independently review deterministic visual receipts per change.

## D010 — Star growth is an outcome metric, not a completion claim

- Decision: engineering completion is proven by user-task success, clean install, evidence accuracy, export quality, tests, accessibility, and cold review. GitHub stars are measured only after an authorized public release.
- Why: stars depend on distribution and community adoption outside local implementation control.
- Applies to: T02, T20, T21, T22.
- Revisit when: public telemetry and repository analytics exist with user authorization.
