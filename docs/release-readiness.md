# v0.2.1 release readiness

Status: **local candidate gates and independent final review passed; exact PR
and tag CI remain required**. This file records evidence for the v0.2.1 maintenance
candidate. It does not prove publication, provider accuracy, adoption or a
stable cross-platform release.

## Candidate scope

v0.2.1 contains the post-v0.2.0 release-governance fixes, reliable npm audit
result classification, separately verified GitHub Actions v7 upgrades, release
metadata consistency checks and safer Dependabot grouping. It does not change
the analysis, human-review, Finalize or client-export product workflow.

The frozen candidate contains **210 product files** with SHA-256
`3f7bd9ad96e941afb78aafb48961f5acb031234d2bdb6863f79c2472c1a56681`.
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

| Gate | v0.2.1 candidate result |
| --- | --- |
| Full Python suite | 520 passed, 29 optional-runtime skips, 393 subtests in 47.32 s; exit 0 |
| Release metadata | pyproject, package, lock, Python, MCP client, citation, README, changelog, release note and mature status agree on 0.2.1 |
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

- PR #7 repaired release-receipt scope and immutable-tag verification.
- PR #8 classified npm advisory endpoint outages without hiding confirmed
  high/critical findings or unknown failures.
- Dependabot PRs #2, #3, #4 and #5 were each rebased onto the cumulative current
  main branch, passed the complete hosted Linux/macOS, Python 3.11/3.12,
  frontend, security and real-export matrix, and were merged separately.
- Dependabot PR #6 was closed without merge. It combined unrelated frontend
  major upgrades while leaving the packaged build mirror and UI evidence stale.
  Dependabot now groups frontend minor/patch updates only; future major updates
  must be reviewed independently.
- The release-candidate base before version freeze is
  `2e3fd3bfe39e9b82f51f41923b2f23090172ff07`.

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

1. Freeze and independently recompute the exact product digest and four
   evidence-file bindings.
2. Pass the complete GitHub PR matrix on the exact candidate SHA.
3. Merge without content changes, create immutable tag `v0.2.1`, and pass the
   tag-only release receipt gate plus the normal tag CI matrix.
4. Create and read back the GitHub pre-release and attached candidate receipt.
