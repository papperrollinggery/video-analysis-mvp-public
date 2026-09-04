# Changelog

All notable public changes are documented here. The project follows Semantic
Versioning while its APIs and on-disk schemas remain pre-1.0.

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
[0.1.0]: https://github.com/papperrollinggery/video-analysis-mvp-public/commit/8419b3a1d1ff09cd355679f5d89d97f7ff524f86
