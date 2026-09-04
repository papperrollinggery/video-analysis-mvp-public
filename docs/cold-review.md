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
`9c8e8cbce9a08d6f8b576325587812e495e9c929b92c8cd56e971f7ad9713b97`.
Its independent staged review found one P2 contradiction in the deferred list:
canonical URLs, badges and GitHub release were still labelled deferred after
being enabled. The minimal wording repair retained only genuinely unavailable
package-registry, DOI, named-author, funding and separate-site metadata. Final
staged verification matched 209 index paths, 204 product files, the digest
above and all four evidence bindings, with no remaining P0-P3. The publication
metadata delta is approved for commit; GitHub Actions must still pass on the
exact pushed revision before merge and release.

The first remote push exposed duplicate `push` and `pull_request` CI matrices.
The release delta limits branch pushes to `main`, leaving one PR matrix and one
post-merge main matrix. Its focused independent review was CLEAN: YAML parsed,
all pull-request synchronization events remain covered, and merge/main pushes
still run CI. The superseded duplicate runs were sent cancellation requests;
the exact new PR checks remain mandatory before merge.

The first exact-SHA PR run then found a real high-severity minimum-dependency
failure: CI pinned `yt-dlp==2026.6.9`, which is affected by
`CVE-2026-55404` / `GHSA-6v4j-43gg-vj32`; the separately locked local version
was already `2026.8.19`. The project minimum and minimum-dependency job now use
the advisory's first patched version, `2026.7.4`, while the lock remains
`2026.8.19`. Local `uv lock --check` and pip-audit pass; the replacement remote
checks and an independent security-delta review remain required before merge.

The remaining remote failures were traced independently rather than grouped as
one flaky run. The frontend job's npm 10 quick-audit endpoint rejected the npm
11 lock tree after install/build passed; only that job now pins npm 11.12.1.
The macOS suites exposed `HTTPServer.server_bind()` reverse DNS on loopback; a
TDD regression first failed, then the minimal server subclass bypassed reverse
DNS while retaining IPv4, localhost and supported IPv6 `::1` bindings. The
real-render job had a Bash quoting error before runtime execution; its
two-step browser path assignment passes `bash -n` and resolves the installed
Playwright browser locally. Independent review found no P0-P3 in these fixes;
the next exact-SHA GitHub run remains the publication gate.

Because the real-render failure occurred inside Playwright after other real PDF
tests passed, the driver now emits only a bounded stage, fixed error code and
sanitized error class. It never emits the raw message or path. A Node test
verifies specific errno classification and close-stage preservation; Python
accepts only a complete final marker from fixed stage/code sets and rejects
path-embedded, oversized or trailing forgeries. Independent review found no
P0-P3. This diagnostic does not change successful PDF bytes or receipts and is
used only to make the next remote failure actionable.

That diagnostic reported `launch-browser / browser-launch-failed` only for the
later service transaction, while earlier direct real PDF tests passed. The
renderer previously placed Chromium's HOME/TMPDIR inside the deeply nested
output staging tree. A TDD boundary test first failed, then the renderer moved
only browser runtime state to a separate short, mode-0700 system temporary
directory. HTML/raw PDF/config and final publication remain output-adjacent and
same-volume; both temporary contexts clean up on success, ToolError and
cancellation. Independent review found no P0-P3 and no over-correction.
