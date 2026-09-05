# v0.3.0 release readiness

This is the local pre-publication candidate record. Exact main/tag CI and the
published release API are separate external evidence; no prior-version verdict
or release state is inherited.

The candidate adds a globally installed Codex Skill with an isolated wheel
runtime, bounded multi-frame batches, checkpoint recovery, complete creative
Markdown and native video-only input handling. It preserves model/human review
boundaries and v1 request/receipt compatibility.

## Current evidence

- Workflow and original-media acceptance are recorded in
  `docs/evidence/codex-skill-optimization-2026-09-05.json` as the first-stage
  optimization snapshot.
- The v0.3.0 global installation, dependency audit and final test results are
  recorded in `docs/evidence/mature-candidate-receipt.json`.
- New-process discovery found exactly one enabled user-scope Skill outside this
  repository. Runtime probes loaded version 0.3.0 from its wheel-installed
  site-packages.
- Cross-project invocation retained a relative workspace under the caller's
  temporary directory despite a PYTHONPATH decoy. The original video-only clip
  completed two inspected batches and stayed applied after prepare.
- The installer checks its own managed pip before installing dependencies; older
  pip is upgraded inside that runtime to the tested 26.2.1 baseline.
- Existing screenshots retain their original capture dates. The frontend source
  and served asset hashes are unchanged; production build and browser tests were
  rerun. These images are not evidence of video-analysis semantic accuracy.

The product digest excludes only the existing five execution-evidence files:
`docs/cold-review.md`, `docs/evidence/mature-candidate-receipt.json`,
`docs/release-readiness.md`, `docs/screenshots/ui-acceptance-receipt.json`, and
`progress.txt`. The mature receipt separately hashes the other four evidence
files; it cannot hash itself. Run `scripts/verify-candidate-receipt.sh v0.3.0`
against the tagged tree to verify the frozen bindings.

## Publication sequence

1. Pass final local checks and independent frozen-candidate review.
2. Commit and push the exact candidate; require its GitHub CI matrix to pass.
3. Create and push v0.3.0 at that commit; require tagged-tree receipt and matrix
   checks to pass.
4. Create a draft prerelease, attach the wheel, standalone Skill ZIP, verification
   receipt and checksums, then publish and read back the release and assets.

## Retained boundaries

External provider accuracy, general ASR and semantic sound identity, exact VFR
PTS, Windows and automatic SIGKILL/power-loss recovery remain unverified. Model
submissions are not human review. Package-registry publication, a hosted service
and external announcements are not part of this release. The independent Codex
CLI model startup check was blocked by that host version; current-host execution
and fresh Skill discovery were verified separately.
