#!/usr/bin/env sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
PYTHON="$ROOT/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
  PYTHON="python3"
fi

command -v curl >/dev/null 2>&1
command -v ffmpeg >/dev/null 2>&1

WORKSPACE="$(mktemp -d "${TMPDIR:-/tmp}/video-evidence-review-smoke.XXXXXX")"
PROJECT_ID="review-workflow"
SERVER_LOG="$WORKSPACE/server.log"
PORT="$("$PYTHON" - <<'PY'
import socket
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
)"
SERVER_PID=""

cleanup() {
  if [ -n "$SERVER_PID" ]; then
    kill "$SERVER_PID" >/dev/null 2>&1 || true
    wait "$SERVER_PID" >/dev/null 2>&1 || true
  fi
  rm -rf "$WORKSPACE"
}
trap cleanup EXIT HUP INT TERM

VIDEO_ANALYSIS_DEMO_PROJECT_ID="$PROJECT_ID" sh "$ROOT/scripts/run-demo.sh" "$WORKSPACE" >/dev/null

# Force one low-confidence boundary in the test fixture, then rebuild the
# measured visual receipt. This exercises the explicit bound review path even
# when the current detector rates the simple synthetic clip as high confidence.
PYTHONPATH="$ROOT/src" "$PYTHON" - "$WORKSPACE/$PROJECT_ID" <<'PY'
import sys
from pathlib import Path

from video_analysis_mvp.paths import ProjectPaths
from video_analysis_mvp.schemas import Scene, Shot, dump_json, load_json
from video_analysis_mvp.visual import _build_visual_generation_receipt

paths = ProjectPaths(Path(sys.argv[1]))
shots = [Shot.model_validate(item) for item in load_json(paths.data / "shots.json")]
scenes = [Scene.model_validate(item) for item in load_json(paths.data / "scenes.json")]
shots[0].boundary_confidence = "low"
dump_json(paths.data / "shots.json", shots)
dump_json(paths.data / "visual_generation.json", _build_visual_generation_receipt(paths, shots, scenes))
PY

PYTHONPATH="$ROOT/src" "$PYTHON" -m video_analysis_mvp.cli \
  --workspace "$WORKSPACE" serve --host 127.0.0.1 --port "$PORT" >"$SERVER_LOG" 2>&1 &
SERVER_PID="$!"

for _ in 1 2 3 4 5 6 7 8 9 10; do
  if curl -fsS "http://127.0.0.1:$PORT/api/runtime/doctor" >/dev/null 2>&1; then
    break
  fi
  sleep 0.5
done

if ! curl -fsS "http://127.0.0.1:$PORT/api/runtime/doctor" >/dev/null 2>&1; then
  sed -n '1,160p' "$SERVER_LOG" >&2
  exit 1
fi

"$PYTHON" - "http://127.0.0.1:$PORT" "$PROJECT_ID" "$WORKSPACE/$PROJECT_ID" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

base, project_id, project_value = sys.argv[1:]
project = Path(project_value)


def request(path: str, *, method: str = "GET", payload: dict | None = None, csrf: str | None = None):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if csrf:
        headers["X-VEW-CSRF"] = csrf
    with urlopen(Request(base + path, data=body, headers=headers, method=method), timeout=300) as response:
        content_type = response.headers.get_content_type()
        raw = response.read()
    return json.loads(raw) if content_type == "application/json" else raw


token = request("/api/session").get("csrf_token")
if not isinstance(token, str) or not token:
    raise SystemExit("review smoke did not receive a CSRF token")

project_path = quote(project_id, safe="")
workspace = request(f"/api/projects/{project_path}/workspace")
initial = workspace["deliverables"]["readiness"]
if initial.get("status") != "blocked" or initial.get("professional_export_allowed") is not False:
    raise SystemExit("unreviewed project did not start fail-closed")
shots = workspace["media"].get("shot_boundaries") or []
if not shots:
    raise SystemExit("review smoke project has no shots")

low_boundary_count = 0
for index, shot in enumerate(shots, start=1):
    review = {
        "expected_shot_digest": shot["edit_version"],
        "story_beat": f"Observed beat {index}",
        "content_summary": f"Synthetic test pattern visible in shot {index}.",
        "subject": "synthetic test pattern",
        "action": "changes over time",
        "shot_scale": "full frame",
        "camera_angle": "straight on",
        "camera_motion": "static",
        "composition": "centered test pattern",
        "onscreen_text": "",
        "dialogue": "Synthetic spoken line reviewed by the operator.",
        "review_notes": "review workflow smoke assertion",
        "visual_confidence": 0.92,
        "readiness_status": "ready",
    }
    if str(shot.get("boundary_confidence") or "low").lower() == "low":
        review["boundary_reviewed"] = True
        low_boundary_count += 1
    saved = request(
        f"/api/projects/{project_path}/shots/{quote(shot['id'], safe='')}",
        method="PATCH",
        payload=review,
        csrf=token,
    )
    if saved.get("review_saved") is not True or saved.get("report_regeneration_required") is not True:
        raise SystemExit(f"shot review was not persisted: {shot['id']}")

# Audio reviews are compare-and-swap bound to one generation. Reload after
# every accepted event instead of submitting a stale bulk page.
audio_review_count = 0
while True:
    audio_page = request(
        f"/api/projects/{project_path}/audio?review_status=needs_review&limit=1"
    )
    events = audio_page.get("events") or []
    if not events:
        break
    event = events[0]
    reviewed_audio = request(
        f"/api/projects/{project_path}/audio/events/{quote(event['event_id'], safe='')}/review",
        method="PATCH",
        payload={
            "expected_generation_id": audio_page["generation_id"],
            "expected_proposal_sha256": event["proposal_sha256"],
            "status": "reviewed",
            "overrides": {},
            "review_notes": "review workflow smoke audio assertion",
            "confirm_operator_review": True,
        },
        csrf=token,
    )
    if reviewed_audio.get("review_saved") is not True:
        raise SystemExit(f"audio review was not persisted: {event['event_id']}")
    audio_review_count += 1
    if audio_review_count > 1000:
        raise SystemExit("review smoke exceeded the bounded audio review count")

stale = request(f"/api/projects/{project_path}/workspace")
if stale["deliverables"]["readiness"].get("professional_export_allowed") is not False:
    raise SystemExit("saved reviews exposed a professional package before finalization")

finalized = request(
    f"/api/projects/{project_path}/report",
    method="POST",
    payload={},
    csrf=token,
)
ready_workspace = finalized.get("workspace") or {}
ready = ready_workspace.get("deliverables", {}).get("readiness", {})
if ready.get("status") != "ready" or ready.get("professional_export_allowed") is not True:
    raise SystemExit(f"finalized review workflow is not ready: {ready.get('reasons')}")
if ready.get("boundary_review_complete") is not True:
    raise SystemExit("finalized project did not complete the boundary-review gate")
finalized_shots = json.loads((project / "data" / "shots.json").read_text(encoding="utf-8"))
if finalized_shots[0].get("dialogue") != "Synthetic spoken line reviewed by the operator.":
    raise SystemExit("report finalization overwrote the human-reviewed dialogue field")

artifacts = ready_workspace["deliverables"].get("artifacts") or []
professional = next((item for item in artifacts if item.get("id") == "profile_analysis_html"), None)
if not professional or not professional.get("url"):
    raise SystemExit("ready project did not expose its professional analysis artifact")
request(professional["url"])

manifest = json.loads((project / "project_manifest.json").read_text(encoding="utf-8"))
if low_boundary_count:
    receipt = project / "data" / "boundary_review.json"
    if not receipt.is_file() or "boundary_review_json" not in manifest.get("artifacts", {}):
        raise SystemExit("bound boundary-review receipt is missing from the committed package")

current_shots = ready_workspace["media"].get("shot_boundaries") or []
first = current_shots[0]
original_review = dict(first.get("review_fields") or {})
mutated = request(
    f"/api/projects/{project_path}/shots/{quote(first['id'], safe='')}",
    method="PATCH",
    payload={
        "expected_shot_digest": first["edit_version"],
        "readiness_status": "blocked",
        "review_notes": "intentional mutation after finalization",
    },
    csrf=token,
)
try:
    request(professional["url"])
except HTTPError as exc:
    if exc.code not in {403, 404}:
        raise SystemExit(f"mutated professional artifact returned {exc.code}, expected a safe 403/404 denial") from exc
else:
    raise SystemExit("mutating a finalized review did not revoke professional artifact access")

blocked_again = request(f"/api/projects/{project_path}/workspace")
if blocked_again["deliverables"]["readiness"].get("professional_export_allowed") is not False:
    raise SystemExit("mutation after finalization did not fail closed")

# Restoring byte-identical reviewed shot content must not revive the old
# generation. Only another explicit report finalization may publish it again.
original_review["expected_shot_digest"] = mutated["saved_shot_digest"]
restored = request(
    f"/api/projects/{project_path}/shots/{quote(first['id'], safe='')}",
    method="PATCH",
    payload=original_review,
    csrf=token,
)
if restored.get("report_regeneration_required") is not True:
    raise SystemExit("restored review did not require explicit finalization")
restored_workspace = request(f"/api/projects/{project_path}/workspace")
restored_readiness = restored_workspace["deliverables"]["readiness"]
if restored_workspace.get("generation_id") is not None:
    raise SystemExit("byte-identical restoration revived an invalidated generation id")
if restored_readiness.get("professional_export_allowed") is not False:
    raise SystemExit("byte-identical restoration revived professional export before finalization")
if not any("Finalize" in str(reason) for reason in restored_readiness.get("reasons") or []):
    raise SystemExit("restored review did not expose the explicit finalization requirement")
try:
    request(professional["url"])
except HTTPError as exc:
    if exc.code not in {403, 404}:
        raise SystemExit(f"restored stale artifact returned {exc.code}, expected a safe 403/404 denial") from exc
else:
    raise SystemExit("byte-identical restoration reopened the stale professional artifact")

refinalized = request(
    f"/api/projects/{project_path}/report",
    method="POST",
    payload={},
    csrf=token,
)
refinalized_readiness = (refinalized.get("workspace") or {}).get("deliverables", {}).get("readiness", {})
if refinalized_readiness.get("professional_export_allowed") is not True:
    raise SystemExit("second explicit finalization did not restore professional export")

print(
    "review workflow smoke ok: "
    f"{len(shots)} shot(s), {audio_review_count} audio review(s), "
    f"{low_boundary_count} low-confidence boundary assertion(s), "
    "blocked -> saved -> finalized ready -> mutation blocked -> exact restore blocked -> refinalized ready"
)
PY
