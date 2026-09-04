# Persistent synthetic demo

This demo generates a four-second video locally with `ffmpeg`, runs the real research pipeline without ASR or external vision, and keeps the resulting workspace for inspection. It does not download media or call a model provider.

From the repository root, after installing the package:

```bash
./scripts/run-demo.sh
```

The default workspace is the ignored `demo-workspace/` directory. Pass a different workspace as the only argument when needed:

```bash
./scripts/run-demo.sh /path/to/a/demo-workspace
```

Each run creates a new timestamped project and refuses to overwrite a colliding project directory. The command prints the project, storyboard, readiness, Codex handoff, and receipt paths. The generated `demo_receipt.json` records the invariant mode and project-relative artifact paths.

Timing receipt: on 2026-07-15, `/usr/bin/time -p ./scripts/run-demo.sh` completed in **1.31 seconds** on the current macOS maintainer machine with Python dependencies and `ffmpeg` already installed. That is a scoped local measurement, not a cross-platform or clean-install benchmark. The public clean-start target therefore remains ten minutes; the prepared-machine demo target is five minutes or less.

Expected result:

- the manifest status is `reported`;
- the profile is `research`;
- ASR is skipped and external vision is disabled;
- `data/readiness.json` is `blocked` with `professional_export_allowed: false`, because synthetic extraction is not reviewed evidence;
- the storyboard, shot data, Codex handoff, and visualization dataset are non-empty.

[`expected-receipt.json`](expected-receipt.json) is the small invariant contract checked by `scripts/demo-smoke-test.sh`; it is not a captured run and contains no machine-specific paths.
