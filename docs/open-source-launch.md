# Open-source launch and discovery plan

Status: release checklist for the canonical repository
[papperrollinggery/video-analysis-mvp-public](https://github.com/papperrollinggery/video-analysis-mvp-public).
Repository publication still requires the exact revision's GitHub checks; this
document does not authorize analytics, provider calls or a separate deployment.

## Public promise

Video Evidence Workbench turns one supported video into a local, reviewable
shot/audio evidence package with timecodes, frames, provenance, readiness and
requested client exports.

The public promise stops there. Do not claim universal scene-detection
accuracy, factual certification, verified model identity, Windows support,
hosted collaboration, automatic Codex/ChatGPT execution, or guaranteed GitHub
adoption.

## Why this launch shape

Official repository surfaces read on 2026-09-01 show that mature adjacent
projects lead with a concrete job, a short runnable
path, visible output, support limits and durable project metadata:

- [PySceneDetect](https://github.com/Breakthrough/PySceneDetect) separates CLI,
  Docker and Python quick starts and links benchmark methodology.
- [Label Studio](https://github.com/HumanSignal/label-studio) makes the product
  output visible before presenting multiple installation paths.
- [FiftyOne](https://github.com/voxel51/fiftyone) connects a direct value
  proposition to quick start, system requirements, documentation and security.
- [WhisperX](https://github.com/m-bain/whisperX) keeps its basic path separate
  from CUDA, alignment, diarization and token-dependent capabilities.
- [CVAT](https://github.com/cvat-ai/cvat) exposes product scope, version choice,
  installation, FAQ, security and citation as separate decisions.

The useful pattern is not repository size or feature count. It is a short first
success backed by honest prerequisites and evidence. This project therefore
keeps its offline synthetic demo and does not copy those projects' hosted,
Docker, GPU or account surfaces.

## Release gates

All blocking gates must bind the same final candidate revision.

| Gate | Required evidence | Current boundary |
| --- | --- | --- |
| Identity | canonical GitHub upstream, exact commit/tag, maintainer-owned repository settings | upstream verified; exact revision/tag is recorded by GitHub at release |
| First success | checkout → install → `doctor` → `run-demo.sh`; expected receipt and storyboard present | local path verified; remote clean-start evidence pending |
| Primary job | own video → shot/audio review → Finalize → requested XLSX/PDF | locally verified; provider inference not required |
| Migration | installed CLI dry-run is byte-stable; explicit apply, rollback, recovery and saved versions pass | locally verified |
| UI proof | current desktop/mobile screenshots and capture receipt bind the local file-set digest | locally recorded; not public-platform evidence |
| Quality | complete tests, benchmark, security/audit checks, browser acceptance and independent cold review | local results are bound in the mature-candidate receipt; GitHub checks decide remote status |
| Platform | remote CI green on every advertised Python/OS combination | verify the exact release revision in GitHub Actions; do not generalize beyond the matrix |
| Governance | license, contribution guide, code of conduct, security policy, citation and issue/PR templates | present; private vulnerability reporting enabled and read back from GitHub |

Do not substitute an old screenshot, a local test, an `ACTIVE` workflow file or
a star count for a missing gate.

## Repository setup and release maintenance

Maintain these settings against the verified canonical repository:

1. Set a plain-language description matching the README's first sentence.
2. Add only accurate topics, for example `video-analysis`, `shot-detection`,
   `storyboard`, `human-in-the-loop`, `video-research`, and `local-first`.
3. Use the final synthetic-demo screenshot as the social preview.
4. Enable private vulnerability reporting, then replace the conditional text in
   `SECURITY.md` with the verified channel.
5. Decide whether Discussions has an accountable maintainer before linking it
   as support. Do not imply an SLA.
6. Keep the verified repository URL and release date in package metadata and
   `CITATION.cff`. GitHub uses root `CITATION.cff` to expose citation guidance;
   see the [official citation-file documentation](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-citation-files).
7. Create release notes from current behavior, migration steps, known limits
   and exact verification—not from the roadmap.

No step above is complete until the GitHub setting or public URL can be read
back.

## Discovery map

| Search or agent intent | Canonical answer surface |
| --- | --- |
| local/offline video analysis | README value proposition, local-only boundary and quick start |
| shot breakdown or storyboard generator | README workflow, demo, output contract and benchmark limits |
| auditable video understanding | architecture, readiness, provenance and schema docs |
| VO/music/SFX video breakdown | audio-intelligence data dictionary and audio-review guide |
| Excel/PDF client video report | explicit export section and fixed template specification |
| Codex video analysis | Codex Desktop and current-task prepare/apply contract |
| migrate an older project | migration guide and installed CLI command |
| privacy/security | architecture trust boundaries and security policy |
| research citation | `CITATION.cff`, exact revision and schema version |

Use the phrases only where they answer the corresponding question. Do not add
keyword blocks or duplicate pages for search engines.

`llms.txt` is a concise retrieval map, not a formal standard or proof that a
particular model used it. A repository-level `robots.txt` would not control
GitHub's host. If a separate website is later deployed, crawler policy belongs
at that site's root; see [Google's robots.txt placement guidance](https://developers.google.com/crawling/docs/robots-txt/create-robots-txt).
Do not add `llms-full.txt`, agent action manifests, MCP actions, sitemap or
structured website markup until a real consumer/site and tested capability
justify them.

## First-success funnel

The product has no usage telemetry and must not scan local workspaces. Use only
aggregate GitHub data, privacy-reviewed site analytics if later authorized,
tagged public links and voluntary user evidence.

```text
qualified visitor with a known source
  → quick-start/demo intent
  → evidenced demo-receipt/v1 success
  → first storyboard or reviewed package
  → first requested client export
  → second project within 30 days or external issue/PR/citation
  └→ star as an external outcome
```

Minimum quarterly fields:

- launch date and starting stars (`D0`, `S0`);
- qualified-visitor denominator and source coverage;
- voluntary demo-success numerator and known denominator;
- first-package, first-export and repeat-project evidence where voluntarily
  supplied;
- external reproducible issues, merged pull requests, tutorials, courses and
  research citations;
- net new stars, reported as an outcome rather than single-user attribution.

For the 2,000-star scenario:

```text
required_qualified_visitors = ceil((2000 - S0) / measured_conversion)
```

The 5%, 10% and 15% examples in [product strategy](product-strategy.md) are
planning inputs, not industry benchmarks or promises. Missing denominators stay
unknown; GitHub unique visitors are not silently relabeled as qualified users.

## First 30 days after an authorized launch

1. Publish one final synthetic-demo evidence package and one short walkthrough
   tied to the release revision.
2. Ask for one reproducible install/demo report rather than broad feature
   feedback.
3. Publish one evidence-led use case showing a readiness gate catching a real
   review problem without exposing private media.
4. Review search/referrer coverage, demo failures and contribution friction.
5. Fix the largest evidenced onboarding failure; do not respond by adding
   another framework or provider.

## Explicitly deferred

- package-registry publication and announcements beyond the GitHub release;
- DOI, named authors, funding metadata, and `og:url` for a separate deployed site;
- live provider interoperability claims;
- website robots/sitemap/Search Console;
- large benchmark corpus without reviewed redistribution rights;
- hosted accounts, cloud queue, payment, team features or automatic publishing.
