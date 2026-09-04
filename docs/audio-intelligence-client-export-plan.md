# Audio intelligence and client export productization plan

Status: accepted implementation plan and decision record. The deterministic
audio timeline, review service/UI, native Codex prepare/apply path, BridgeDeck
compatibility, fixed professional XLSX/PDF templates, and explicit export
transaction are implemented. Optional semantic providers, diarization/source
separation, public release, and cross-platform evidence remain conditional or
unverified. The normative current contracts are the linked schema and product
documents; early examples below are historical planning context.

## Execution refinement after independent review (2026-09-01)

- First expose the audio evidence/review loop: T06 → T13 → minimum necessary T14 → T15. Audio APIs/UI no longer wait for all export renderers; export CLI/API moves into T12.
- Retain professional **Excel and PDF** in the client-delivery milestone. Both are explicit user requirements. Use one fixed neutral template and bounded brand fields, not a template editor.
- T07 prioritizes evidence-backed music/SFX/VO semantics and the same native Codex protocol when host capability is actually available. Energy plus ASR does not satisfy all semantic requirements. Diarization/source separation remain optional later enhancements, not mandatory heavy installs.
- Redesign incrementally in the existing app shell. Extract components only where real audio/export interactions need shared state; no framework migration or whole-app rewrite.
- Preserve separate Task IDs, but reuse verification receipts for an unchanged candidate. Final installation/security/docs/UI evidence is collected against one frozen candidate, not repeatedly regenerated after every local edit.
- Independent review recommendations are checked against current evidence. Do not remove requested deliverables, compatibility entry points, or integrity safeguards merely to reduce code size. DeepSeek Harness is no longer a required or permitted project gate.

## Objective

Turn the one-off reference delivery workflow into a supported product path:

1. analyze speech, music, sound effects, silence, and rhythm from the final mix;
2. align those observations to shots and narrative sections;
3. produce a client-readable storyboard package without a separate Codex request;
4. export the same reviewed dataset to Excel, HTML, and PDF;
5. preserve the workbench's evidence, provenance, and fail-closed delivery rules.

The target is repeatable structure and review quality. It is not a promise that a mixed soundtrack can be reconstructed into factual source stems, that a speaker is automatically known to be voice-over, or that generated creative interpretation is true without review.

## Current baseline

The current audio stage already produces:

- optional explicitly configured local Whisper transcript segments and SRT; otherwise explicit unknown/skipped/failed capability status;
- measured PCM RMS and threshold silence, plus conservative energy-onset/regular-pulse candidates;
- a compatibility whole-video energy summary with zero music-identity confidence and no generated style/mood tags;
- per-shot transcript overlap, beat density, and a generic audio review note;
- an audio-generation receipt bound into report generation;
- a separate `audio-timeline/v1` dataset and receipt bound to the exact WAV and media package. New run reuse validates both generations.

The reporting stage produces the evidence package independently from client
exports. The explicit client-export service now renders professional XLSX and,
when its verified optional runtime is configured, PDF from one validated
dataset and fixed template. It still does not provide speaker diarization,
source separation, or verified music/SFX/VO identities. See [local baseline
limits](audio-baseline.md).

The private reference client package informed the desired output shape. Its one-off
builder remains historical reference material; the supported product path is
the shared export dataset, template package, and explicit export service in
this repository.

## Product contract

### Normal analysis does not export files

Ingest, visual analysis, audio analysis, report generation, review, and Finalize update structured project data and receipts only. They do not create Excel or PDF files.

Export is an explicit user action through one of these surfaces:

- click **Generate client package** in Export mode;
- run `analyze-video export PROJECT_ID ...`;
- explicitly ask Codex to invoke the same project export operation.

The action is blocked until the project has a current finalized report generation. Draft evidence remains reviewable in the workbench but is not rendered as a client package.

### Stable output slots

By default, generation atomically replaces one stable `current` slot instead of creating timestamped copies:

```text
reports/client/current/client_breakdown.xlsx
reports/client/current/client_breakdown.pdf        # when a verified PDF renderer is available
reports/client/current/export_receipt.json
data/client_export_dataset.json
```

No automatic timestamped copy is created. **Save as version** is a separate explicit action that writes an immutable export ID under `reports/client/saved/<version-id>/`. The UI shows saved-version count and total size and provides explicit deletion; the product never silently deletes a user-saved version.

A draft remains local and visibly watermarked; it is not a professional export. Missing optional engines produce explicit gaps, not invented text.

Edits to shots, transcript, timeline, media, review state, or template settings mark the current package stale and block it from professional download. They do not regenerate it automatically. The next explicit Generate action replaces the stable slot.

All staging files live in a private temporary directory and are removed after success or failure. Startup recovery removes abandoned export staging directories only after verifying that they are not current or saved outputs.

### Finalization and export

The existing human-review and Finalize workflow remains authoritative, but Finalize does not generate client files. It changes whether the next explicit Generate action is eligible to publish a professional package.

These files are exposed as professional deliverables only when:

- the current visual and audio generations are valid;
- the current shot data passes readiness;
- the client export binds the current committed report generation;
- every declared export file matches its receipt;
- no subsequent shot, transcript, timeline, media, or template mutation occurred.

## Evidence model

Introduce `audio-timeline/v1` without mutating the existing `transcript.json`, `beats.json`, or `music_profile.json` contracts.

### Timeline segment

Each segment carries:

| Field | Contract |
| --- | --- |
| `segment_id` | stable within one audio-intelligence generation |
| `start_time`, `end_time` | finite, ordered, bounded by current media |
| `kind` | `voice`, `music`, `sfx`, `silence`, or `mixed` |
| `label` | bounded descriptive label; empty when not inferable |
| `text` | ASR text for voice segments only |
| `speaker_id` | anonymous diarization label, never a real identity claim |
| `voice_role` | `voice_over`, `dialogue`, `singing`, or `unknown` |
| `energy`, `onset_density`, `estimated_bpm` | measured or estimated numeric features |
| `confidence` | finite value in `[0, 1]` |
| `source` | detector or adapter identifier and version |
| `verification` | `measured`, `machine_estimated`, `model_interpreted`, or `human_reviewed` |
| `review_notes` | explicit operator correction or unresolved gap |

`voice_role=voice_over` must remain an estimate unless a human explicitly confirms it. A final mix can contain overlapping voice, music, and SFX, so `mixed` is a first-class state.

### Derived records

The exporter derives three views from the canonical timeline rather than maintaining conflicting copies:

- voice/VO table with timecode, text, anonymous speaker, role, and confidence;
- music cue table with entrance, exit, energy arc, tempo, mood, and narrative function;
- SFX table with event time, class, sync relationship, confidence, and evidence note.

### Interpretation layers

The UI and exports must visually distinguish:

1. measured media facts;
2. deterministic or ML estimates;
3. optional model-generated narrative interpretation;
4. human-reviewed client language.

No fallback may silently turn a blank human decision into machine text.

## Architecture

### Modules

```text
src/video_analysis_mvp/
├── audio.py                         # existing stage orchestration and legacy outputs
├── audio_timeline.py                # canonical timeline construction and validation
├── audio_features.py                # loudness, energy, onsets, silence, tempo estimates
├── audio_adapters/
│   ├── asr.py                       # Whisper adapter
│   ├── diarization.py               # optional speaker diarization adapter
│   └── separation.py                # optional external source-separation adapter
├── client_export.py                 # dataset builder and generation receipt
├── client_excel.py                  # workbook renderer
├── client_html.py                   # shared client-facing HTML renderer
└── pdf_renderer.py                  # renderer discovery, execution, and verification
```

`audio.py` remains backward compatible during the first release. New timeline files receive their own generation receipt, so current audio-generation validation is not weakened by optional artifacts.

The client export layout follows the versioned [client export template specification](client-export-template-spec.md). Template changes never mutate an existing receipt silently.

### Shared export dataset

All client formats consume `client-export-dataset/v1`, containing:

- project summary and limitations;
- narrative sections;
- every shot with timecode, evidence frame, description, VO/dialogue, on-screen text, music/SFX, and transition rhythm;
- full voice/VO timeline;
- music and SFX timeline;
- provenance and unresolved-review notes.

Excel, HTML, and PDF must not implement independent business logic. Format renderers receive only the validated dataset plus a versioned template.

### Artifact registry

Add the new files through one canonical artifact registry used by synthesis, the API, file serving, readiness checks, and the React deliverables page. Do not append independent lists in multiple modules.

The current `_deliverable_specs` path is critical and has several consumers. The registry migration must ship with compatibility tests before removing existing lists.

## Analysis tiers

### Tier 1: default local baseline

Required tools: existing FFmpeg/FFprobe; local Whisper remains optional.

Automatically produce:

- voice activity from ASR segments or conservative energy/silence evidence;
- transcript timing when Whisper is installed;
- RMS/loudness curve, onset density, silence ranges, and estimated tempo;
- music-energy sections and likely transition points;
- shot alignment and deterministic narrative templates;
- explicit `unknown` values for unsupported claims.

This tier must remain usable without network access, model downloads, or credentials.

### Tier 2: enhanced local adapters

Optional and separately installed:

- speaker diarization for anonymous `SPEAKER_00`-style turns;
- source separation for vocals/music/other evidence;
- richer audio-event classification.

Enhanced adapters must be discoverable through `doctor`, record engine/model/version/device, cap runtime and output, and fail back to Tier 1 without corrupting a generation.

`pyannote.audio` is a candidate diarization adapter, not a core dependency. Its model access, resource cost, and any remote-service mode require a separate privacy and credential boundary.

The original Demucs repository states that it is no longer actively maintained. Source separation must therefore use an adapter protocol and a pinned, independently reviewed engine rather than making Demucs a mandatory runtime dependency.

### Tier 3: optional narrative enrichment

An explicit configured model may rewrite evidence into concise client language, but it may not change measured timing or erase uncertainty. Inputs are the bounded export dataset and selected frames, never arbitrary project files. Outputs are schema validated, provenance recorded, and presented as `model_interpreted` until reviewed.

The deterministic template remains the offline fallback and must always generate a complete, honest package.

## Client workbook

Use `openpyxl` in an optional `export` dependency group. The official library supports embedding images in worksheets, matching the validated one-off workbook pattern.

Required sheets:

1. `01_项目概览` / project overview;
2. `02_逐镜分镜表` / complete shot storyboard with embedded frames;
3. `03_VO与画面文字` / voice, dialogue, and on-screen text timeline;
4. `04_音乐与节奏` / music, SFX, energy, and edit rhythm;
5. `05_证据与说明` / provenance, confidence legend, missing engines, and review state.

Workbook requirements:

- formula-injection neutralization for every untrusted text cell;
- project-relative evidence references only;
- no secrets or private source URL components;
- embedded thumbnails bounded by pixel and byte limits;
- freeze panes, filters, print areas, page headers, and repeatable sheet names;
- Chinese/English template selection;
- deterministic content ordering;
- workbook receipt with template version and source-generation bindings.

## PDF strategy

Generate PDF from the client HTML dataset, not by converting the workbook. This keeps HTML and PDF layout logic aligned and avoids requiring LibreOffice for normal users.

Preferred renderer: headless Chromium through an optional Playwright adapter. Playwright's documented `page.pdf()` supports print CSS, A4 sizing, landscape mode, backgrounds, headers/footers, outlines, and tagged PDF output.

Migration rules:

- keep the current `wkhtmltopdf` renderer as a compatibility fallback for one deprecation window;
- prefer Playwright when its pinned browser is installed and verified;
- never download a browser during an analysis run;
- report `pdf_renderer_missing` when no renderer is available;
- validate page count, searchable text, embedded fonts, image presence, file size, and PDF metadata before publishing;
- include a CJK font discovery check and fail visibly when required glyphs cannot render.

The wkhtmltopdf project documents that its Qt 4/WebKit base is obsolete. It should not remain the long-term primary renderer.

## CLI, API, and UI

### CLI

Add:

```text
analyze-video audio PROJECT_ID --mode auto|basic|enhanced
analyze-video export PROJECT_ID --template client-storyboard --format xlsx --format pdf
```

The normal `run` path uses `--mode auto` for structured audio evidence but creates no Excel or PDF. `export` is the only CLI path that renders client files and never invokes a network adapter implicitly.

### API

Add versioned endpoints:

```text
GET   /api/projects/{id}/audio-timeline
PATCH /api/projects/{id}/audio-segments/{segment_id}
POST  /api/projects/{id}/exports/client
GET   /api/projects/{id}/exports/client/status
```

Mutations use the existing CSRF, project lock, optimistic digest, invalidation, and finalization rules.

### React workspace

Add an Audio mode with:

- waveform/energy/rhythm tracks;
- filterable voice, music, SFX, silence, and mixed segments;
- synchronized player seek;
- transcript and anonymous-speaker editing;
- VO/dialogue role confirmation;
- per-segment confidence and provenance;
- unresolved-item counter.

Extend Export mode with:

- template and language selection;
- a **Generate client package** button and explicit confirmation of formats;
- draft versus finalized eligibility and stale/current status;
- missing renderer/adapter explanations;
- Excel, HTML, and PDF availability;
- a stable current package plus an explicit **Save as version** action;
- saved-version count, disk usage, download, and explicit delete controls;
- the same Available/Blocked/Missing truth states used by current deliverables.

## Implementation sequence

### Gate 0: establish a change baseline

The repository currently has no Git `HEAD`; all project files appear untracked. Before implementation, create an authorized baseline commit or otherwise freeze an exact file-set hash. Without this, diff-based impact review and rollback evidence are incomplete.

Do not commit or publish without explicit user authorization.

### Phase 1: contracts and backward-compatible timeline

- add timeline schemas, validators, fixtures, and generation receipt;
- construct baseline segments from current transcript, beats, energy, and silence;
- keep legacy audio artifacts byte-compatible;
- expose timeline read-only through CLI/API;
- add synthetic overlap, silence, speech, and music fixtures.

Exit gate: old projects still load; current audio/report tests pass; timeline validation rejects overlap, out-of-range timing, non-finite confidence, and unsupported kinds.

### Phase 2: shared client dataset and Excel

- port the validated one-off workbook design into generic templates;
- remove every project-specific title, section, and path;
- build `client-export-dataset/v1` from current source-of-truth files;
- generate draft or professional Excel only through the explicit export action;
- atomically replace the stable current slot without creating an automatic history;
- add formula-injection, image-bound, multilingual-font, and deterministic-order tests.

Exit gate: a synthetic project with any shot count produces all required sheets, one image per shot, complete timecodes, and explicit blanks instead of fabricated VO/SFX.

### Phase 3: HTML/PDF and artifact registry

- render the shared dataset as print-specific HTML;
- add Playwright PDF adapter and current-renderer fallback;
- centralize artifact registration;
- add export-generation receipt and readiness binding;
- surface draft/final files in API and UI.

Exit gate: PDF text, images, Chinese glyphs, metadata, page size, and receipt hashes verify; mutated inputs block old final exports.

### Phase 4: audio review UI and finalization

- add synchronized audio timeline and segment editor;
- invalidate draft/final export receipts on edits;
- keep Finalize and client export as separate observable actions;
- generate the final client package only after the user clicks Generate or invokes the CLI command;
- prove blocked → reviewed → finalized → mutation blocked → refinalized.

Exit gate: the full workflow runs from the production React app at desktop, tablet, and mobile breakpoints without terminal editing.

### Phase 5: enhanced adapters and quality benchmark

- add diarization and separation adapter protocols;
- implement `doctor` checks, timeouts, resource caps, and provenance;
- benchmark with synthetic or licensed speech/music/SFX mixtures;
- publish quality limitations rather than one aggregate score.

Exit gate: optional adapter failure cannot remove baseline outputs; no model download or external request occurs without explicit setup and invocation authority.

### Phase 6: release hardening

- run full Python, frontend, install, API, lifecycle, security, and responsive gates;
- validate macOS and Linux clean installs;
- perform independent code, product, privacy, and artifact cold reviews;
- refresh screenshots, README, FAQ, architecture, and migration notes;
- keep Windows and live-provider claims explicitly unverified until separately proven.

## Quality gates

| Area | Release gate |
| --- | --- |
| Export completeness | 100% of current shots appear exactly once with timecode and frame status |
| VO/transcript | every emitted token is traceable to ASR/import/human source; no silent fallback over reviewed blanks |
| Music/SFX | every label carries timing, confidence, source, and verification state |
| Excel | valid ZIP/XLSX, expected sheets, safe text cells, bounded embedded media, deterministic row order |
| PDF | opens successfully, searchable text, CJK glyph coverage, images present, A4 print contract, valid receipt |
| Layout | versioned template passes cover, overview, storyboard, VO/text, music/SFX, overflow, and pagination visual regressions without manual correction |
| Provenance | final exports bind media, visual, audio, timeline, review, report, and template generations |
| Mutation safety | any bound input or output byte change blocks professional download |
| Storage | normal analysis produces no Excel/PDF; Generate replaces one current slot; only explicit Save as version creates history |
| Accessibility | HTML/PDF headings, table headers, alt text, keyboard workflow, and tagged PDF when supported |
| Performance | explicit baseline client-package generation target under 30 seconds on the prepared reference machine |
| Privacy | local by default; no automatic model/browser download or external audio upload |

## Benchmark plan

Public fixtures must be synthetic or redistributable. Keep private reference video only as a local acceptance fixture and never add it to the repository.

Tests write generated workbooks, PDFs, rendered pages, extracted frames, and temporary fonts only under process-scoped temporary directories. Passing tests remove them. CI may upload a bounded failed-test diagnostic artifact with an explicit retention period; successful runs do not publish generated documents. Small reviewed golden fixtures may be committed only when they are stable, redistributable, and necessary for regression detection.

The benchmark set should include:

- clean VO over music;
- dialogue with two anonymous speakers;
- speech/music overlap;
- isolated transition hits and mechanical SFX;
- intentional silence and low-energy ambience;
- fast montage with beat-aligned and off-beat cuts;
- Chinese and English speech;
- no-speech and no-music controls.

Report separate metrics:

- ASR character/word error and timing error;
- anonymous diarization error when enabled;
- segment boundary tolerance;
- music/SFX event precision and recall by supported class;
- export field coverage and provenance coverage;
- render success and visual regression results;
- runtime, peak memory, and optional-engine status.

## Delivery definition

This initiative is complete only when a new user can:

1. install the declared extras on a supported platform;
2. analyze a local video through the normal UI or CLI;
3. review structured storyboard, VO/text, music, SFX, and rhythm data without creating client files;
4. click Generate client package when a draft is needed;
5. review uncertain fields and Finalize once when a professional package is required;
6. click Generate again and download current Excel and PDF files whose receipts match the reviewed project;
7. regenerate into the same stable current slot without accumulating automatic copies;
8. reproduce the same outputs from the same frozen inputs and template version.

Until these gates pass, the reference workbook and PDF remain validated one-off deliverables, not evidence that the product feature is complete.
