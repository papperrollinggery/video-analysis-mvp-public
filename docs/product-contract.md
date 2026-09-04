# Video Evidence Workbench product contract

Status: current contract for the mature local candidate. The deterministic
core, review/finalization lifecycle and explicit client-export path are
implemented; optional semantic providers, corpus workflows, public release and
cross-platform claims remain conditional or unverified. Runtime details are
described in `docs/architecture.md`.

Contract version: 0.3-draft

## Product promise

Video Evidence Workbench turns one short video into a reviewable evidence timeline and, only when the operator asks, a professional client package.

The product succeeds when an operator can answer all of these questions without opening or editing raw JSON:

1. Where does every relevant shot, spoken segment, music section, sound event, beat, pause, and on-screen statement occur?
2. Which values were measured, inferred by a model, or asserted by a person?
3. Which uncertain or missing items still require review?
4. Does the current finalized package still match the source and reviewed evidence?
5. Can the operator explicitly generate a consistent XLSX/PDF package suitable for client review?

It is not a chat answer, an autonomous creative director, or a factual certification service. Its durable value is an inspectable project whose claims remain connected to source bytes, time ranges, frames, methods, confidence, review state, and artifact digests.

## Primary users and jobs

### 1. Agency creative strategist or director

Job: deconstruct a brand film, AI film, commercial, or social video into a client-ready account of shots, screen text, VO, music, sound design, and pacing.

Success: review uncertain events in the browser, finalize the evidence, click an explicit export action, and send a professionally formatted package without reformatting cells or pages.

### 2. Video analyst or media researcher

Job: produce a traceable shot/audio dataset for qualitative coding, comparison, teaching, research, or model evaluation.

Success: every observation resolves to a time range and evidence reference; missing or model-derived values remain visible; JSON/CSV/HTML remain usable without the web client.

### 3. Editor, AI filmmaker, or video-AI engineer

Job: reuse reviewed structure in an edit, recreation brief, prompt workflow, benchmark, or downstream Codex/notebook task.

Success: consume stable schemas and generation receipts without trusting a screenshot or a free-form summary as the source of truth.

These are profiles over one evidence model. They do not create separate project formats.

## Three complete user workflows

### Workflow A — Analyze and inspect

Input:

- one supported local video;
- a project/profile choice;
- optional explicit ASR/model configuration.

Flow:

1. ingest and bind source/review media;
2. detect shots and extract keyframes;
3. create the deterministic audio timeline;
4. optionally run explicitly enabled enrichment adapters;
5. open a project workspace with stage status and evidence.

Output:

- measured media metadata;
- shots/scenes/keyframes;
- transcript and audio events;
- machine proposals with methods and confidence;
- no XLSX or client PDF.

Failure and recovery:

- stage failure is durable and names the failed capability;
- retry starts at the first invalid generation;
- optional enrichment failure preserves the deterministic result;
- cancellation cannot publish a partial generation.

### Workflow B — Review and finalize evidence

Input:

- a complete analysis generation;
- unresolved shot, boundary, transcript, or audio events.

Flow:

1. navigate every unresolved event from a DOM outliner or timeline;
2. play, zoom, loop, split, merge, relabel, or explicitly keep `unknown`/`mixed`;
3. save drafts with an optimistic generation/edit digest;
4. resolve structural readiness requirements;
5. explicitly Finalize the evidence package.

Output:

- human assertions separated from original machine evidence;
- a committed, digest-valid report generation;
- old client exports marked stale, never silently regenerated.

Failure and recovery:

- conflicting edits produce a visible reload/reapply path;
- save or Finalize failure leaves the prior committed generation intact;
- restoring byte-identical content does not revive an invalidated generation;
- only a new explicit Finalize commits the current evidence.

### Workflow C — Explicit client export and companion research

Input:

- a current finalized report generation;
- a template/language selection;
- an explicit request for XLSX, PDF, or both.

Flow:

1. build one validated `client-export` dataset;
2. run layout preflight;
3. render in a private staging directory with progress and cancellation;
4. validate outputs;
5. atomically replace the `current` export package;
6. optionally save a named version;
7. explicitly hand selected local artifacts to Codex Desktop or another tool.

Output:

- consistent XLSX/PDF data;
- output and renderer receipts bound to dataset, template, and source generations;
- one `current` package by default, plus only explicitly saved versions.

Failure and recovery:

- failure or cancellation cannot replace the previous valid `current` package;
- concurrent duplicate requests are idempotent;
- stale results remain downloadable with a warning but cannot be presented as current;
- Codex/Desktop, OpenAI Vision, or deep research never starts implicitly.

## Truth and confidence contract

Every user-visible evidence value belongs to one of four categories:

| Category | Examples | Required treatment |
|---|---|---|
| Measured | source hash, duration, time range, pixel dimensions, frame digest | preserve units/method and fail on invalid values |
| Machine estimate | shot boundary, transcript, music interval, SFX candidate, subject/action | store provider/method/model and confidence; allow `unknown` |
| Human assertion | corrected text, accepted boundary, assigned event class, review note | store edit digest and timestamp; never rewrite the original estimate |
| Interpretation | narrative role, creative takeaway, pacing explanation | label as interpretation and retain evidence links |

No global confidence score may convert these categories into certainty. Speaker clusters are anonymous unless the user explicitly names them. Emotion, intent, identity, authenticity, copyright ownership, and legal permission are never inferred as verified facts.

Audio layers may overlap. `mixed` is a valid event state; silence is an observed interval, not missing data; a missing analyzer result is `unknown`, not silence.

## Lifecycle contract

### Analysis run

```text
queued → ingest → visual → audio → report → finalize → completed
```

This existing run creates the reviewable evidence package. It never creates client XLSX/PDF.

### Evidence review

```text
machine proposal → draft human edits → structurally ready → finalized current
                                      ↘ mutation → stale/blocked → re-finalize
```

### Client export

```text
requested → preparing dataset → layout preflight → rendering → validating → publishing → current
     ↘ cancelled / failed ────────────────────────────────────────────────┘
```

Export is a separate transaction and receipt family. It is not inserted into the normal analysis stage percentages. A project mutation changes a valid client export to `stale`; it does not enqueue regeneration.

## Storage contract

Planned target layout:

```text
<project>/
├── data/
│   ├── audio_intelligence.json
│   ├── audio_intelligence_generation.json
│   ├── artifact_registry.json
│   └── client_export_dataset.json
├── reports/
│   ├── client/current/
│   │   ├── client_breakdown.xlsx
│   │   ├── client_breakdown.pdf
│   │   └── export_receipt.json
│   └── client/saved/<version-id>/
└── .staging/                 # private, bounded, recoverable temporary work
```

Rules:

- `current` is replaced atomically as a package;
- saved versions require explicit user action and never disappear through automatic retention;
- caches and abandoned staging data may be cleaned only by documented bounded rules;
- test exports use temporary roots and are removed after successful tests;
- the artifact registry stores project-relative paths only;
- private source paths and credentials never enter customer documents.

## Capability tiers

| Tier | Availability | Contract |
|---|---|---|
| Core | installed by default | ingest, deterministic visual/audio measurements, review, readiness, JSON/CSV/HTML evidence |
| Spreadsheet extra | explicit install | professional XLSX generation and structural validation |
| PDF extra | explicit install plus preinstalled browser runtime | professional print HTML/PDF; no runtime browser download |
| ASR extra | explicit model installation/configuration | transcript proposals; no implicit model download during a run |
| Diarization/separation extra | explicit install, license review, resource limits | anonymous speaker/source proposals; deterministic fallback on any failure |
| Provider adapters | explicit credentials and action | bounded external calls with clear privacy/cost/retention boundary |

`doctor` must distinguish `not installed`, `installed`, `configured`, `available`, and `verified in this environment`.

## Product quality metrics

These are release gates or measurement contracts, not current claims.

| Dimension | Mature local-candidate gate |
|---|---|
| First success | a clean supported environment completes the synthetic analyze → review → Finalize → explicit export workflow using documented commands |
| Evidence traceability | 100% of client rows resolve to project ID, shot/event ID, time range, generation digest, and source evidence |
| Cross-format consistency | all shared XLSX/PDF fields equal the canonical dataset fixture; output hashes and dataset digest are recorded |
| Export behavior | zero XLSX/PDF files before explicit request; failure/cancel/concurrent duplicate tests preserve the previous `current` package |
| Audio honesty | silence, pure music, speech, overlap, and corrupted-audio fixtures produce valid `unknown`/`mixed` outcomes without fabricated speaker identity |
| Timeline boundaries | deterministic synthetic event boundaries meet the tolerance documented per detector; exact-edge association tests cover `[start, end)` semantics |
| Layout | no clipped required content; minimum 8.5 pt body text; repeated headers/continuation pages; missing-image fallback; A4 landscape PDF |
| Accessibility | complete review and export path is keyboard reachable, has visible focus and an accessible list equivalent for timeline events |
| Responsive UI | no blocking overflow at 1440×900, 900×1000, and 390×844; all primary actions remain reachable |
| Reliability | restart, disk failure, cancellation, duplicate click, stale generation, migration rollback, and renderer failure tests pass |
| Privacy | no implicit network/model download; no private media, absolute source path, credential, macro, external workbook link, or formula injection in outputs |
| Verification | all applicable tests, install smoke, browser evidence, performance/security checks, and independent cold review bind one candidate digest |

Product telemetry remains opt-in. Local workspaces are never scanned to claim adoption.

## Maturity definitions

### Usable local alpha

- one supported local short video can be analyzed and reviewed;
- limitations and stale evidence are visible;
- developer environment may still be required.

### Mature local candidate

- all three workflows above pass from a clean supported environment;
- old projects migrate or fail with a recoverable explanation;
- professional outputs pass structural and visual acceptance;
- current browser, security, performance, and cold-review evidence bind one file-set digest;
- no P0/P1 finding remains.

### Public release

- requires a user-authorized Git baseline, canonical remote, license/fixture audit, CI on each declared platform, release notes, public demo assets, and publication readback;
- local completion does not imply publication, user adoption, or a GitHub star count.

## Non-goals for the mature local candidate

- hosted accounts, payment, authentication, multi-tenant or real-time collaboration;
- a full non-linear editor, DAW, or arbitrary template designer;
- background cloud processing or silent provider fallback;
- automatic export during analysis, save, or Finalize;
- automatic publication, content generation, or consequential decisions;
- factual, legal, copyright, deepfake, authenticity, or identity certification;
- a promise of 2,000 GitHub stars.

## Codex Desktop and OpenAI companion boundary

Codex Desktop can inspect the repository, run explicit local commands, help review evidence, and prepare client or research artifacts when the user requests it. The product may expose a copyable project context and safe local paths, but it does not programmatically control Codex tasks.

OpenAI Vision/provider analysis is an optional evidence proposal. Deep research is a separate web-research workflow. Neither is invoked by importing a video, opening a project, saving a review, finalizing, or clicking a local download link.

Every companion workflow must state which files were selected, which external boundary was crossed, and which receipt or citation proves the action actually occurred.

## Change control

- Schema changes require a version, fixture, migration, rollback path, and compatibility test.
- A renderer is an adapter over the canonical dataset; it cannot add business facts.
- A provider is an adapter over validated project evidence; it cannot mutate measured timing.
- New client outputs require an explicit action and artifact-registry entry.
- No readiness or publication rule may be weakened merely to make a demo or test green.
