#!/usr/bin/env sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
if [ ! -x "$PYTHON" ]; then
  PYTHON="python3"
fi
WORKSPACE="$(mktemp -d "${TMPDIR:-/tmp}/video-analysis-smoke.XXXXXX")"
PROJECT_ID="smoke-ad-10s"
PROJECT="$WORKSPACE/$PROJECT_ID"
unset OPENAI_API_KEY MINIMAX_API_KEY VIDEO_ANALYSIS_VISION_PROVIDER || true

cleanup() {
  rm -rf "$WORKSPACE"
}
trap cleanup EXIT

mkdir -p "$PROJECT/assets/keyframes" "$PROJECT/data" "$PROJECT/reports" "$PROJECT/ingest"

for frame in \
  shot_0001_start.jpg shot_0001_mid.jpg shot_0001_end.jpg \
  shot_0002_start.jpg shot_0002_mid.jpg shot_0002_end.jpg \
  shot_0003_start.jpg shot_0003_mid.jpg shot_0003_end.jpg
do
  printf 'placeholder frame %s\n' "$frame" > "$PROJECT/assets/keyframes/$frame"
done
printf 'placeholder contact sheet\n' > "$PROJECT/assets/contact_sheet.jpg"
printf 'placeholder review video\n' > "$PROJECT/assets/review.mp4"
printf 'placeholder master video\n' > "$PROJECT/ingest/master.mp4"

cat > "$PROJECT/data/media_package.json" <<JSON
{
  "project_id": "$PROJECT_ID",
  "source_type": "file",
  "source": "synthetic-10s.mp4",
  "local_master_path": "$PROJECT/ingest/master.mp4",
  "review_copy_path": "$PROJECT/assets/review.mp4",
  "audio_path": "$PROJECT/assets/audio.wav",
  "duration_seconds": 10.0,
  "frame_rate": 30.0,
  "resolution": "1080x1920",
  "aspect_ratio": 0.5625,
  "status": "analyzed",
  "analysis_profile": "ads",
  "metadata": {"fixture": "10-second synthetic ad"}
}
JSON

cat > "$PROJECT/data/shots.json" <<'JSON'
[
  {
    "shot_id": "shot_0001",
    "scene_no": "001",
    "shot_no": 1,
    "setup_id": "A",
    "start_time": 0.0,
    "end_time": 3.0,
    "duration": 3.0,
    "timecode": "00:00-00:03",
    "frame_ref": "shot_0001_mid.jpg",
    "primary_frame_ref": "shot_0001_mid.jpg",
    "frame_refs": ["shot_0001_start.jpg", "shot_0001_mid.jpg", "shot_0001_end.jpg"],
    "boundary_confidence": "high",
    "story_beat": "hook",
    "shot_scale": "medium",
    "camera_angle": "front",
    "camera_motion": "static",
    "composition": "centered subject",
    "subject": "founder holding product",
    "action": "names the customer pain",
    "remake_notes": "Keep product visible while naming the pain.",
    "visual_description": "UGC-style opening hook with product in hand",
    "content_summary": "Hook: pain point and product promise",
    "onscreen_text": "Stop wasting edits",
    "visual_confidence": 0.72,
    "readiness_status": "ready",
    "confidence": 0.72
  },
  {
    "shot_id": "shot_0002",
    "scene_no": "001",
    "shot_no": 2,
    "setup_id": "B",
    "start_time": 3.0,
    "end_time": 7.0,
    "duration": 4.0,
    "timecode": "00:03-00:07",
    "frame_ref": "shot_0002_mid.jpg",
    "primary_frame_ref": "shot_0002_mid.jpg",
    "frame_refs": ["shot_0002_start.jpg", "shot_0002_mid.jpg", "shot_0002_end.jpg"],
    "boundary_confidence": "high",
    "story_beat": "demo",
    "shot_scale": "close",
    "camera_angle": "top-down",
    "camera_motion": "push in",
    "composition": "product demo close-up",
    "subject": "app screen",
    "action": "shows one-click result",
    "remake_notes": "Cut directly from problem to visible before-after proof.",
    "visual_description": "Product demo proof moment",
    "content_summary": "Demo: workflow result",
    "onscreen_text": "Before / After",
    "visual_confidence": 0.68,
    "readiness_status": "ready",
    "confidence": 0.68
  },
  {
    "shot_id": "shot_0003",
    "scene_no": "001",
    "shot_no": 3,
    "setup_id": "C",
    "start_time": 7.0,
    "end_time": 10.0,
    "duration": 3.0,
    "timecode": "00:07-00:10",
    "frame_ref": "shot_0003_mid.jpg",
    "primary_frame_ref": "shot_0003_mid.jpg",
    "frame_refs": ["shot_0003_start.jpg", "shot_0003_mid.jpg", "shot_0003_end.jpg"],
    "boundary_confidence": "high",
    "story_beat": "cta",
    "shot_scale": "medium",
    "camera_angle": "front",
    "camera_motion": "static",
    "composition": "creator points to CTA",
    "subject": "creator",
    "action": "asks viewer to try the service",
    "remake_notes": "Keep price anchor and one concrete next step on screen.",
    "visual_description": "CTA with price anchor",
    "content_summary": "CTA: 24-hour teardown service",
    "onscreen_text": "Synthetic fixture",
    "visual_confidence": 0.7,
    "readiness_status": "ready",
    "confidence": 0.7
  }
]
JSON

cat > "$PROJECT/data/scenes.json" <<'JSON'
[
  {
    "scene_id": "scene_001",
    "start_time": 0.0,
    "end_time": 10.0,
    "shot_ids": ["shot_0001", "shot_0002", "shot_0003"],
    "scene_function": "ad hook-demo-cta",
    "pace_label": "fast",
    "confidence": 0.66
  }
]
JSON

cat > "$PROJECT/data/transcript.json" <<'JSON'
[
  {
    "segment_id": "seg_001",
    "start_time": 0.2,
    "end_time": 2.7,
    "text": "你的广告不是没人看，是前三秒没把问题说清楚。",
    "language": "Chinese",
    "speaker": "host",
    "confidence": 0.82
  },
  {
    "segment_id": "seg_002",
    "start_time": 7.2,
    "end_time": 9.5,
    "text": "发我一条广告，24 小时给你拆片包。",
    "language": "Chinese",
    "speaker": "host",
    "confidence": 0.8
  }
]
JSON

cat > "$PROJECT/data/beats.json" <<'JSON'
[
  {"time": 0.4, "strength": 0.82, "source": "fixture", "confidence": 0.9},
  {"time": 1.8, "strength": 0.74, "source": "fixture", "confidence": 0.9},
  {"time": 3.2, "strength": 0.78, "source": "fixture", "confidence": 0.9},
  {"time": 7.1, "strength": 0.86, "source": "fixture", "confidence": 0.9}
]
JSON

cat > "$PROJECT/data/music_profile.json" <<'JSON'
[
  {
    "start_time": 0.0,
    "end_time": 10.0,
    "energy_level": "medium",
    "tempo_bucket": "fast",
    "style_tags": ["ugc", "direct-response"],
    "mood_tags": ["urgent", "clear"],
    "confidence": 0.65
  }
]
JSON

cat > "$PROJECT/reports/transcript.srt" <<'SRT'
1
00:00:00,200 --> 00:00:02,700
你的广告不是没人看，是前三秒没把问题说清楚。

2
00:00:07,200 --> 00:00:09,500
发我一条广告，24 小时给你拆片包。
SRT

cat > "$PROJECT/reports/music_rhythm_summary.json" <<'JSON'
{
  "beats": [
    {"time": 0.4, "strength": 0.82, "source": "fixture", "confidence": 0.9},
    {"time": 7.1, "strength": 0.86, "source": "fixture", "confidence": 0.9}
  ],
  "music_profile": [
    {
      "start_time": 0.0,
      "end_time": 10.0,
      "energy_level": "medium",
      "tempo_bucket": "fast",
      "style_tags": ["ugc", "direct-response"],
      "mood_tags": ["urgent", "clear"],
      "confidence": 0.65
    }
  ]
}
JSON

"$PYTHON" - "$ROOT" "$PROJECT" <<'PY'
from __future__ import annotations

import sys
from pathlib import Path

root = Path(sys.argv[1])
project = Path(sys.argv[2])
sys.path.insert(0, str(root / "src"))

from video_analysis_mvp.audio import _stage_and_commit_audio_generation
from video_analysis_mvp.paths import ProjectPaths
from video_analysis_mvp.schemas import (
    BeatEvent,
    MusicProfile,
    Scene,
    Shot,
    TranscriptSegment,
    dump_json,
    load_json,
)
from video_analysis_mvp.visual import _build_visual_generation_receipt

paths = ProjectPaths(project)
shots = [Shot.model_validate(item) for item in load_json(paths.data / "shots.json")]
scenes = [Scene.model_validate(item) for item in load_json(paths.data / "scenes.json")]
transcript = [
    TranscriptSegment.model_validate(item)
    for item in load_json(paths.data / "transcript.json")
]
beats = [BeatEvent.model_validate(item) for item in load_json(paths.data / "beats.json")]
music = [
    MusicProfile.model_validate(item)
    for item in load_json(paths.data / "music_profile.json")
]

# The hand-authored fixture must cross the same staged-generation boundaries as
# production analysis before report synthesis can consume it.
dump_json(paths.data / "shots.json", shots)
dump_json(paths.data / "scenes.json", scenes)
dump_json(
    paths.data / "visual_generation.json",
    _build_visual_generation_receipt(paths, shots, scenes),
)
_stage_and_commit_audio_generation(paths, transcript, beats, music)
PY

PYTHONPATH="$ROOT/src" "$PYTHON" -m video_analysis_mvp.cli --workspace "$WORKSPACE" report "$PROJECT_ID" >/tmp/video-analysis-smoke-report.json

for file in \
  "$PROJECT/reports/storyboard.html" \
  "$PROJECT/reports/shot_list.csv" \
  "$PROJECT/reports/profile_analysis.html" \
  "$PROJECT/reports/shot_table.csv" \
  "$PROJECT/reports/remake_brief.md" \
  "$PROJECT/reports/branch_board.html" \
  "$PROJECT/reports/prompt_reverse_engineering.md" \
  "$PROJECT/reports/model_prompt_pack.json" \
  "$PROJECT/reports/revision_plan.md" \
  "$PROJECT/reports/codex_handoff.md" \
  "$PROJECT/reports/transcript.srt" \
  "$PROJECT/reports/music_rhythm_summary.json" \
  "$PROJECT/data/audio_generation.json" \
  "$PROJECT/data/visual_generation.json" \
  "$PROJECT/data/lineage.json" \
  "$PROJECT/data/readiness.json" \
  "$PROJECT/data/visualization_dataset.json"
do
  test -s "$file"
done

grep -q "Evidence Export Blocked" "$PROJECT/reports/profile_analysis.html"
grep -q "storyboard.html available" "$PROJECT/reports/profile_analysis.html"
grep -q '"kling_style_json"' "$PROJECT/reports/model_prompt_pack.json"
grep -q '"professional_export_allowed": false' "$PROJECT/data/readiness.json"

"$PYTHON" - "$PROJECT/data/visualization_dataset.json" "$PROJECT" <<'PY'
import json
import sys

dataset_path, absolute_project_path = sys.argv[1:]
with open(dataset_path, encoding="utf-8") as handle:
    dataset = json.load(handle)
if dataset.get("dataset_type") != "video_shot_evidence":
    raise SystemExit("unexpected visualization dataset type")
if dataset.get("evidence_summary", {}).get("shot_count") != 3:
    raise SystemExit("visualization dataset did not preserve all shots")
if absolute_project_path in json.dumps(dataset, ensure_ascii=False):
    raise SystemExit("visualization dataset leaked an absolute project path")
for shot in dataset.get("shots", []):
    if not shot.get("timecode") or not shot.get("evidence_refs", {}).get("shot_record"):
        raise SystemExit("shot evidence is missing timecode or source reference")
PY

if command -v ffmpeg >/dev/null 2>&1 && command -v ffprobe >/dev/null 2>&1; then
  VIDEO="$WORKSPACE/synthetic-10s.mp4"
  ffmpeg -y -f lavfi -i testsrc2=size=720x1280:rate=30 -f lavfi -i sine=frequency=880:sample_rate=44100 -t 10 -shortest -pix_fmt yuv420p "$VIDEO" >/tmp/video-analysis-smoke-ffmpeg.log 2>&1
  PYTHONPATH="$ROOT/src" "$PYTHON" -m video_analysis_mvp.cli --workspace "$WORKSPACE" run "$VIDEO" --profile ads --skip-asr --project-id smoke-video-10s >/tmp/video-analysis-smoke-video.json
  test -s "$WORKSPACE/smoke-video-10s/reports/storyboard.html"
  test -s "$WORKSPACE/smoke-video-10s/reports/shot_list.csv"
  test -s "$WORKSPACE/smoke-video-10s/reports/profile_analysis.html"
  test -s "$WORKSPACE/smoke-video-10s/reports/branch_board.html"
  test -s "$WORKSPACE/smoke-video-10s/reports/codex_handoff.md"
  test -s "$WORKSPACE/smoke-video-10s/data/visualization_dataset.json"
  grep -q "Evidence Export Blocked" "$WORKSPACE/smoke-video-10s/reports/profile_analysis.html"
else
  printf '%s\n' "ffmpeg/ffprobe missing; skipped full synthetic video ingest."
fi

printf '%s\n' "smoke ok: $PROJECT_ID delivery package generated"
