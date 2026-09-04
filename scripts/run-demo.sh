#!/usr/bin/env sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"

if [ "$#" -gt 1 ]; then
  printf '%s\n' "usage: ./scripts/run-demo.sh [workspace]" >&2
  exit 2
fi

WORKSPACE="${1:-$ROOT/demo-workspace}"
PROJECT_ID="${VIDEO_ANALYSIS_DEMO_PROJECT_ID:-demo-$(date -u +%Y%m%dt%H%M%Sz)-$$}"
case "$PROJECT_ID" in
  ""|*[!a-z0-9-]*|-*|*-|*--*)
    printf '%s\n' "Demo project id must be a lowercase slug containing letters, numbers, and single hyphens." >&2
    exit 2
    ;;
esac
if [ "${#PROJECT_ID}" -gt 80 ]; then
  printf '%s\n' "Demo project id must be 80 characters or fewer." >&2
  exit 2
fi

for tool in ffmpeg ffprobe; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    printf '%s\n' "$tool is required. Install ffmpeg, then rerun this command." >&2
    exit 1
  fi
done

if [ -x "$ROOT/.venv/bin/python" ]; then
  PYTHON="$ROOT/.venv/bin/python"
else
  PYTHON="python3"
fi

mkdir -p "$WORKSPACE"
WORKSPACE="$(CDPATH= cd -- "$WORKSPACE" && pwd)"
PROJECT="$WORKSPACE/$PROJECT_ID"
if [ -e "$PROJECT" ]; then
  printf '%s\n' "Refusing to overwrite existing demo project: $PROJECT" >&2
  exit 1
fi

TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/video-evidence-demo.XXXXXX")"
SOURCE="$TEMP_DIR/synthetic-demo.mp4"
RUN_STATUS="$TEMP_DIR/run-status.json"
cleanup() {
  rm -rf "$TEMP_DIR"
}
trap cleanup EXIT HUP INT TERM

unset OPENAI_API_KEY MINIMAX_API_KEY VIDEO_ANALYSIS_VISION_PROVIDER || true

ffmpeg \
  -hide_banner \
  -loglevel error \
  -y \
  -f lavfi \
  -i "testsrc2=size=480x270:rate=12" \
  -f lavfi \
  -i "sine=frequency=880:sample_rate=44100" \
  -t 4 \
  -shortest \
  -pix_fmt yuv420p \
  "$SOURCE"

if [ -x "$ROOT/.venv/bin/analyze-video" ]; then
  "$ROOT/.venv/bin/analyze-video" \
    --workspace "$WORKSPACE" \
    run "$SOURCE" \
    --project-id "$PROJECT_ID" \
    --profile research \
    --delivery-language en \
    --skip-asr >"$RUN_STATUS"
else
  PYTHONPATH="$ROOT/src" "$PYTHON" -m video_analysis_mvp.cli \
    --workspace "$WORKSPACE" \
    run "$SOURCE" \
    --project-id "$PROJECT_ID" \
    --profile research \
    --delivery-language en \
    --skip-asr >"$RUN_STATUS"
fi

"$PYTHON" - "$PROJECT" "$RUN_STATUS" <<'PY'
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

project = Path(sys.argv[1])
run_status_path = Path(sys.argv[2])

with run_status_path.open(encoding="utf-8") as handle:
    run_status = json.load(handle)
with (project / "project_manifest.json").open(encoding="utf-8") as handle:
    manifest = json.load(handle)
with (project / "data" / "readiness.json").open(encoding="utf-8") as handle:
    readiness = json.load(handle)

required_artifacts = {
    "manifest": "project_manifest.json",
    "shots": "data/shots.json",
    "readiness": "data/readiness.json",
    "storyboard": "reports/storyboard.html",
    "codex_handoff": "reports/codex_handoff.md",
    "visualization_dataset": "data/visualization_dataset.json",
}
missing = [relative for relative in required_artifacts.values() if not (project / relative).is_file()]
if run_status.get("status") != "success":
    raise SystemExit("demo pipeline did not return success")
if manifest.get("status") != "reported" or manifest.get("profile") != "research":
    raise SystemExit("demo manifest does not describe a reported research project")
if readiness.get("status") != "blocked" or readiness.get("professional_export_allowed") is not False:
    raise SystemExit("unreviewed demo must remain blocked for professional export")
if missing:
    raise SystemExit(f"demo is missing required artifacts: {', '.join(missing)}")

receipt = {
    "schema_version": "demo-receipt/v1",
    "project_id": manifest.get("project_id"),
    "profile": manifest.get("profile"),
    "source": "generated-synthetic-video",
    "pipeline": {
        "external_vision": "disabled",
        "asr": "skipped",
    },
    "readiness": {
        "status": readiness.get("status"),
        "professional_export_allowed": readiness.get("professional_export_allowed"),
    },
    "artifacts": required_artifacts,
}
receipt_path = project / "demo_receipt.json"
temporary = project / ".demo_receipt.json.tmp"
temporary.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
os.replace(temporary, receipt_path)
PY

printf '%s\n' "Demo evidence package created."
printf '%s\n' "project: $PROJECT"
printf '%s\n' "manifest: $PROJECT/project_manifest.json"
printf '%s\n' "storyboard: $PROJECT/reports/storyboard.html"
printf '%s\n' "readiness: $PROJECT/data/readiness.json"
printf '%s\n' "Codex handoff: $PROJECT/reports/codex_handoff.md"
printf '%s\n' "receipt: $PROJECT/demo_receipt.json"
