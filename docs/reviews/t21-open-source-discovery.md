# T21 open-source presentation and discovery review

Status: completed pre-launch documentation candidate. No repository, package,
website or announcement was published.

## Result

- README now leads with one copyable local synthetic proof and an explicit
  expected blocked-readiness outcome. Own-video, UI, benchmark, Codex and client
  export are clearly subsequent steps rather than first-success requirements.
- The capability/support matrix separates base analysis, UI, XLSX, PDF, current
  Codex and provider adapters, including their optional dependencies and network
  boundaries.
- Historical screenshots are visibly labeled as stale relative to the current
  candidate. Their receipt will be refreshed once during final freeze rather
  than rewritten as self-approval during implementation.
- Architecture and strategy documentation now describe the implemented audio
  timeline, review, Finalize, client-export and migration lifecycle rather than
  an obsolete planned state.
- `llms.txt` maps the current schemas, migration, Codex contract, explicit
  export and trust boundaries. It remains a concise retrieval aid, not a claimed
  standard or proof of ingestion by a model.
- `docs/open-source-launch.md` defines exact release gates, future GitHub
  settings, a query-to-answer map, privacy-safe activation funnel and the
  2,000-star scenario boundary.
- GitHub issue forms now cover current Codex, BridgeDeck, audio, client export
  and migration surfaces. Dependabot covers Actions, root Python and frontend
  npm dependencies with bounded grouped updates.
- Citation keywords were expanded without inventing authors, organizations,
  repository URLs, release dates or DOI metadata.

## Mature-project research

Official repository surfaces read on 2026-09-01:

- [PySceneDetect](https://github.com/Breakthrough/PySceneDetect): direct job,
  split quick starts and benchmark methodology.
- [Label Studio](https://github.com/HumanSignal/label-studio): visible output,
  multiple clearly separated installation paths and community files.
- [FiftyOne](https://github.com/voxel51/fiftyone): value proposition tied to a
  runnable start, requirements, docs and security.
- [WhisperX](https://github.com/m-bain/whisperX): basic path separated from
  GPU/token-dependent capabilities.
- [CVAT](https://github.com/cvat-ai/cvat): explicit scope, versions,
  installation, FAQ, security and citation.

Only presentation patterns were adopted. No feature parity, popularity or
architecture equivalence is claimed.

## Over-correction review

Deliberately rejected:

- repository-root `robots.txt`, `llms-full.txt`, sitemap, JSON-LD, action
  manifests or MCP files without a real hosted consumer;
- fabricated canonical URLs, badges, author identity, funding or support SLA;
- keyword-stuffed duplicate landing pages;
- Docker, hosted accounts, GPU/model bundles or other surfaces copied from
  larger reference projects;
- automatic analytics or local-workspace telemetry;
- treating 2,000 stars as an engineering gate, forecast or guarantee.

## Verification

- Relative Markdown/`llms.txt` files and local anchors resolve.
- Markdown code fences are balanced.
- `CITATION.cff`, Dependabot and issue-form YAML parse with `YAML.safe_load`.
- Current public surfaces contain no maintainer absolute paths, private project
  names, credentials or DeepSeek execution requirement.
- The five official reference repository URLs and the cited GitHub/Google
  documentation returned current readable pages.
- Generated-artifact audit passed with 203 candidate files and one bounded
  diagnostic file.
- Independent AEO/public-repo reviewer found one medium wording mismatch and
  one low duplicate-path issue; both were repaired. Final delta review found no
  new HIGH/MEDIUM findings.

## Remaining release gates

- Canonical upstream, authorship/maintainer identity, repository settings and
  public release are unavailable and were not invented.
- Remote CI and public link/crawl behavior remain unverified.
- Current screenshots and their file-set receipt remain intentionally stale
  until T22 final freeze.
- The combined final test/security/browser/render/candidate-digest receipt and
  independent final release review remain T22 work.
