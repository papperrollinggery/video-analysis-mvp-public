# Product strategy

## Product thesis

Video Evidence Workbench is the open-source, local-first operator layer between raw media and downstream research or model workflows.

Its unit of value is not a chat answer. It is a human-reviewable evidence package in which every material observation can be traced to a shot, timecode, frame, confidence value, and review state.

## Positioning

**Category:** video research workbench

**One sentence:** Turn any supported video into an auditable shot dataset, storyboard, and provenance-backed analysis package on your own machine.

**What makes it distinct:** deterministic extraction, optional model enrichment, human review, readiness gates, and portable outputs are one workflow rather than separate scripts.

Do not position the project as generic “chat with a video.” That category obscures the evidence model and makes the project compete on provider breadth rather than inspectability.

## Primary users

### 1. Video AI engineers

Job: produce a reproducible dataset to debug, compare, or evaluate a video-understanding pipeline.

Required proof: stable inputs and outputs, machine-readable schemas, boundary confidence, source frames, and a repeatable smoke fixture.

### 2. Media and communication researchers

Job: inspect pacing, visual motifs, speech, and narrative structure without losing the link to source timecodes.

Required proof: readable reports, evidence references, explicit missing data, and exports that can move into notebooks or qualitative tools.

### 3. Computational social science and digital-humanities teams

Job: prepare a corpus-level shot table while preserving an audit trail for later coding and interpretation.

Required proof: local processing, versioned JSON, human review state, and clear model/provider boundaries.

### Secondary profiles

Ad strategists, festival reviewers, short-form editors, film students, and AI filmmakers can use profile-specific language and reports. These are profiles over the same evidence model, not separate products.

## Flagship workflow

1. Ingest one local video from the CLI or local UI, or deliberately use a supported public URL from the trusted-operator CLI only.
2. Build a canonical review copy and metadata package.
3. Detect shots, extract primary frames, and build deterministic audio/rhythm evidence plus the structured sound timeline.
4. Optionally add bounded observations through the current Codex task or an explicitly configured provider adapter.
5. Review shots and audio events, then resolve the readiness gate.
6. Explicitly Finalize the digest-bound evidence package.
7. Generate professional XLSX/PDF only when requested; save a version only when history is wanted.
8. Hand the portable package to Codex Desktop, a notebook, or ChatGPT Work for a separate research task.

The first public proof should optimize for one short video completed end to end, not a large batch scheduler.

## Product principles

- **Evidence before interpretation.** Store what was observed and where before explaining why it matters.
- **Local by default.** The deterministic core runs on project files; external provider calls are optional and disclosed.
- **Human review is a product surface.** Confidence is visible and readiness can block a package.
- **Files are the API of last resort.** HTML, CSV, Markdown, and JSON remain useful without the web UI.
- **Provider choice is an adapter.** Core shot and evidence schemas do not depend on one model vendor.
- **No synthetic completeness.** Missing fields remain missing; the UI must not silently replace failed requests with plausible demo data.

## Non-goals

- hosted accounts or multi-tenant collaboration;
- payment, order, or lead-management flows;
- a full non-linear editor or frame-accurate finishing system;
- autonomous publishing or consequential decision-making;
- guaranteed factual, copyright, or authenticity certification;
- a general-purpose image or video generation canvas.

## Release roadmap

### Now: credible local workbench

- deterministic short-video pipeline;
- optional OpenAI and MiniMax vision adapters;
- responsive shot review UI backed by real project data;
- readiness, lineage, Codex handoff, and visualization dataset;
- structured VO/music/SFX review, explicit Finalize, and requested XLSX/PDF;
- explicit legacy-schema migration and fresh-install verification;
- fresh-install documentation, smoke tests, and open-source governance files.

### Next: research-grade repeatability

- schema stabilization and documented compatibility windows;
- corpus/batch index without hiding per-project evidence;
- evaluation fixtures for cuts, dissolves, animation, vertical video, and speech-heavy video;
- explicit provider request logs with sensitive values removed;
- import/export adapters for notebooks and annotation tools.

### Later: ecosystem

- plugin interface for detectors, ASR, and annotation providers;
- comparison views across pipeline versions or human coders;
- reproducible benchmark dataset with documented licenses;
- optional MCP interface after the file and API contracts stabilize.

## Quantified quality gates

These are planning targets, not measured claims.

| Dimension | Public-release target | Evidence |
| --- | --- | --- |
| Fresh start | checkout through first short-video package in 10 minutes on a supported machine | timed install/run receipt |
| Prepared demo | persistent synthetic evidence package in 5 minutes or less after dependencies are installed | `time -p` receipt plus `scripts/demo-smoke-test.sh`; current local macOS receipt is 1.31 seconds |
| Determinism | smoke fixture passes twice with the same structural shot/artifact contract | CI logs and normalized diff |
| Honesty | zero undocumented external network boundaries | architecture/privacy review |
| UI | no blocking overflow at 1440×900 and 390×844 | desktop/mobile screenshots |
| Accessibility | keyboard-reachable core review flow; visible focus; labeled controls | manual audit plus automated checks when added |
| Documentation | every public command copied from `--help` and exercised | docs verification checklist |
| Data | every exported finding can resolve to project, shot, timecode, and evidence path | schema validation |
| Community | issue and pull-request templates, contribution guide, security policy, license | repository file audit |

## Growth goal: 2,000 GitHub stars

Two thousand stars is a 24-month post-launch adoption scenario, not a quality claim, delivery commitment, or guarantee. The clock starts at the first public GitHub release in the canonical repository. Its release date and starting-star baseline belong in that release or the first quarterly snapshot rather than being inferred from this working tree; qualified-visitor and conversion baselines remain unknown until measured.

### Measurement contract

- **Qualified visitor:** a deduplicated human visit to the canonical repository or public landing page whose referrer, tagged campaign, or search intent matches one of the intent clusters below. Until that measurement exists, GitHub unique visitors may be shown only as an explicitly labeled upper-bound proxy—not silently substituted for qualified visitors.
- **Successful demo:** a prepared-machine run of `scripts/run-demo.sh` that produces `demo-receipt/v1`, a `reported` manifest, a deliberately blocked readiness receipt, and all required non-empty artifacts in five minutes or less. The separate clean-start target remains ten minutes. CI proves reproducibility; adoption counts require an opt-in user receipt, tutorial, issue, or study because the application has no usage telemetry.
- **Baseline:** capture launch date `D0`, starting stars `S0`, traffic source coverage, and the first demo success-rate denominator at launch. Pre-launch values remain `unknown`, not zero.
- **Data sources:** aggregate GitHub traffic/stars/referrers, aggregate privacy-reviewed landing-page analytics if later configured, tagged release/tutorial links, CI demo receipts, release downloads, and voluntary issue/discussion/tutorial receipts. Private local workspaces are never scanned for growth reporting.
- **Accountable role:** the **release maintainer** records a quarterly snapshot and the **release reviewer** checks definitions and calculations. Those assignments remain TBD; repository ownership alone does not establish a named accountable person or reporting SLA.

For target `T = 2,000`, launch baseline `S0`, and measured qualified-visitor-to-star conversion `c`, the planning requirement is:

```text
remaining_stars = max(0, T - S0)
required_qualified_visitors = ceil(remaining_stars / c)
observed_conversion = new_stars / qualified_visitors
```

Do not calculate `observed_conversion` when the qualified-visitor denominator is unavailable. The examples below assume `S0 = 0` only to expose acquisition scale; replace them after launch.

| Scenario conversion `c` | Qualified visitors needed if `S0 = 0` |
| ---: | ---: |
| 5% | 40,000 |
| 10% | 20,000 |
| 15% | 13,334 |

These conversion rates are scenario inputs, not an industry benchmark. Replace them with measured repository traffic after launch.

### Quarterly checkpoints

The cumulative star shares below are a pacing scenario across eight quarters, not forecasted results. Each review must publish the actual numerator, denominator coverage, successful-demo evidence, and corrective experiment; it must not backfill missing data.

| Checkpoint | Scenario share of remaining-star goal | Required evidence gate |
| --- | ---: | --- |
| Q1 | 10% | capture `D0`/`S0`; assign maintainer roles; verify weekly source export and at least one public demo receipt |
| Q2 | 20% | report qualified-visitor coverage and demo-success denominator; choose one measured acquisition loop |
| Q3 | 30% | compare measured conversion with 5/10/15% scenarios; publish the largest onboarding failure |
| Q4 | 40% | one-year review of stars, successful demos, repeat projects, citations, issues, and outside contributions |
| Q5 | 52.5% | retire one weak channel and document one reproducible community use case |
| Q6 | 67.5% | rerun the funnel from current stars and measured conversion; revise the visitor requirement |
| Q7 | 82.5% | audit retention and contributor health rather than optimizing stars alone |
| Q8 | 100% | report the actual outcome, including a miss; do not relabel the target as achieved without evidence |

### Growth loops

1. **Proof loop:** publish one licensed sample input, generated evidence package, and a short explanation of what the gate caught.
2. **Search loop:** answer concrete queries such as “local video analysis,” “shot breakdown,” “storyboard generator,” and “auditable video understanding.”
3. **Research loop:** provide versioned schemas, CITATION metadata, and notebook-friendly JSON so a paper or class can cite a revision.
4. **Builder loop:** document one provider adapter and one detector adapter well enough for an outside contributor to add another.
5. **Release loop:** ship small releases with before/after evidence, migration notes, and verified commands.

### Activation metrics

- qualified visitor → successful install, only where an opt-in denominator exists;
- install → successful `demo-receipt/v1` and first generated `storyboard.html`;
- first package → second project within 30 days;
- percentage of projects with human-reviewed shots;
- median time to resolve a blocked readiness gate;
- outside issues with a reproducible fixture;
- outside pull requests merged;
- research citations or course use.

## SEO and generative-engine discovery

### Intent clusters

- **Local pipeline:** local video analysis, offline video analysis, open-source video analysis.
- **Shot evidence:** shot detection, scene detection, shot breakdown, video storyboard generator.
- **Research:** video research tool, video content analysis, timestamped video evidence.
- **Trust:** auditable video AI, video provenance, human-reviewed video annotation.
- **Workflow:** Codex video analysis, structured video dataset, video-to-JSON.

Use these phrases where they answer a real question. Do not repeat them as keyword stuffing.

### Answer surfaces

- README: category, quick start, verified limits.
- `docs/faq.md`: direct questions and short factual answers.
- `docs/architecture.md`: stable component and schema names.
- `llms.txt`: high-signal map for retrieval systems.
- sample package: small, licensed, versioned evidence with expected output.
- release notes: exact behavior changes, commands, and migrations.

Each page should state version/status, distinguish implemented behavior from roadmap, and link claims to a command, schema, or artifact. The verified canonical repository is [papperrollinggery/video-analysis-mvp-public](https://github.com/papperrollinggery/video-analysis-mvp-public); repository description, topics, citation metadata and release notes should use that identity consistently. A separate website, search-console property, sitemap, `og:url`, or structured-site markup still requires a real deployed site.

`CITATION.cff` retains a collective contributor label because no individual author record has been supplied; do not invent a person, organization, email, or unrelated landing page to improve search appearance.
