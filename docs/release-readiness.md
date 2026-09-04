# v0.2.2 release readiness

Status: **local candidate gates and independent final review passed; exact PR
CI, tag CI and release readback remain required**. This file records evidence for the v0.2.2 integrity
candidate. It does not prove publication, provider accuracy, adoption or a
stable cross-platform release.

## Candidate scope

v0.2.2 retains the v0.2.1 release-governance fixes, reliable npm audit
result classification, separately verified GitHub Actions v7 upgrades, release
metadata consistency checks and safer Dependabot grouping. It does not change
the analysis, human-review, Finalize or client-export product workflow.

The frozen candidate contains **211 product files** with SHA-256
`f4bedd8e1a70c6df56525f573aeb116bc8dbf4ac27ce02f76311ac2bc338a90f`.
The same binding is recorded in both
`docs/screenshots/ui-acceptance-receipt.json` and
`docs/evidence/mature-candidate-receipt.json`. The product digest excludes only
the following self-referential evidence files, which are bound separately by
the mature receipt:

- `docs/screenshots/ui-acceptance-receipt.json`
- `docs/evidence/mature-candidate-receipt.json`
- `docs/release-readiness.md`
- `docs/cold-review.md`
- `progress.txt`

## Fresh local verification

| Gate | v0.2.2 candidate result |
| --- | --- |
| Full Python suite | 520 passed, 29 optional-runtime skips, 393 subtests in 48.18 s after the independent verdict was bound; exit 0 |
| Release metadata | version fields, release language and version-scoped current review are aligned on 0.2.2 |
| Python lint/source scan | Ruff 0.15.22 clean; Bandit 1.9.4 found no medium/high issue |
| Python dependency audit | pip-audit 2.10.1 reported no known vulnerability; unpublished editable package explicitly skipped |
| Frontend install/audit | clean `npm ci`; 78 packages audited; 0 vulnerabilities |
| Frontend contracts | same-origin integration passed; seven PDF diagnostic cases passed; npm audit classifier clean/high/critical/malformed/outage/unknown cases passed |
| Frontend build | TypeScript/Vite production build transformed 1,703 modules; packaged asset names and bytes remained unchanged |
| Browser E2E | explicit generate/cancel/save/delete lifecycle, 1440/900/390 responsive checks, 44 px targets and 3 px focus passed without unexpected console problems |
| Real XLSX/PDF/browser | 71 tests passed in 63.015 s with Playwright Chrome, Noto Sans CJK, pypdf and LibreOffice required |
| Workflow smokes | package, persistent demo, synchronous/persistent API, review/Finalize/mutation/refinalize lifecycle passed |
| Product benchmark | 6/6 functional and 5/5 accuracy-gated video cases passed in 5.646 s; one fade case remained observational; Python-process peak RSS 52,101,120 bytes |
| Audio benchmark | 5/5 generated PCM cases passed in 0.071 s; ASR and semantic identity accuracy remain `not_run` |
| Clean install | 215-file candidate copied; clean frontend build, packaged mirror, wheel, migration and installed HTTP asset parity passed |
| Artifact policy | candidate audit passed; no generated media, model output or client document entered the repository |

## GitHub maintenance disposition

- PR #7 repaired release-receipt scope and tagged-tree verification.
- PR #8 classified npm advisory endpoint outages without hiding confirmed
  high/critical findings or unknown failures.
- Dependabot PRs #2, #3, #4 and #5 were each rebased onto the cumulative current
  main branch, passed the complete hosted Linux/macOS, Python 3.11/3.12,
  frontend, security and real-export matrix, and were merged separately.
- Dependabot PR #6 was closed without merge. It combined unrelated frontend
  major upgrades while leaving the packaged build mirror and UI evidence stale.
  Dependabot now groups frontend minor/patch updates only; future major updates
  must be reviewed independently.
- v0.2.1 passed its candidate, main and tagged-tree CI and was published with a
  matching receipt. A later independent readback found that its platform-level
  immutability wording lacked a state check; the public release page now records
  creation with the setting disabled and a later API result of `immutable: true`
  after enablement and a note update.
- Repository release immutability is enabled. GitHub's
  release API, not this local file, decides whether v0.2.2 is platform-locked
  after publication.
- Post-freeze PRs #10-#14 were triaged and closed with exact asset-drift,
  visual-review, compiler-major or paired-peer-dependency requirements. None was
  merged into this candidate.
- The release-candidate base before version freeze is
  `834507cc1e92779d8231d3b2fc1019adee9f6193`.

## Security and truth boundaries

- Confirmed npm high/critical findings fail CI. Malformed and unknown audit
  failures also fail. A scoped registry advisory endpoint timeout/503 emits an
  `unavailable` warning and cannot be represented as a clean audit.
- URL ingest remains trusted-operator-only because downloader redirects and
  later DNS resolution are outside the local public-address precheck.
- Optional OpenAI, MiniMax and BridgeDeck calls retain bounded bytes/socket
  timeouts but do not have a separate monotonic total-response deadline.
- Live provider inference, Windows, ASR accuracy, semantic VO/music/SFX
  identity accuracy, copyright suitability, public adoption and 2,000 stars are
  outside this maintenance-release claim.

## Remaining publication gates

1. Pass the complete GitHub PR matrix on the exact candidate SHA.
2. Merge without content changes, create tag `v0.2.2`, and pass the
   tag-only release receipt gate plus the normal tag CI matrix.
3. Create the pre-release as a draft with its receipt, publish it, and read back
   GitHub's immutable flag, tag target and asset digest.
