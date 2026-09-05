# Changelog

All notable public changes are documented here. The project follows Semantic
Versioning while its APIs and on-disk schemas remain pre-1.0.

## [0.3.0] - 2026-09-05

### Added

- globally installable `video-evidence-workbench` Codex Skill with a wheel-bound
  isolated runtime, cross-project invocation, upgrade backup and rollback;
- bounded `codex next` / `codex submit` batches, checkpoints and retry support
  for up to 1024 eligible shots;
- ordered supporting frames, adjacent-shot context and explicit review targets
  for repeated fields and single-frame evidence;
- native video-only input handling without synthesizing an audio track.

### Fixed

- preserve a valid applied request on repeated prepare and keep v1 receipts
  compatible; restore prior state after caught annotation commit failures;
- retain model bindings alongside protected or subsequently reviewed human rows;
- preserve single detected cuts and safely sample short CFR clips, including
  single-frame and two-frame videos;
- cover every selected shot in remake and reverse-engineering Markdown while
  consolidating shared prompt controls;
- remove cross-thread environment-patch interference and an invalid-response
  fixture's premature-stdin-close race from the regression suite.

### Verification boundary

- model proposals still require review; schema validation does not establish
  semantic accuracy or verified model identity;
- external providers, general ASR accuracy, exact VFR PTS, Windows and automatic
  recovery after abrupt process termination remain outside the tested scope;
- package-registry publication and a hosted service are not part of this release.

## [0.2.2] - 2026-09-04

### Fixed

- corrected release materials that used “immutable tag/tree” as shorthand
  before platform state had been checked;
- now describes CI as binding a named tagged Git tree and treats the GitHub API
  as the authority for platform-level release immutability.

### Release integrity

- enabled GitHub release immutability before creating v0.2.2;
- records the time-dependent v0.2.1 state without inferring policy behavior:
  release immutability was disabled at creation, then the API reported
  `immutable: true` after policy enablement and a release-note update.

## [0.2.1] - 2026-09-04

### Changed

- moved frozen candidate-digest enforcement to tagged release trees so routine
  pull requests retain UI, screenshot and asset checks without inheriting a
  historical whole-tree digest;
- upgraded the maintained GitHub Actions runtime set to checkout, setup-python,
  setup-node and upload-artifact v7 after full hosted-runner verification;
- constrained grouped frontend dependency updates to minor and patch releases so
  unrelated major upgrades receive separate compatibility and visual review.

### Fixed

- completed the release receipt gate by binding the mature candidate schema,
  release status, product digest and separately hashed review evidence;
- distinguished confirmed npm high/critical findings from advisory-service
  outages while keeping malformed and unknown audit failures blocking.

### Release boundary

- this maintenance release does not expand live provider, Windows, ASR accuracy
  or semantic sound-identity claims from v0.2.0;
- the rejected grouped frontend-major update was not included because its built
  asset mirror and UI acceptance evidence were stale.

## [0.2.0] - 2026-09-04

### Added

- persistent, resumable local analysis runs with stage timing and cancellation;
- structured VO, music, SFX, silence and mixed-audio evidence with operator review;
- one review/finalization workflow across the React UI, CLI and local API;
- explicit professional XLSX/PDF generation with current and saved-version lifecycle;
- Codex prepare/apply guidance and optional BridgeDeck/OpenAI/MiniMax adapters;
- migration, synthetic benchmark, responsive browser checks and release receipts.

### Changed

- repositioned the project from a one-off video-analysis MVP to an auditable,
  local-first Video Evidence Workbench;
- made report finalization and client export separate explicit actions;
- replaced mutable legacy shot/vision forms with a read-only recovery viewer and
  clear migration responses;
- expanded documentation, citation, governance, security and GitHub discovery metadata.

### Security

- hardened loopback origin/CSRF checks, file and value-file handling, media-tool
  execution, URL-ingest limits, artifact confinement and spreadsheet safety;
- raised the minimum `yt-dlp` version to `2026.7.4`, which fixes
  `CVE-2026-55404` / `GHSA-6v4j-43gg-vj32`;
- enabled GitHub private vulnerability reporting.

### Known limitations

- Windows, live provider behavior, ASR accuracy and semantic sound-identity
  accuracy are not yet release claims;
- URL downloader redirects and later DNS resolution remain outside an SSRF sandbox;
- shot boundaries and model proposals require human review.

See the [v0.2.0 release notes](docs/releases/v0.2.0.md) for verification and
migration details.

## [0.1.0] - 2026-05-02

- Initial sanitized public prototype.

[0.2.0]: https://github.com/papperrollinggery/video-analysis-mvp-public/releases/tag/v0.2.0
[0.2.1]: https://github.com/papperrollinggery/video-analysis-mvp-public/releases/tag/v0.2.1
[0.2.2]: https://github.com/papperrollinggery/video-analysis-mvp-public/releases/tag/v0.2.2
[0.3.0]: https://github.com/papperrollinggery/video-analysis-mvp-public/releases/tag/v0.3.0
[0.1.0]: https://github.com/papperrollinggery/video-analysis-mvp-public/commit/8419b3a1d1ff09cd355679f5d89d97f7ff524f86
