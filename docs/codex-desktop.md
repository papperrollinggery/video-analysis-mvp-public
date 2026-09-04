# Codex Desktop and ChatGPT companion workflow

Codex Desktop is the model-execution surface, while this workbench owns media evidence, analysis contracts, review, Finalize and export. The current Codex task can use the workbench's `codex prepare/apply/status` interface without a separate provider API key. The workbench does not embed a model, start a task, upload files, or render a ChatGPT visualization automatically.

## Use the current Codex task as the analyzer

This is the existing workflow with Codex supplying the analysis step, not a separate research mode:

```text
Ingest → local visual/audio evidence → Codex analysis → human review → Finalize → requested export
```

After the normal local pipeline has produced a project:

```bash
analyze-video --workspace ./analysis-projects codex prepare my-project
analyze-video --workspace ./analysis-projects codex status my-project
```

`prepare` replaces one current `data/codex_analysis_request.json`. It returns the built-in guide, exact shot/frame versions, source-generation bindings and required response schema. It does not change shots or call a provider. Ask the current Codex task to read this request, inspect its listed frames and available audio/text evidence, and create the specified `codex-analysis-response/v1` JSON.

Submit the result through the tool, never by directly editing `shots.json`:

```bash
analyze-video --workspace ./analysis-projects codex apply my-project --result ./response.json
```

Apply rejects stale or mismatched evidence, preserves human/rejected shots, and writes model proposals through the existing annotation merge path. Model identity is marked `host-managed-unverified`; a submission receipt is not proof that a particular model ran or that the user approved its observations. Use the existing review controls and explicitly Finalize afterwards. Neither prepare nor apply generates Excel/PDF.

The workspace's **Codex** panel exposes the same prepare and response-import actions. A browser button alone cannot call the model in an unrelated Codex conversation; the current task executes the returned guide. Missing OpenAI/MiniMax keys disables those optional API adapters, not this current-task path.

The visual adapter reuses the existing visual-observation schema. Structured VO/music/SFX/silence/mixed events now live in the separate canonical audio timeline with their own machine proposal, human review, effective value, generation digest, and shot association. The current Codex task may enrich that timeline only through the audio provider request/apply contract and only when the host actually inspected the bound audio. Unsupported sound identity remains unknown. See the [native analysis contract](codex-native-analysis.md).

## Generated handoff files

After `run` or `report`, each project includes:

- `reports/codex_handoff.md`: project summary, evidence map, unverified items, and a bounded audit brief;
- `data/visualization_dataset.json`: normalized shot rows with timecodes, confidence, readiness, and project-relative evidence paths.

The handoff deliberately points back to `data/shots.json`, `data/readiness.json`, and `data/lineage.json`. It is a guide, not an independent source of truth.

## Use with Codex Desktop

Codex Desktop can open a local project or repository, and its task and integrated terminal operate in the selected project or worktree context. Use that boundary deliberately:

1. Generate the evidence package locally.
2. Open the repository or the specific project directory in Codex Desktop.
3. Use the current task for tool-driven analysis, or start a separate task only when you want a separate audit/research outcome.
4. Ask Codex to read `reports/codex_handoff.md` first.
5. Add the contact sheet or selected keyframes as image inputs when visual inspection matters.
6. Require every conclusion to cite `shot_id`, timecode, and a project-relative evidence path.
7. Compare the answer with the source video before publishing or acting on it.

Suggested task brief:

```text
Read reports/codex_handoff.md, data/visualization_dataset.json,
data/shots.json, data/readiness.json, and data/lineage.json.

Audit this as an evidence set. Separate measured facts, machine annotations,
human review state, and interpretation. For every finding, cite shot_id,
timecode, and project-relative evidence path. List contradictions and missing
evidence. Do not fill absent values or treat readiness as proof.
```

Use separate Codex tasks for separate outcomes—for example, one evidence audit and one documentation edit—so the files changed by each task remain clear.

## Image inputs

Codex supports images as task inputs. Useful choices are:

- `assets/contact_sheet.jpg` for the full visual sequence;
- selected files under `assets/keyframes/` for close inspection;
- a UI screenshot when diagnosing a review interaction.

Tell Codex what to inspect and which source files to cross-check. An image alone does not include its timecode or review state.

## Use with ChatGPT Work and optional `@Visualize`

In ChatGPT web or desktop, availability of Work and `@Visualize` can vary by plan, platform, account, and rollout. When available, `@Visualize` creates a snapshot preview from the files in that ChatGPT context. It is not a live dashboard connected to this workbench.

Codex CLI and Codex IDE integrations do not render the ChatGPT visualization preview.

To request a visualization explicitly:

1. Attach `data/visualization_dataset.json`.
2. Attach only the keyframes needed for the question.
3. Invoke `@Visualize` if it is available in your ChatGPT surface.
4. Ask for an exploratory shot timeline keyed by `shot_id` and timecode.
5. Preserve missing values and display readiness/confidence without converting them into certainty.
6. Export or record the snapshot if it becomes research evidence; it will not update when local data changes.

Suggested request:

```text
@Visualize Use visualization_dataset.json as the only structured source.
Create an exploratory shot timeline for duration, story beat, annotation
confidence, and readiness. Every point must expose shot_id, timecode, and
primary-frame path. Surface missing frames and unverified_items. Describe
patterns as observations, not causes, and do not invent values.
```

If visualization is unavailable, ask for the same result as a Markdown table and a short evidence memo.

## OpenAI vision is a separate boundary

BridgeDeck is another explicit adapter, not the current Codex task and not a substitute for a missing official API key. Use its [account-scoped local Responses contract](bridgedeck-compatibility.md); do not point the OpenAI Chat Completions adapter at the bridge and assume identical behavior.

The workbench's optional OpenAI vision adapter is not Codex Desktop and is not `@Visualize`. It sends selected frame inputs to an OpenAI API model only after an explicit `vision` command or `run --with-vision` opt-in. Storing credentials alone never starts a provider call. Review the provider request, data handling, model, and cost before enabling it.

The current implementation boundary is specific and pre-1.0:

| Setting | Current behavior |
| --- | --- |
| API route | OpenAI Chat Completions at the configured validated base URL; it is not the Responses API |
| Default model | `gpt-5.4-mini`, read from the local runtime config |
| One-run override | `analyze-video vision <project-id> --provider openai --model <model>` |
| Frame selection | all current shots by default, or the first `N` shots with `--limit N`; each selected shot sends its `frame_ref` image, normally the generated middle keyframe |
| Protected/skipped shots | human-authored and rejected shots are skipped; an unsafe, missing, or invalid image is skipped rather than replaced by a later shot |
| Data sent | one validated PNG/JPEG frame plus shot number, timecode, analysis profile, and required output-field instructions; the source video and transcript are not included in this request |
| Stored locally | normalized annotation fields and a versioned provider/model/frame/media receipt; credentials and a raw provider response are not stored in the project |

`run --with-vision` uses the configured provider and model and does not expose a frame limit. The standalone `vision` command is the explicit surface for provider, model, and positive `--limit` overrides. The adapter can make at most one provider request for each validated, eligible selected shot and caps the response at 2,000 completion tokens, but the workbench does not estimate or enforce monetary cost. Provider pricing, retention, training/data-use policy, region, availability, and model lifecycle remain outside this repository and must be checked against the current provider terms before sending frames. Use the local demo or a normal run without `--with-vision` when those boundaries are unacceptable.

OpenAI references:

- [Codex image inputs](https://learn.chatgpt.com/docs/image-inputs)
- [Codex projects](https://learn.chatgpt.com/docs/projects)
- [ChatGPT visualizations](https://learn.chatgpt.com/docs/visualizations)
- [OpenAI API images and vision guide](https://developers.openai.com/api/docs/guides/images-vision)
- [OpenAI Responses API reference: text, image, and file inputs](https://developers.openai.com/api/reference/cli/resources/responses/methods/create)
- [ChatGPT Work overview and review-oriented workflows](https://learn.chatgpt.com/docs/get-started-with-work)

Availability and product behavior can change; verify the current official documentation before making a release claim.

## Privacy checklist

Before opening or attaching a project in another product:

- confirm you have permission to process and share the source;
- inspect the handoff, transcript, manifests, and paths for sensitive data;
- remove provider keys and private URLs;
- attach the smallest necessary set of frames;
- verify the destination account and retention policy;
- record which files and revision were used for the result.
