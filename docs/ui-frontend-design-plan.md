# Frontend product and design contract

Status: target contract for the Video Evidence Workbench redesign.

The interface exists to review evidence, not to simulate a team of autonomous tools. Every visible value must come from a real project response or an explicitly labeled demo fixture.

This document defines the target and its acceptance boundaries; it is not an implementation or verification receipt. A requirement below is implemented only when current source plus a fresh build/test or interaction receipt proves it.

## Reference concepts

- [Desktop workspace concept](ui-concepts/workspace-desktop-concept.png)
- [Mobile workspace concept](ui-concepts/workspace-mobile-concept.png)

These generated images define information hierarchy and visual direction. They are not proof that every pictured control is implemented. Final browser screenshots and interaction checks are the verification evidence.

## Current production-build evidence

- [Desktop workspace with the local synthetic demo](screenshots/workspace-desktop-1440x900.png)
- [Desktop human-review drawer](screenshots/review-drawer-desktop-1440x900.png)
- [Mobile export truth states with the same demo](screenshots/mobile-export-390x844.png)
- [Machine-readable capture and asset receipt](screenshots/ui-acceptance-receipt.json)

The screenshots use repository-generated synthetic media and show the human-review path through a finalized local package; no external vision provider ran. The receipt also records the initial blocked state, explicit finalization, post-finalization invalidation, byte-identical restoration remaining blocked, and a second finalization. It binds the images to the served production assets and records local browser checks. These files are implementation evidence, not proof of a public release or factual model accuracy.

## Primary jobs

1. Create or open a local-file project; URL ingest remains a trusted-operator CLI boundary.
2. Watch the source and move between shot boundaries.
3. Inspect one shot's measured, estimated, annotated, and reviewed fields.
4. See why the package is ready or blocked.
5. Open the source/provenance files.
6. export or open the generated Codex handoff explicitly;
7. find human-readable and machine-readable deliverables.

## Information architecture

### Desktop

- **Project header:** title, duration, local-data indicator, readiness summary, provenance action, Codex handoff action.
- **Left navigation:** new project, projects, shots, deliverables, settings, about.
- **Evidence stage:** review video and standard playback controls.
- **Shot strip:** numbered keyframes aligned to a time ruler; selection moves the player and inspector.
- **Evidence timeline:** shot boundaries and audio energy, with confidence encoded accessibly.
- **Shot inspector:** timecode, duration, shot scale, camera, action, transcript, annotation source, evidence files.
- **Shot table:** dense scan view with stable columns and row selection.
- **Readiness footer:** named checks with pass/warn/block state and evidence-package action.

An infinite graph, generic chat rail, payment flow, and image-generation dock are not part of the default workspace.

### Mobile

- compact project header and readiness state;
- top-level tabs for Video, Shots, Evidence, and Export;
- edge-to-edge player;
- horizontally scrollable shot strip with a visible selected shot;
- one shot-detail card with expandable secondary fields;
- collapsed provenance and readiness sections;
- sticky evidence-package and Codex-handoff actions.

Mobile is an inspection and triage surface. Bulk table editing can remain desktop-first.

## Visual system

### Color

| Token | Intent | Target |
| --- | --- | --- |
| `--surface` | page background | warm white, approximately `#FAF9F7` |
| `--panel` | primary content | `#FFFFFF` |
| `--ink` | primary text | near-black, approximately `#171717` |
| `--muted` | secondary text | neutral gray with WCAG-readable contrast |
| `--line` | rules and boundaries | light neutral gray |
| `--accent` | selected shot and primary action | dark vermilion, approximately `#C13A24`, with WCAG AA contrast on white and warm paper |
| `--success` | reviewed/passed | restrained green |
| `--warning` | incomplete/needs review | amber |
| `--blocked` | gate failure | red distinct from the selection accent |

No gradients, decorative glow, or color-only status communication.

### Typography

- neutral system sans-serif for interface and narrative fields;
- tabular or monospaced numerals for timecode, duration, IDs, and file sizes;
- project title 20–24px desktop and 18–20px mobile;
- body text at least 14px desktop and 16px for mobile reading surfaces;
- sentence case; avoid all-caps operational theater.

### Density and shape

- 4px base spacing with common steps 8, 12, 16, 24, and 32px;
- mostly square or lightly rounded panels; reserve stronger rounding for grouped mobile sections;
- one-pixel rules establish hierarchy;
- controls use familiar icons plus labels where ambiguity is possible;
- do not turn metadata into decorative pills.

## Data contract

### No silent success

Failed API requests must display an error and a retry. Mock/demo data is allowed only when explicitly enabled and visibly labeled. A real project must never be combined with fabricated shot rows, readiness, transcript, or provenance.

The main workspace must come from one lock-consistent API snapshot with a content digest and, when present, the verified committed report generation ID. The client must not assemble a screen by racing independent project, media, shot, readiness, and deliverable reads.

### Field provenance

The inspector should make these categories distinguishable:

- **Measured:** timing, duration, frame path, resolution.
- **Estimated:** boundary confidence, scene grouping, rhythm.
- **Provider annotated:** visual labels and provider confidence.
- **Human reviewed:** edited fields and review state.

The UI must not call a provider annotation “human annotated.”

### Readiness language

- `ready`: configured checks passed;
- `needs_review`: evidence exists but review is required;
- `blocked`: one or more named gate reasons prevent the package;
- `draft`: the relevant stage has not completed.

Never translate readiness into “verified truth.”

### Codex handoff

The action targets the real `reports/codex_handoff.md` artifact. It may open, preview, reveal, or copy its path. It must not imply that a Codex task has started or that files were uploaded.

## Interaction details

- Selecting a shot updates player time, strip highlight, inspector, and table row.
- Keyboard shortcuts are discoverable and do not override browser or assistive-technology conventions.
- Timecode can be copied without copying hidden formatting.
- Missing frames show an explicit unavailable state, not a generic stock image.
- Long transcripts and descriptions wrap without covering playback or actions.
- Blocked workspaces render the backend readiness reasons/checks directly; reasons link to the affected field or shot where possible.
- A deliverable blocked by the readiness gate remains visibly disabled even when the file is present; the UI does not offer an action that the backend will reject.
- Destructive project actions require scope confirmation and are not primary controls.
- Modal drawers move focus to Close, trap forward and reverse Tab, close on Escape/backdrop, restore opener focus, and make the background inert and hidden from assistive technology while open.

## Responsive behavior

### ≥1200px

Two-column stage: video/timeline left and inspector right, with the shot table below.

### 768–1199px

Inspector becomes a drawer or stacked panel. Navigation can collapse, but project/readiness context stays visible.

### <768px

Single column, tabbed sections, sticky bottom actions. No horizontal page overflow; only the shot strip may scroll horizontally.

## Accessibility acceptance

- Core flow works with keyboard only.
- Every icon-only control has an accessible name and visible tooltip/focus state.
- Selected shot uses border/icon/text in addition to color.
- Readiness state is announced in text.
- Video controls remain native or provide equivalent labels and focus behavior.
- Motion respects `prefers-reduced-motion`.
- Minimum touch target is 44×44px.
- Text and control contrast meet WCAG 2.2 AA targets.

## Required states

- loading skeleton that preserves layout;
- empty workspace with one clear intake action;
- invalid or unsupported source;
- pipeline running with current stage;
- API unavailable with retry and command hint;
- no vision credentials;
- missing optional ASR/PDF tool;
- blocked readiness with reasons;
- no frame for a shot;
- Codex handoff not generated yet;
- completed package with artifact list.

## Visual verification checklist

At minimum, compare the implemented UI with both concept images on:

1. project/readiness hierarchy;
2. video-to-inspector balance;
3. selected-shot synchronization;
4. evidence and provenance visibility;
5. Codex handoff wording;
6. desktop table readability;
7. mobile sticky actions and overflow.

Record desktop and mobile screenshots after a production build. A generated concept is not an acceptance screenshot.
