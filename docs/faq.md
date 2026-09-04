# FAQ

## What is Video Evidence Workbench?

It is an open-source, local-first video research tool that turns a video into timecoded shot records, frames, audio/rhythm data, readiness checks, lineage, and portable reports.

## Is it a “chat with video” app?

No. Its primary output is an inspectable evidence package. You can give that package to Codex, ChatGPT, a notebook, or another tool in a separate, explicit step.

## Does the video stay on my machine?

The deterministic local-file pipeline processes media in the selected workspace. Browser/API intake is local-file-only; trusted-operator CLI URL ingest contacts the source host and may follow downloader-controlled redirects. Selected frames are sent to OpenAI or MiniMax only after an explicit `vision` command or `run --with-vision` opt-in; storing credentials alone does not trigger a call. Unset those keys and avoid URL ingest for a strict local-only first pass.

## Does it need an OpenAI key?

No. Ingest, shot/keyframe extraction, audio/rhythm analysis, reports, readiness output, and the local UI can run without one. Vision fields will be incomplete or machine-estimated until a provider or a person annotates them.

## Can I try it without supplying a video?

Yes. After installation, run `./scripts/run-demo.sh`. It creates a four-second synthetic video locally, runs the real pipeline without ASR or external vision, and preserves a new ignored `demo-workspace/` project. The resulting readiness state is deliberately blocked until evidence is reviewed. See [the demo contract](../examples/demo/README.md).

## What does a passing readiness gate mean?

It means the current v3 structural checks passed: shot IDs/timing, confined frame files, media hashes, profile-specific fields, confidence/boundary thresholds, per-shot provenance, the current structured audio binding, and all audio events that require review. Missing audio remains unknown rather than silence; invalid or unresolved present audio blocks professional export. `readiness.json` must match current evidence and report generation. A configured key alone never counts. Passing does not prove truth, copyright permission, or fitness for a consequential decision.

## How accurate is shot detection?

It is a first-pass heuristic. Fast cuts, fades, dissolves, animation, screen recordings, and unusual frame rates can produce missed or extra boundaries. Review the timeline against the source video.

## What video formats work?

The ingest layer recognizes common extensions such as MP4, MOV, MKV, WebM, M4V, and AVI, then relies on the local `ffmpeg` build. Codec support therefore varies by machine. Use `doctor` and a non-sensitive sample to verify your setup.

The declared runtime targets are macOS and POSIX/Linux. Install `ffmpeg` with `brew install ffmpeg` on macOS or, for example, `sudo apt-get install ffmpeg` on Debian/Ubuntu. Package names and codecs vary by distribution. Windows behavior is currently unverified.

## Can it analyze a video URL?

The trusted-operator CLI can use `yt-dlp` for one supported public video only when the URL is read from an owner-only `--source-value-file` and `--acknowledge-url-risk` is present; URL values in argv and playlist/multi-video metadata are rejected. The browser and FastAPI creation surfaces accept local paths only. Availability can change when a site changes its player or access rules. Userinfo credentials and initial hosts resolving to non-public address ranges are rejected, but downloader-controlled redirects and later DNS resolution remain a third-party network boundary rather than an application sandbox. Query strings and fragments are used for the download request but removed from stored receipts and command errors; use an owner-only `--password-file` when required. Downloads are re-probed and must satisfy the default 60-second and bounded-size ingest policy. Only download content you are authorized to access, and prefer an egress-restricted downloader for untrusted URLs.

## Why does MiniMax not auto-install on first use?

Provider adapters are executable supply-chain boundaries. Install `minimax-coding-plan-mcp==0.0.4` explicitly, verify that its top-level `--version` reports exactly `0.0.4`, and place the executable on `PATH` or set an absolute `MINIMAX_MCP_EXECUTABLE`. Runtime `uvx` package fetches are intentionally disabled.

## Can I use a custom vision endpoint with an environment key?

No. Ambient `OPENAI_API_KEY` and `MINIMAX_API_KEY` are eligible only for official provider hosts. Save a custom endpoint and its key explicitly in this workbench's private runtime config. Changing the endpoint clears the stored key so it must be rebound deliberately.

## Is speech transcription required?

No. Use `--skip-asr` for a faster first pass. A local `whisper` executable is optional when transcript detail matters.

## Why are there HTML, CSV, Markdown, and JSON outputs?

HTML supports human review, CSV works with spreadsheets, Markdown carries bounded context into research tasks, and JSON preserves structured evidence for code and visualization. The package remains useful even if the web UI changes.

## How do I use it with Codex Desktop?

Generate the package, open the project in Codex Desktop, and use the tool's `codex prepare → apply → human review → Finalize → requested export` sequence. Ask the current task to follow the generated guide and inspect only the listed evidence. Missing evidence should be reported; state-changing pipeline commands require your explicit authorization. See [the companion guide](codex-desktop.md).

## Does “Open Codex handoff” run Codex?

No. It opens or identifies a generated local artifact for the user. Starting a Codex task and deciding which files to expose remain explicit user actions.

## Is `@Visualize` built into this project?

No. It is an optional ChatGPT web/desktop capability whose availability varies. It creates a snapshot in ChatGPT from explicitly attached data; it is not available as an embedded live view in Codex CLI or IDE integrations.

## Can I process many videos?

The current flagship flow is one project at a time. Shell automation is possible, but corpus management, scheduling, retries, and aggregate evaluation are roadmap items.

## Are schemas stable?

Not yet. This project is pre-1.0. Pin a revision for research and inspect schema changes before upgrading. Use `analyze-video --workspace WORKSPACE migrate PROJECT` for a read-only inspection. Add `--apply` only when you intend to recover any interrupted migration, invalidate legacy publication, and explicitly re-Finalize; migration never regenerates media or client files itself.

## Does it have authentication, teams, or payments?

No production-ready version of those features exists. The local server is intended for loopback use by one trusted operator.

## Why did PDF output look like text?

There are two separate outputs. `overview.pdf` is a legacy convenience file and is omitted when `wkhtmltopdf` is unavailable. The professional client PDF is generated only through the explicit export service and requires `.[pdf]`, Node, Playwright modules, an existing Chromium executable, and a CJK-capable font configured through the four `VEW_PDF_*` runtime variables. XLSX needs only `.[export]`. Finalize never generates either file; use the README's copyable `export generate` commands.

## Why is professional export still blocked after analysis?

Analysis and a model proposal are not approval. Resolve every shot and audio event that the readiness response lists, save the explicit operator review, then run **Finalize package** once. If any bound media, frame, review, audio generation, or report changed afterward, Finalize again before requesting a new export. Do not edit readiness or receipts directly.

## Why did starting or retrying an analysis return HTTP 429?

One workspace admits one background analysis run at a time and reserves the source size plus 256 MiB of free space. Inspect the existing run first; cancel it only when intended, or free disk space and retry. The limit is local admission control, not a provider rate limit.

## How do I verify a change?

```bash
python3 -m compileall -q src
sh scripts/smoke-test.sh
sh scripts/demo-smoke-test.sh
sh scripts/api-smoke-test.sh
sh scripts/install-smoke-test.sh
sh scripts/benchmark-audio.sh
sh scripts/test-client-exports.sh
sh scripts/audit-test-artifacts.sh
npm --prefix frontend run test:integration
npm --prefix frontend run test:e2e
npm --prefix frontend run build
```

These checks do not exercise external providers or every codec. State those gaps when reporting results.

## How can I contribute?

Start with a reproducible issue using synthetic or licensed media, then read [CONTRIBUTING.md](../CONTRIBUTING.md). Do not attach private media, credentials, or password-protected links.
