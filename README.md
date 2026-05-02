# Video Analysis MVP

Local-first video analysis MVP for turning a raw video file or supported URL into a reviewable shot package, subtitle assets, rhythm data, and a customer-facing report.

This public repository is a sanitized version of a local prototype. It intentionally excludes private videos, generated analysis projects, runtime config, API keys, and customer artifacts.

## What It Does

- Accepts local video files and URL sources supported by `yt-dlp`
- Builds a canonical local project package
- Generates review video, audio WAV, keyframes, and a contact sheet
- Produces first-pass shot and scene structures
- Produces transcript files when ASR is enabled
- Detects rhythm peaks and a coarse music profile
- Exports client deliverables: HTML report, PDF placeholder, CSV, SRT, JSON
- Provides a local Web UI with no mandatory web framework dependency
- Supports English/Chinese switching in the Web UI and generated reports
- Exports an industrial shot breakdown table where each row maps to one reviewable shot/image unit

## Agent-Assisted Build

This project was built with AI coding agents as part of a local video-analysis workflow. Agent assistance was used for:

- Python package structure and CLI design
- Data schemas for media packages, shots, scenes, transcripts, beats, and reports
- FFmpeg/yt-dlp workflow planning
- Report synthesis and bilingual HTML generation
- Web UI iteration
- Error handling and local-first file layout
- README and implementation documentation

The next model-integration target is Xiaomi MiMo. Planned usage includes Chinese shot descriptions, subtitle summarization, report drafting, visual prompt generation, and model comparison for video-analysis workflows.

## Requirements

Required command-line tools:

- `ffmpeg`
- `ffprobe`
- `yt-dlp` for URL ingest

Optional:

- `whisper` for speech transcription
- `wkhtmltopdf` for true PDF rendering
- `fastapi` and `uvicorn` for the optional API module

Python:

- Python 3.11+
- `pydantic`

## Quick Start

Install in editable mode:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

Run a fast first pass without ASR:

```bash
PYTHONPATH=src python3 -m video_analysis_mvp.cli \
  --workspace ./analysis-projects \
  run /path/to/video.mp4 \
  --profile ads \
  --skip-asr
```

Run with a supported URL:

```bash
PYTHONPATH=src python3 -m video_analysis_mvp.cli \
  --workspace ./analysis-projects \
  run "https://example.com/video.mp4" \
  --profile festival \
  --skip-asr
```

Start the local Web UI:

```bash
PYTHONPATH=src python3 -m video_analysis_mvp.cli \
  --workspace ./analysis-projects \
  serve --port 8787
```

Open:

```text
http://127.0.0.1:8787
```

## Output Package

Each project is written under:

```text
analysis-projects/<project-id>/
```

Customer-facing files:

- `reports/report.html`
- `reports/overview.pdf`
- `reports/shot_breakdown.csv`
- `reports/transcript.srt`
- `reports/music_rhythm_summary.json`
- `assets/contact_sheet.jpg`
- `assets/keyframes/`
- `project_manifest.json`

Machine-readable files:

- `data/media_package.json`
- `data/shots.json`
- `data/scenes.json`
- `data/transcript.json`
- `data/beats.json`
- `data/music_profile.json`
- `data/analysis_report.json`

## Review Loop

Edit these files for human review:

- `data/shots.json`
- `data/transcript.json`
- `data/music_profile.json`

Then regenerate:

```bash
PYTHONPATH=src python3 -m video_analysis_mvp.cli \
  --workspace ./analysis-projects \
  report <project-id>
```

## Shot Table Format

`shot_breakdown.csv` uses a professional review table shape:

- `shot_no`
- `shot_id`
- `timecode`
- `duration`
- `frame_ref`
- `shot_scale`
- `camera_angle`
- `camera_motion`
- `composition`
- `visual_description`
- `subject`
- `action`
- `location`
- `onscreen_text`
- `dialogue`
- `speech_summary`
- `sound_design`
- `music_state`
- `beat_density`
- `rhythm_notes`
- `motifs`
- `continuity_notes`
- `review_notes`
- `confidence`

## Current Limits

- Shot boundaries are first-pass estimates, not model-grade scene detection.
- Music style tags are coarse and audit-friendly, not musicology-level classification.
- ASR is available through local `whisper`, but can be slow; `--skip-asr` is the recommended first pass.
- `overview.pdf` is a placeholder text file unless `wkhtmltopdf` is installed. `report.html` is the designed client report.
- Runtime config is local and must not be committed.
