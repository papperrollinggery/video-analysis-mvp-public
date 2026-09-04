# Real-data frontend verification plan

This checklist protects the boundary between a real project and demo data.

## Contract

- Project, media, shots, deliverables, readiness, and provenance come from the selected workspace.
- An API failure is visible and retryable.
- Demo mode, if enabled, is explicit and labeled on every affected page.
- A response must not be filled with plausible hard-coded copy.
- File links remain project-scoped and must not escape the workspace root.

## Setup

```bash
npm --prefix frontend ci
npm --prefix frontend run test:integration
npm --prefix frontend run build

.venv/bin/analyze-video \
  --workspace ./analysis-projects \
  serve --host 127.0.0.1 --port 8787
```

Analyze a non-sensitive local sample with the README quick start. `scripts/api-smoke-test.sh` verifies the API in an ephemeral workspace and removes that workspace on exit, so it is not a persistent browser fixture.

## API checks

Replace `<project-id>` with a generated project:

```bash
curl -fsS http://127.0.0.1:8787/api/runtime/doctor
curl -fsS http://127.0.0.1:8787/api/projects/<project-id>/workspace
```

The workspace route reads project, media, shots, readiness, lineage, and deliverables under the shared project locks, then returns one `snapshot_id` and the verified committed report `generation_id`. Confirm that those bindings are present and that shot counts, project title, readiness reasons, and artifact paths match on-disk JSON. The frontend must not assemble one screen from independently timed project/media/shot/deliverable responses.

## Browser checks

### Desktop

- open the project from the project list;
- play the real review video;
- select at least three shots from strip and table;
- confirm player time, selected row, primary frame, and inspector timecode remain synchronized;
- open readiness and compare every reason with `data/readiness.json`;
- open provenance and compare with `data/lineage.json`;
- locate `reports/codex_handoff.md` and `data/visualization_dataset.json` in deliverables;
- stop the API and confirm the UI shows a real error instead of demo data;
- capture a production-build screenshot at 1440×900.

### Mobile

- test at 390×844 or narrower;
- verify no page-level horizontal overflow;
- confirm the shot strip is independently scrollable;
- open one shot's details, provenance, and readiness;
- confirm sticky actions do not obscure content or browser controls;
- capture a production-build screenshot.

## Build and smoke gates

```bash
npm --prefix frontend run test:integration
npm --prefix frontend run build
sh scripts/api-smoke-test.sh
sh scripts/install-smoke-test.sh
```

Build success does not prove data truth or responsive usability; keep the API and browser receipts with release notes.
