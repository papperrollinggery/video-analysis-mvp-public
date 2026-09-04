# T14/T15: progressive workbench and audio-review UI

Date: 2026-09-01 (Asia/Shanghai). Scope: the existing React/Vite workbench, shared audio API types/client and the operator-facing audio timeline. This is not professional Excel/PDF export, semantic-model accuracy or release acceptance.

## Design frame

- **Purpose:** let a local operator see VO/dialogue, music, SFX, mixed measurements and threshold silence in the same evidence workspace as picture, then cue, inspect and review one source-bound event.
- **Audience:** filmmakers, researchers and client-delivery operators who need audit evidence rather than a black-box summary.
- **Direction:** the existing industrial/editorial evidence desk — white work surface, fine rules, brick-red selection, monospace timecode, dense horizontal rails and a fixed inspector. No new framework, font, design dependency, decorative AI gradient, fake metric or generic card dashboard.
- **Accepted baseline:** `docs/screenshots/workspace-desktop-1440x900.png` and `docs/screenshots/mobile-export-390x844.png`. This is an additive feature inside that accepted system, so no unrelated Image Gen redesign was introduced.
- **Memorable task:** click an audio interval and immediately see the player seek to the same evidence time, while original proposal and effective operator decision remain visibly distinct.

## Delivered

- Typed `audio-review/v1` page, event, capability, review and result contracts in `frontend/src/types.ts`.
- Shared GET/PATCH functions in `frontend/src/api/client.ts`, using project/event segment encoding, existing CSRF handling and generation/proposal compare-and-swap values.
- `frontend/src/features/audio/AudioReviewPanel.tsx`: five time-aligned layers, capability ledger, filter/state/shot controls, bounded pagination, event list, player cue, immutable proposal disclosure, effective review form and explicit real-operator confirmation.
- Desktop panel between picture and shot table; a fifth mobile **Audio** mode hides unrelated workspace regions. The mobile form retains clearance above the fixed action bar.
- Read failures clear the prior page before showing the error. Unfiltered shot changes do not trigger full audio re-verification. Requests are event-driven; there is no timer polling.
- Real event geometry is separate from a clamped, at least 44 px hit target, including events at the media tail.
- `needs_work` drafts read back their stored overrides without presenting them as accepted effective content. Unknown capability and empty/filter states never mean silence.
- Saving an audio review refreshes workspace readiness but never calls Finalize or export. `audio_commit_failed` and generation/proposal conflicts disable save until refresh.

## Functional browser evidence

Target flow: `/projects/audio-review-ui-final` → select/cue VO → edit and confirm operator review → save → report remains stale and no customer files exist. Conflict flow: prepare a browser draft → a second writer changes the generation → stale save is rejected → explicit Refresh reads the second writer's draft.

Using the Codex in-app browser against a temporary synthetic workspace and real built-in backend/Vite proxy:

- VO row click moved the review video from 0 to **0.45 s**, without auto-play.
- On mobile, an explicit **Cue** moved to Video mode, restored keyboard focus to the video, kept playback paused and announced the 00:00.45 position. Selecting a row still stays in Audio mode for uninterrupted review.
- Reviewed transcript became `Evidence stays attached to every frame.`; browser displayed the explicit Finalize/no-client-files message.
- A concurrent change caused `Audio changed; refresh before continuing`; save became disabled. Refresh read `Concurrent operator change` and its server-side notes rather than retaining the browser draft.
- Backend readback: VO `reviewed`, concurrent onset `needs_work`, report generation invalid, `.pdf/.xlsx` count zero.
- The empty project displayed `No audio timeline yet`, `audio timeline unavailable; this is not evidence of silence`, and the exact existing CLI recovery command. If media duration is unavailable but events exist, the app hides the misleading scale while preserving the event list and source timecodes.
- Desktop CSS viewport 1440×900 and mobile CSS viewport approximately 389×843 had no page-level horizontal overflow and no console warning/error during normal flows. The desktop and form captures are 965×603 and 261×565; the final mobile overview is 522×1130 at a different DPR. All map to the stated CSS viewports; capture pixels are not a layout claim.
- Mobile capability status is a readable 2+2+1 grid at 11 px. The media-tail event hit target measured about 44 px and remained within its track. The mobile form's save button ended above, not behind, the fixed 64.7 px action bar.
- A deliberate backend shutdown followed by a filter change produced an error with **zero** old event rows and no lanes, proving a failed new query cannot masquerade as its prior filter result. The induced proxy error is excluded from the normal-flow console claim.

Temporary visual evidence lives outside the repository:

- `/tmp/vew-audio-desktop-final.png`
- `/tmp/vew-audio-mobile-final.png`
- `/tmp/vew-audio-mobile-form.png`

## Visual comparison

The accepted and current screenshots were both inspected at native size.

1. App shell, narrow left rail and top project receipt remain unchanged.
2. Brick-red remains the primary selection/action color; blue/amber are limited to semantic audio layers/status.
3. Timecode, source IDs and measured values continue the established monospace language.
4. Audio is a rail/list/inspector hierarchy, not a new pile of cards.
5. Thin rules, square controls and compact labels match the shot table and existing inspector.
6. Mobile retains the existing top mode switch and bottom action bar; Audio is an equal view, not a modal dead end.
7. Original/effective text, capability uncertainty and operator confirmation are more explicit than ordinary UI copy, intentionally preserving the evidence boundary.

Intentional deviations: the mobile navigation grows from four to five equal modes; semantic audio categories add restrained blue/amber/gray bands; capability status wraps to three rows on mobile. These support the requested function and remain within the accepted visual system.

## Independent review and fixes

- Native code review initially found stale-page leakage after failed filters (HIGH), an unnecessary full query on every unfiltered shot change, and a clipped tail-event hit target. All were fixed and the final delta was approved with zero remaining findings.
- Independent visual review found the mobile capability strip clipped/undersized and the 389 px event status lacked a stable visual anchor. Capability status now wraps at 11 px; lane labels are 11 px; status is separated and weighted. Final static delta review found no new visual regression.
- DeepSeek Harness session `session-a6967841-d0d5-419c-9c7b-61c119d615a9` completed two read-only turns (`completionReason=completed`, final live state false) with `deepseek-v4-pro`, high effort, `code` + `proposal-only`. It found no HIGH defect. Adopted: selection preservation, durable no-op/save feedback, shot-scoped pagination, unknown-duration honesty, removal of the redundant JS width floor, count-scope wording and explicit mobile Cue handoff. Deferred: review-payload provenance semantics, caching, a query/state library and any new dependency. Final delta review found no HIGH/MEDIUM correctness issue; two LOW suggestions—bounded empty-page wording and focus restoration after mobile Cue—were applied without expanding architecture.
- DeepSeek could not build or run the copied snapshot because deliberate scope minimization omitted node_modules and unrelated test fixtures; this is recorded as a reviewer limitation, not a passing test. Its screenshot dimension check corrected the DPR wording above. Original `RESULT.md`/`OPINION.md` remain unaltered in the local audit record.
- Local audit directory: `tmp/.t15-deepseek-audit-20260901`; the source/screenshots were compacted into one 556 KB `audit-snapshot.tar.gz` (`sha256 de61183ab0e6498bc2c023d29525770fdb30404e5b6f44e47848524d719103fb`). The final local delta and its digest are retained separately and dry-run cleanly.

## Verification

- `npm run build`: PASS; 1,701 modules, current generated bundle about 307 KB JS / 42 KB CSS before gzip.
- `npm run test:integration`: PASS; proxy rewrites Origin/Host and withholds sibling-origin CORS.
- Frontend contract excluding the intentionally stale visual receipt: **18 passed, 1 deselected, 45 subtests passed**.
- T13 API boundary: **29 passed, 22 subtests passed**.
- Full repository suite: **402 passed, 328 subtests passed; 1 failure**. The only failure is the deliberately stale prior UI candidate receipt (`113 != 165` files); it is reserved for current T22 screenshot binding rather than rewritten to manufacture a pass.
- Native final code review reran build and the scoped frontend contract successfully.
- Generated `frontend/dist` was synchronized into `src/video_analysis_mvp/frontend_dist`; only the two currently referenced hashed assets remain.

Overall verification is partial. The old `ui-acceptance-receipt.json` remains intentionally stale until T22 captures and binds current screenshots. Committed browser automation, large real-project interaction counts and accessibility tooling belong to T19/T22. Real ASR/music/SFX semantic accuracy, professional templates and full-product release remain later Goal work. Git has no initial commit, so a real T15 worktree could not be created without unauthorized commit history; this stage used one sequential writer and independent read-only reviewers instead of pretending isolation existed.

Two deliberate UI semantics remain: the list writes accepted displayed values as explicit review overrides (a future payload-diff change belongs to the T13 schema owner), and dense time ranges use an intentional two-line 11 px monospace treatment rather than hiding or truncating exact in/out points.
