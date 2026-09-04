# Mature candidate independent cold review

Status: **APPROVE WITH RESIDUALS**. Final repaired-candidate review found no
P0, P1 or P2 issue.

## Initial verdict

The independent T22 reviewer returned **REJECT**. It independently recomputed
the then-current 202-file product digest and found one P1: the actively linked
legacy shot/vision forms could mutate evidence and immediately call report
generation without the separately named Finalize action or the primary
workspace's optimistic edit contract. It also found stale public status text
and the missing combined candidate receipt.

## Repair

- Legacy shot and vision evidence is now read-only; retired POST endpoints
  return `410` with a primary-workspace migration instruction and do not read a
  body or change project evidence.
- Legacy explicit Finalize now calls the same readiness-checked
  `regenerate_project_report()` service as the primary workspace.
- All legacy shots remain visible; the former 24-shot inspector cap and dead
  mutation helpers were removed.
- The ads keeper sidecar was deliberately retained for compatibility, then
  constrained server-side to ads projects and the three existing branch
  values. Non-ads and invalid branch writes fail without creating or replacing
  the receipt.
- Public status documents now distinguish implemented local behavior from
  optional semantic providers, public release and cross-platform evidence.

## Over-correction and negative-optimization check

An independent code reviewer first found two P2 side effects in the repair: a
non-ads keeper write gap and a dead inspector anchor. After the minimal fixes,
its delta verdict was **CLEAN** with no P0-P3 findings. It explicitly confirmed
that the `410` retirement did not remove the supported primary UI, CLI vision,
legacy evidence viewing, ads keeper sidecar, or explicit legacy Finalize. No
replacement mutation API, feature-flag framework, duplicate export path, or
new runtime dependency was added.

## Current verification supplied for final re-review

- product digest: `fae3ad0b3d77b304724e2592fe4ec1109e868488f24d4b7b29b30dfe7ded9748`
  across 202 product files;
- full Python: 515 passed, 28 optional-runtime skips, 393 subtests;
- legacy/keeper focused: 57 passed, 29 subtests;
- frontend build, asset parity, same-origin integration and browser E2E passed;
- real client render: 69/69 plus one 4.403-second explicit XLSX+PDF transaction;
- clean candidate/wheel/migration/served-asset install passed with cache-backed
  exact-lock npm acquisition;
- synthetic video 6/6 functional, 5/5 accuracy-gated; generated PCM 5/5;
- Ruff, Bandit, pip-audit, cache-backed npm audit and artifact cleanup passed.

## Final verdict

The independent final reviewer returned **PASS / APPROVE WITH RESIDUALS**. It
independently recomputed 207 total candidate paths and 202 product files,
matched product digest `fae3ad0b3d77b304724e2592fe4ec1109e868488f24d4b7b29b30dfe7ded9748`,
verified all four external evidence-file size/hash bindings, collected 543
Python tests (matching 515 pass + 28 optional skips), and found no P0/P1/P2.

Accepted P3 residuals:

- screenshots were captured on 2026-09-01, rebound on 2026-09-04, and have no
  retained trace; the repaired visible frontend wording is covered by the
  later production build, asset parity and E2E rather than a new screenshot;
- downloader redirects/later DNS remain outside an SSRF sandbox, and optional
  provider/BridgeDeck reads have socket/byte bounds but no separate monotonic
  total-response deadline.

The reviewer did not replay the high-load browser/media/render gates; it
validated their current candidate/evidence bindings and independently reran the
bounded legacy/report/persistence tests, TypeScript check, Ruff, metadata and
hash checks. Public/cross-platform/provider/adoption states remain unverified.

## Publication metadata delta

After the local-candidate verdict, the verified canonical GitHub repository was
reconnected and publication was explicitly authorized. README badges/status,
package project URLs, citation metadata, `llms.txt`, security reporting,
launch/growth wording, changelog and v0.2.0 release notes were updated without a
runtime-code change. The resulting 204-file product digest is
`b80792c243a24ad09fa0212f31c4ced28d500b2c7816e13d983bfd870ac1c765`.
Its independent staged review found one P2 contradiction in the deferred list:
canonical URLs, badges and GitHub release were still labelled deferred after
being enabled. The minimal wording repair retained only genuinely unavailable
package-registry, DOI, named-author, funding and separate-site metadata. Final
staged verification matched 209 index paths, 204 product files, the digest
above and all four evidence bindings, with no remaining P0-P3. The publication
metadata delta is approved for commit; GitHub Actions must still pass on the
exact pushed revision before merge and release.
