# Mature product benchmark: evidence timelines, audio intelligence, and client exports

Status: T01 research evidence, based on official documentation and official source repositories read on 2026-07-22.

This benchmark asks a narrow product question: which established patterns make Video Evidence Workbench more trustworthy and useful without turning the local-first core into an unmaintainable bundle of heavyweight models and renderers?

## Decision summary

The mature pattern is not “run more AI.” It is a layered workflow:

1. create deterministic, versioned time ranges and evidence references;
2. let optional models propose richer labels;
3. make uncertain segments visible and editable;
4. keep one canonical reviewed dataset;
5. render client formats from that dataset only after an explicit user action;
6. bind every artifact to the source generation and make stale state visible.

The project should adopt interaction and schema patterns from mature tools, while keeping most heavyweight audio packages behind optional adapters. No researched project provides the complete product contract required here.

## Official-source comparison

| Project | Mature pattern worth learning | Current constraint or risk | Decision for this project |
|---|---|---|---|
| [OpenTimelineIO](https://opentimelineio.readthedocs.io/en/v0.16.0/tutorials/architecture.html) | Timelines use explicit start plus duration, typed schema objects, tracks, and adapters; the native format is versioned and lossless. | It models editorial interchange, not evidence confidence, human review, audio semantics, or client reports. | Adopt half-open time ranges, explicit coordinate systems, versioned schemas, and adapter boundaries. Do not add OTIO as a core dependency in v1; add an optional adapter later if user demand exists. |
| [Label Studio Audio](https://labelstud.io/tags/audio.html) | Waveform, zoom, playback speed, synchronized media, region selection, and per-region results are first-class. | Some advanced multi-channel transcript editing uses Enterprise-only `ReactCode`; the product cannot depend on that implementation. | Build an original React timeline using the interaction contract: layers, zoom, playhead, selected region, keyboard control, evidence outliner, and explicit save. |
| [Label Studio audio workflow](https://labelstud.io/videos/labeling-audio-data-with-label-studio/) | Segment looping, hotkeys, undo/redo, snapping, draft state, and an outliner reduce correction cost for long audio. | Its generic annotation model does not enforce this product's Finalize and generation-digest rules. | Adopt correction-first interaction and unresolved-segment navigation; retain the existing stricter provenance lifecycle. |
| [pyannote.audio](https://github.com/pyannote/pyannote-audio) | Produces speaker turns, overlapped speech, speech activity, progress hooks, and benchmarked diarization under an MIT code license. | The community pipeline requires accepting model conditions and a Hugging Face token for initial retrieval; it is a large PyTorch stack and can emit optional telemetry. Premium execution sends audio to a service. | Optional local enrichment only. Never make it a default install, never silently request a token/download, disclose telemetry controls, validate outputs locally, and preserve `unknown` when diarization is absent. |
| [OpenAI Whisper](https://github.com/openai/whisper) | Multilingual transcription with segment timestamps and several model-size/resource tradeoffs; code and weights are MIT licensed. | Model loading downloads into a cache when given a model name; resource needs range from roughly 1 GB to 10 GB VRAM, and official compatibility guidance centers on Python 3.8–3.11. The model card warns that diarization and subjective classification are not robustly evaluated uses. | Keep the existing optional Whisper path, but require explicit model installation/configuration, expose model provenance, and never infer speaker identity, emotion, or human attributes as fact. |
| [Demucs](https://github.com/facebookresearch/demucs) | Separates vocals, drums, bass, and accompaniment and demonstrates useful source-separation capability. | The official repository states it is no longer maintained, and the successor fork only accepts important fixes. | Do not make Demucs a direct or default dependency. Define a generic source-separation adapter so a maintained implementation can be selected later. |
| [Essentia](https://essentia.upf.edu/streaming_extractor_music.html) | Offers frame-level and summarized rhythm, spectral, tonal, beat-position, BPM, and loudness descriptors. | The library is AGPLv3 for non-commercial use, models have additional non-commercial terms, and some dependency rights require separate clearance. Version changes can make high-level models incompatible. | Use its descriptor taxonomy as research input only. Do not link or bundle Essentia in the MIT core. Implement small deterministic features locally or evaluate a permissively licensed alternative in the optional adapter lane. |
| [PySceneDetect](https://www.scenedetect.com/docs/latest/api/detectors.html) | Multiple detectors, per-frame statistics, minimum scene lengths, adaptive thresholds, and export adapters provide a good separation between measurement and editorial output. | Its v0.7 release is breaking and official docs warn the API remains under development. | Preserve detector metrics and human boundary review as evidence; benchmark rather than blindly replace the current detector. If adopted, pin a compatible version and add migration tests. |
| [openpyxl](https://openpyxl.readthedocs.io/en/stable/) | Native OOXML workbook creation; supports images, filters, print titles, print areas, orientation, and page setup. | It does not visually render the workbook, cannot prove Excel/LibreOffice appearance from XML alone, and official docs warn about unsafe XML when reading untrusted workbooks. | Use for generation only, never accept arbitrary client workbooks in v1, validate OOXML structure, and add an explicit visual/manual acceptance gate for the template. |
| [Playwright `page.pdf`](https://playwright.dev/docs/api/class-page#page-pdf) | Print CSS, A4 format, landscape, margins, CSS page-size precedence, backgrounds, outlines, and tagged PDFs are controllable from one browser renderer. | Chromium installation is large and `page.pdf` is Chromium-only; header/footer templates have styling/script limitations. | Preferred professional PDF adapter, installed explicitly as an export extra. Use versioned print HTML/CSS, `preferCSSPageSize`, backgrounds, and tagged output. Never download a browser at runtime. |

## Product-pattern benchmark

### 1. Timeline data contract

Adopt:

- half-open ranges `[start, end)` in seconds with source media timebase recorded separately;
- stable event IDs and schema versions;
- separate machine proposal, human override, and effective value;
- `unknown` and `mixed` as valid states rather than errors;
- confidence tied to method and evidence, not a universal pseudo-probability;
- cross-links from audio events to shots without rewriting either source record.

Reject:

- a single free-form “sound description” per shot;
- speaker names inferred from acoustic clusters;
- forcing every region into VO, music, or SFX when layers overlap;
- destructive merge of human edits into machine evidence.

### 2. Review interaction

Adopt:

- correction-first UI initialized from machine proposals;
- synchronized playback, zoom, loop, playhead, keyboard shortcuts, region resize, split, merge, and undo;
- a visible unresolved/low-confidence queue;
- an outliner for event navigation and screen-reader access;
- draft save separate from Finalize.

Reject:

- canvas-only information with no accessible list representation;
- automatic acceptance of high-confidence predictions;
- saving after every pointer movement without optimistic-concurrency protection;
- hiding unsupported audio capabilities behind empty charts.

### 3. Optional model boundary

Adopt:

- provider capability discovery before a run;
- explicit install and explicit enablement;
- version, model, device, duration, parameters, and output digest in the receipt;
- bounded time, memory, disk, and cancellation;
- deterministic baseline result preserved when enrichment fails;
- local schema validation before provider output becomes project state.

Reject:

- implicit downloads during analysis;
- mandatory tokens for the base product;
- silent cloud fallback;
- an unmaintained model repository as a product-critical dependency;
- license-incompatible libraries in the MIT core.

### 4. Client export architecture

Adopt:

- one canonical `client-export` dataset shared by XLSX, HTML, and PDF;
- versioned templates with a layout preflight;
- explicit generation, progress, cancellation, atomic `current` replacement, and explicit saved versions;
- a receipt binding dataset digest, template digest, renderer version, source generation, and output hashes;
- structural workbook/PDF tests plus visual acceptance fixtures.

Reject:

- generating files during analysis or Finalize;
- renderer-specific business logic;
- timestamped output on every test run;
- shrinking text below the minimum size to avoid pagination;
- treating “the file opens” as professional layout proof.

### 5. Open-source maturity

Adopt:

- a five-minute synthetic quick start that never needs private media or a model token;
- optional extras for API, transcription, diarization, spreadsheet, and PDF capabilities;
- `doctor` output that distinguishes installed, configured, available, and actually verified;
- evidence-linked screenshots and receipts refreshed only at candidate freeze;
- issue templates, security policy, contribution guide, architectural decision records, migration guide, and honest platform support.

Reject:

- star-count claims as engineering verification;
- screenshots that are not bound to the current assets;
- “AI powered” positioning without a concrete user task;
- a clean-install path that depends on undeclared global pytest, ffmpeg, fonts, or browser caches.

## Quality metrics to carry into implementation

| Area | Minimum measurable signal |
|---|---|
| Shot/audio alignment | event-to-shot association tested at exact boundaries and tolerance edges |
| Speech | segment timestamp error and transcript completeness on licensed fixtures; no forced speaker identity |
| Music/rhythm | beat-position tolerance, tempo bucket stability, energy-segment boundary tolerance |
| Review | unresolved count, edits per event, correction latency, stale-conflict recovery |
| XLSX/PDF consistency | canonical dataset digest plus field-by-field cross-format fixture assertions |
| Layout | no clipping, minimum font size, predictable continuation pages, missing-image fallback |
| Storage | one atomic current package by default; no unbounded test/export artifacts |
| Reliability | cancellation, concurrent-click, renderer crash, disk-full, and restart recovery tests |
| Onboarding | clean environment to first successful synthetic analysis and explicit export |

## Implications for the Task plan

- T04–T06 implement the time-range, evidence, and correction contract.
- T07 is an adapter lane; pyannote and any separator stay optional and fail-soft.
- T08 is the only source of client-facing report data.
- T09–T12 enforce professional layouts and explicit artifact lifecycle.
- T14–T17 implement a correction-first, accessible timeline and export center.
- T18–T20 add resource limits, clean dependencies, migration, and onboarding.
- T22 refreshes screenshots and receipts against one final candidate; old evidence remains stale until then.

## Source-quality boundary

All sources above are official project documentation or official source repositories. No search-result popularity, community anecdote, or star count is used as proof of suitability. “Adopt” decisions are architectural inferences for this project and still require local implementation tests.
