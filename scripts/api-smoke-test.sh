#!/usr/bin/env sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
PYTHON="$ROOT/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
  PYTHON="python3"
fi

command -v ffmpeg >/dev/null 2>&1
command -v curl >/dev/null 2>&1

WORKSPACE="$(mktemp -d "${TMPDIR:-/tmp}/video-analysis-api-smoke.XXXXXX")"
PROJECT_ID="api-smoke-real-intake"
ASYNC_PROJECT_ID="api-smoke-persistent-run"
VIDEO="$WORKSPACE/source.mp4"
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
trap cleanup EXIT

ffmpeg -y \
  -f lavfi -i testsrc2=size=480x270:rate=12 \
  -f lavfi -i sine=frequency=660:sample_rate=44100 \
  -t 4 -shortest -pix_fmt yuv420p "$VIDEO" >/tmp/video-analysis-api-smoke-ffmpeg.log 2>&1

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
  cat "$SERVER_LOG" >&2
  exit 1
fi

SESSION_RESPONSE="$WORKSPACE/session.json"
curl -fsS "http://127.0.0.1:$PORT/api/session" > "$SESSION_RESPONSE"
CSRF_TOKEN="$("$PYTHON" - "$SESSION_RESPONSE" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    token = json.load(handle).get("csrf_token")
if not isinstance(token, str) or not token:
    raise SystemExit("missing CSRF token")
print(token)
PY
)"

CREATE_RESPONSE="$(mktemp "$WORKSPACE/create.XXXXXX")"
curl -fsS -X POST "http://127.0.0.1:$PORT/api/projects" \
  -H "Content-Type: application/json" \
  -H "X-VEW-CSRF: $CSRF_TOKEN" \
  -d "{\"source\":\"$VIDEO\",\"project_id\":\"$PROJECT_ID\",\"profile\":\"research\",\"language\":\"auto\",\"delivery_language\":\"en\",\"skip_asr\":true}" \
  > "$CREATE_RESPONSE"

"$PYTHON" - "$CREATE_RESPONSE" "$PROJECT_ID" <<'PY'
import json
import sys

path, expected = sys.argv[1:]
with open(path, encoding="utf-8") as handle:
    data = json.load(handle)
actual = data.get("project_id")
if actual != expected:
    raise SystemExit(f"expected project_id {expected!r}, got {actual!r}")
PY

RUN_CREATE_RESPONSE="$WORKSPACE/run-create.json"
curl -fsS -X POST "http://127.0.0.1:$PORT/api/runs" \
  -H "Content-Type: application/json" \
  -H "X-VEW-CSRF: $CSRF_TOKEN" \
  -d "{\"source\":\"$VIDEO\",\"project_id\":\"$ASYNC_PROJECT_ID\",\"profile\":\"research\",\"language\":\"auto\",\"delivery_language\":\"en\",\"skip_asr\":true,\"with_vision\":false}" \
  > "$RUN_CREATE_RESPONSE"

RUN_ID="$("$PYTHON" - "$RUN_CREATE_RESPONSE" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)
run_id = payload.get("run_id")
if payload.get("state") not in {"queued", "running", "completed"} or not isinstance(run_id, str):
    raise SystemExit(f"invalid persistent run creation response: {payload!r}")
print(run_id)
PY
)"

RUN_STATUS_RESPONSE="$WORKSPACE/run-status.json"
RUN_STATE=""
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
  curl -fsS "http://127.0.0.1:$PORT/api/runs/$RUN_ID" > "$RUN_STATUS_RESPONSE"
  RUN_STATE="$("$PYTHON" - "$RUN_STATUS_RESPONSE" <<'PY'
import json
import sys
with open(sys.argv[1], encoding="utf-8") as handle:
    print(json.load(handle).get("state") or "")
PY
)"
  case "$RUN_STATE" in
    completed) break ;;
    failed|interrupted|cancelled)
      cat "$RUN_STATUS_RESPONSE" >&2
      exit 1
      ;;
  esac
  sleep 0.25
done
test "$RUN_STATE" = "completed"

"$PYTHON" - "$RUN_STATUS_RESPONSE" "$ASYNC_PROJECT_ID" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)
if payload.get("project_id") != sys.argv[2] or payload.get("progress") != 100:
    raise SystemExit("persistent run did not bind the expected completed project")
stages = payload.get("stages")
if not isinstance(stages, list) or [item.get("id") for item in stages] != ["ingest", "visual", "audio", "report", "finalize"]:
    raise SystemExit("persistent run stage history is incomplete")
if not all(item.get("state") == "completed" and isinstance(item.get("elapsed_seconds"), (int, float)) for item in stages):
    raise SystemExit("persistent run timing receipt is incomplete")
PY

for path in \
  "/api/projects/$PROJECT_ID" \
  "/api/projects/$PROJECT_ID/canvas" \
  "/api/projects/$PROJECT_ID/media" \
  "/api/projects/$PROJECT_ID/deliverables"
do
  OUT="$(mktemp "$WORKSPACE/response.XXXXXX")"
  curl -fsS "http://127.0.0.1:$PORT$path" > "$OUT"
  "$PYTHON" - "$OUT" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    data = json.load(handle)
if not isinstance(data, dict):
    raise SystemExit("response is not a JSON object")
PY
done

PROJECT_RESPONSE="$WORKSPACE/project-response.json"
CANVAS_RESPONSE="$WORKSPACE/canvas-response.json"
curl -fsS "http://127.0.0.1:$PORT/api/projects/$PROJECT_ID" > "$PROJECT_RESPONSE"
curl -fsS "http://127.0.0.1:$PORT/api/projects/$PROJECT_ID/canvas" > "$CANVAS_RESPONSE"
"$PYTHON" - "$PROJECT_RESPONSE" "$CANVAS_RESPONSE" <<'PY'
import json
import sys

project_path, canvas_path = sys.argv[1:]
with open(project_path, encoding="utf-8") as handle:
    project = json.load(handle)
with open(canvas_path, encoding="utf-8") as handle:
    canvas = json.load(handle)
if "order_status" in project or "generations" in project:
    raise SystemExit("removed mock commerce/generation state leaked into project response")
for node in canvas.get("nodes", []):
    if not isinstance(node, dict):
        continue
    data = node.get("data")
    if node.get("source") == "generation_stub" or (isinstance(data, dict) and data.get("mock") is True):
        raise SystemExit(f"mock canvas node leaked: {node.get('id')}")
PY

REMOVED_STATUS="$(curl -sS -o "$WORKSPACE/removed-route.json" -w '%{http_code}' \
  -X POST "http://127.0.0.1:$PORT/api/orders" \
  -H "Content-Type: application/json" \
  -H "X-VEW-CSRF: $CSRF_TOKEN" -d '{}')"
test "$REMOVED_STATUS" = "404"

test -s "$WORKSPACE/$PROJECT_ID/project_manifest.json"
test -s "$WORKSPACE/$PROJECT_ID/reports/storyboard.html"
test -s "$WORKSPACE/$PROJECT_ID/reports/profile_analysis.html"
test -s "$WORKSPACE/$PROJECT_ID/reports/codex_handoff.md"
test -s "$WORKSPACE/$PROJECT_ID/data/visualization_dataset.json"
test -s "$WORKSPACE/$PROJECT_ID/data/canvas_graph.json"
test -s "$WORKSPACE/$ASYNC_PROJECT_ID/project_manifest.json"
test -s "$WORKSPACE/.vew/runs/$RUN_ID.json"

printf '%s\n' "api smoke ok: synchronous $PROJECT_ID and persistent run $RUN_ID completed"
