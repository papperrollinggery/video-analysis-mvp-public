# Product maturity requirements

## Decision

Video Evidence Workbench should become the local review-and-provenance layer for short-video analysis, not another generic “chat with video” interface. Its first useful job is complete only when one operator can import a real local video, inspect every detected shot, correct and explicitly approve evidence, resolve the readiness gate, and export a digest-bound package without editing JSON.

The pre-refactor baseline was **2.1 / 5**. The current evidence-based score is
**3.8 / 5 (local candidate)** after closing the primary review/finalization job,
adding persistent task recovery, explicit client exports, migration, and a
six-case synthetic benchmark. The current digest and screenshots are bound in
the [UI acceptance receipt](screenshots/ui-acceptance-receipt.json) and summarized
in [release readiness](release-readiness.md). This is not a public or
cross-platform release claim. Corpus workflows, remote platform evidence,
fade/dissolve accuracy, and verified provider interoperability remain open.
These scores are an internal rubric, not an external benchmark result.

## Requirements analysis

### Primary user and job

The primary user is a video-AI engineer or media researcher working with short local videos. They need a traceable dataset for review, evaluation, or downstream research—not a fluent answer whose supporting frames cannot be inspected.

The product must answer five questions:

1. What source bytes were analyzed?
2. Where does each observation occur in the video?
3. Which fields were measured, model-generated, or asserted by a person?
4. What still blocks a professional evidence package?
5. Can another tool consume the result without depending on this UI?

### Mature-product benchmark

The references below are capability benchmarks, not endorsements or claims of feature parity.

| Product | Mature behavior demonstrated by official documentation | Requirement for this project |
| --- | --- | --- |
| [FiftyOne](https://docs.voxel51.com/user_guide/basics.html) | persistent datasets, filtering, saved views, and visual inspection | keep project evidence queryable; later add a corpus index without hiding file-level provenance |
| [FiftyOne annotation](https://docs.voxel51.com/user_guide/annotation.html) | in-app annotation, autosave, and video-aware review | primary UI must own the entire review loop and expose saved state |
| [CVAT](https://docs.cvat.ai/docs/manual/advanced/annotation-with-polygons/track-mode-with-polygons/) | timeline-aware annotation and interpolation | preserve exact time boundaries and make boundary approval an explicit human assertion |
| [Label Studio](https://labelstud.io/templates/video_timeline_segmentation) | video timeline segmentation as a first-class task | every detected shot must be reachable, including projects with more than 24 shots |
| [PySceneDetect](https://www.scenedetect.com/docs/latest/) | documented detector configuration, CLI, and scene statistics | publish reproducible detector settings and a fixture benchmark rather than claiming universal accuracy |
| [Twelve Labs](https://docs.twelvelabs.io/docs/guides/search) | asynchronous indexing status and exact start/end search segments | long analysis must expose durable stage/progress/failure state before corpus search is attempted |

OpenAI surfaces are companion workflows, not embedded capabilities. [Deep research](https://help.openai.com/en/articles/10500283-deep-research-in-chatgpt) can synthesize cited web sources; [apps in ChatGPT](https://help.openai.com/en/articles/11487775-connectors-in) can provide explicitly connected context. This workbench prepares local evidence for a separate Codex or ChatGPT task and never implies that an upload, task, visualization, or deep-research run happened automatically.

## Maturity scorecard

| Dimension | Before | Current | 4/5 release-candidate gate |
| --- | ---: | ---: | --- |
| Evidence provenance | 4.0 | 4.4 | current media, visual, review, readiness, invalidation, and report receipts all verify after mutation tests |
| Single-video extraction/export | 3.5 | 3.8 | real short-video happy path passes from clean install on each declared platform |
| Primary review UI | 1.5 | 4.0 | all shots editable; save-next; explicit boundary receipt; one finalization action; browser happy path |
| Installation and recovery | 2.0 | 4.0 | clean isolated install, durable run ID, stage timing, cooperative cancel, interrupted detection, receipt-verified retry |
| Evaluation | 1.5 | 3.2 | six generated CC0 cases report boundary, evidence, readiness, environment, and stage timing; expand to 20 real/redistributable cases |
| Corpus and interoperability | 0.5 | 0.5 | stable import/export adapter contract and a small local corpus index |
| Accessibility | 2.5 | 3.5 | keyboard-only review/finalize path, visible focus, live status, no blocking desktop/mobile overflow |

The score is not averaged into a claim of accuracy. A release candidate requires every P0 gate below, even if the arithmetic mean is high.

## P0: close the first useful job

1. The production React workspace lists and can edit every shot; no 24-shot cap is allowed.
2. Each save requires an optimistic edit digest, records `annotation_source=human`, and leaves reports fail-closed until explicit finalization.
3. A low-confidence cut can become structurally ready only after a separate operator assertion bound to the current visual-generation receipt. Detector confidence must remain unchanged.
4. “Save & next unresolved” advances across the complete project. One “Finalize package” action regenerates and commits the package after reviews are complete.
5. Blocked, ready, stale, conflict, save failure, and finalization failure states are visible and recoverable.
6. A browser acceptance run must prove `blocked → all-shot review → finalize → ready → professional artifact opens`; mutation after finalization must block again, and restoring byte-identical reviewed content must remain blocked until a second explicit finalization.

## P1: credible public release

- Keep persistent run IDs, per-stage state/timing, failure detail, cooperative cancellation, receipt-verified retry, per-project leases, run ownership claims, and final generation bindings covered by unit, HTTP, browser, and install tests.
- Keep the isolated wheel-install gate and installed migration smoke in CI rather than reusing maintainer dependencies.
- Keep the six generated fixture categories in CI and expand to 20 real or redistributable cases only after licensing, expected boundaries, and thresholds are reviewed. Do not turn the current fade/dissolve observation into an accuracy claim.
- Add versioned import/export adapters before promising CVAT, Label Studio, notebook, or corpus interoperability.
- Verify optional providers against live official APIs with explicit cost/privacy receipts; do not require them for the deterministic core.

## Done and promise boundary

The core workflow may be described as “usable” only when all P0 acceptance evidence is current. It may not be described as a mature cross-platform product until the P1 installation, run-lifecycle, and benchmark gates pass. A `ready` receipt certifies structural evidence completeness only; it does not certify factual truth, copyright, authenticity, or fitness for a consequential decision.

The 2,000-star objective remains a post-launch adoption scenario. Product quality can improve the probability of adoption, but no implementation can guarantee stars.
