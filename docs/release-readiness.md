# Mature candidate release readiness

Status: all applicable local runtime gates passed and the runtime candidate
ended **APPROVE WITH RESIDUALS** with no P0/P1/P2 finding. Publication metadata
and CI-trigger deltas passed independent review. The remote minimum-dependency
security correction is awaiting its replacement PR checks.
This document records the local pre-publication verification. GitHub checks,
the merged revision and release page are the authority for remote publication;
this receipt does not prove a separate deployment or package-registry upload.

## Candidate boundary

The current product file set contains **205** non-ignored files and has digest:

```text
sha256:9c8e8cbce9a08d6f8b576325587812e495e9c929b92c8cd56e971f7ad9713b97
```

The digest hashes `mode\0path\0size\0bytes\0` in sorted Git-candidate order.
It includes source, tests, public docs, generated frontend assets and all six
current screenshots. To avoid circular hashes, it excludes only:

- `docs/screenshots/ui-acceptance-receipt.json`
- `docs/evidence/mature-candidate-receipt.json`
- `docs/release-readiness.md`
- `docs/cold-review.md`
- `progress.txt`

The mature-candidate receipt separately hashes these evidence files.

## Local gate results

| Gate | Current result |
| --- | --- |
| Full Python suite | 518 passed, 29 optional-runtime skips, 393 subtests in 52.76 s; exit 0 |
| Focused security/Codex/BridgeDeck/migration/API regression | 110 passed, 84 subtests in 11.77 s; exit 0 |
| Ruff 0.15.22 | `src` and `tests` clean |
| Bandit 1.9.4 | no medium/high findings; the three B314 suppressions are limited to bounded self-generated XLSX post-validation |
| Python dependency audit | pip-audit 2.10.1 reported no known vulnerability in the installed `.venv` path; unpublished editable package skipped |
| Frontend dependency audit | `browserslist` updated 4.28.6 → 4.28.8 after two high-severity advisories; current cache-backed `npm audit --offline --audit-level=high` reports 0 vulnerabilities |
| Frontend | same-origin integration, TypeScript/Vite build and export-center E2E passed |
| Browser E2E | generate 2, cancel 1, save 1, delete 1, serialized status reads, 3 px focus, no unexpected console problems |
| Real client render | 71/71 passed in 78.853 s with openpyxl, Playwright/Chrome, CJK font, pypdf and LibreOffice; a current standalone XLSX+PDF transaction completed in 4.376 s with output hashes recorded |
| Install/migration | current 210-file candidate, fresh temporary venv, candidate wheel, installed migrate dry-run/apply, frontend-byte and served-byte parity passed; exact-lock npm install was cache-backed and reported 0 vulnerabilities |
| Pipeline smoke | package, persistent demo, synchronous/asynchronous API and full review/Finalize lifecycle passed |
| Product benchmark | six of six functional and five of five accuracy-gated video cases passed in 5.608 s with 50,921,472-byte Python-process peak RSS; fade/dissolve remains observational |
| Audio benchmark | five of five generated PCM cases passed in 0.070 s; ASR and semantic audio identity remain `not_run` |
| UI capture | six current PNGs inspected; 1440×900, 900×1000 and 390×844 had status 200, no console warning/error, no horizontal overflow and no undersized measured controls |
| UI interaction | review/source/Codex/mobile-Codex focus restoration, current XLSX export, direct run reload and workspace link verified |
| Artifact cleanup | 210 candidate paths including evidence files; one bounded diagnostic file; no generated media/model/customer document in the release candidate |
| Docs and metadata | 77 local Markdown file links resolve; 14 JSON, one TOML and six YAML/CFF files parse; generated-Markdown safety is covered by the full suite |

The current UI receipt independently re-hashes the 205-file product candidate,
frontend source, packaged frontend files and all screenshots.

The six screenshots were captured on 2026-09-01 and rebound to the repaired
candidate on 2026-09-04; no Playwright trace was retained. The later visible
frontend delta was limited to legacy-viewer wording and its production build,
asset parity and E2E were rerun. Recapture screenshots if any further visible
UI changes are made before an authorized public release.

The first final install attempt reached `npm ci` but the public registry request
produced no output for almost three minutes and was interrupted. The second run
used the exact lockfile and existing npm cache (`npm_config_offline=true`) and
passed from candidate reconstruction through installed HTTP byte parity. This
proves a clean local wheel/venv and reproducible cached frontend install, not an
uncached remote first install. The final 209-file publication candidate repeated
that cache-backed clean-install path successfully after canonical metadata and
release notes were added.

## Canonical GitHub state

- repository: `https://github.com/papperrollinggery/video-analysis-mvp-public`;
- remote base before this upgrade: `8419b3a1d1ff` on `main`;
- visibility: public;
- private vulnerability reporting: enabled and read back through GitHub;
- repository description and 11 accurate discovery topics: updated and read back;
- CI trigger: pull requests run once; branch pushes run only on `main`, avoiding
  duplicate feature-branch push and PR matrices;
- existing pull requests, issues, releases and Actions runs before this upgrade:
  none.

The branch, PR, checks, merge commit and tag are recorded by GitHub after this
local snapshot. They must be inspected before the release is described as
published.

The first exact-SHA PR run supplied useful red evidence rather than release
proof. It found four independent CI integration gaps: a vulnerable minimum
`yt-dlp` pin, npm 10 audit incompatibility with the npm 11 lock tree, macOS
loopback server startup blocked by reverse DNS, and a Bash quoting error while
binding the Playwright browser path. The current candidate raises the minimum
to the first patched `yt-dlp` release, pins npm only in the affected audit job,
uses an IPv4/IPv6 loopback server that avoids reverse DNS, and uses a
syntax-checked two-step browser-path assignment. Local targeted tests and
independent delta review pass; replacement GitHub checks remain mandatory.

The bounded renderer diagnostic on the next SHA localized the remaining real
export failure to browser launch after earlier direct PDF renders succeeded.
The renderer now keeps HTML/raw PDF/config in the original output-adjacent
same-volume staging while giving Chromium a separate short, mode-0700 system
temporary HOME/TMPDIR that is removed on every exit path. The change has no
effect on final atomic publication and passed independent review without a
P0-P3 finding; the next remote real-render job remains authoritative.

## Security disposition

Codex Security standard scan
`bfb9a48c-8668-4a1d-a7ca-cfc4f69bb1d1` inspected the pre-remediation
snapshot and produced three medium plus two low findings. Current fixes and
fresh regression evidence close:

- unbounded `ffprobe` execution;
- signed URL/password values accepted directly in CLI argv;
- missing FastAPI `Sec-Fetch-Site: cross-site` guard;
- value-file permission/read TOCTOU;
- playlist/multi-video disk amplification;
- verified ffmpeg/ffprobe path drift.

Independent delta review ended CLEAN. Two explicit residuals remain:

1. **Medium, accepted boundary:** yt-dlp controls redirects and later DNS
   resolution. URL ingest requires an owner-only value file plus
   `--acknowledge-url-risk`, rejects playlists, and is documented as
   trusted-operator-only—not an SSRF sandbox. Use a separately restricted
   downloader for untrusted URLs.
2. **Low, deferred:** provider/BridgeDeck reads use bounded bytes and socket
   timeouts but not a separate monotonic total-response deadline. It requires an
   explicitly configured provider and does not affect the deterministic core.

TAC access could not be verified because the Codex Security Access connector
was not logged in (`USER_NOT_LOGGED_IN`). The scan still completed locally and
its report exists, but this is not a TAC-grant claim.

## Product completion audit

- Structured VO/music/SFX/silence/mixed timeline, machine proposal, operator
  review, effective values and shot associations are implemented and gated.
- Review saves never auto-Finalize or auto-export.
- The legacy evidence viewer retains all-shot inspection, the ads keeper
  sidecar and explicit readiness-checked Finalize. Retired shot/vision POST
  endpoints return `410`; they cannot silently mutate or regenerate reports.
- Professional XLSX/PDF share one validated dataset and fixed template; output
  is created only by explicit request, one current package is replaced, and
  history is created only by Save version.
- Current Codex analysis uses the versioned prepare/apply contract, returns to
  human review and does not claim provider/human/model verification.
- BridgeDeck remains an optional explicit numeric-loopback/account/model
  adapter with no environment credential forwarding; live upstream inference
  remains unverified.
- Legacy migration is read-only by default, explicit on apply, rollback-safe,
  crash-recoverable and preserves saved versions.
- The open-source README, schemas, FAQ, launch plan, `llms.txt`, citation,
  governance files and measured 2,000-star scenario are current.
- Over-correction review rejected auto migration/export/provider fallback,
  heavyweight default runtimes, duplicate transaction systems, fake canonical
  metadata, keyword-stuffed AEO files and unverified platform claims. A later
  compatibility repair kept valid legacy viewing/ads behavior while removing
  only the proven bypass; its independent code-review delta ended CLEAN.

## Unverified external states

- exact merged commit/tag and public release state, which are recorded externally by GitHub;
- remote GitHub Actions execution for a specific pushed revision;
- Windows and every Linux distribution/codec;
- live OpenAI, MiniMax and BridgeDeck authentication, cost, retention and model
  accuracy;
- real ASR accuracy and semantic music/SFX/VO identity accuracy;
- public adoption, qualified-user funnel and 2,000 stars.

The product is eligible only for a **local mature-candidate** verdict. It is not
yet a public or cross-platform stable release.
