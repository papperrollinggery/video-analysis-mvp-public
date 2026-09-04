#!/usr/bin/env sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
if [ -z "${PYTHON:-}" ]; then
  PYTHON="python3"
fi
command -v curl >/dev/null 2>&1
command -v ffmpeg >/dev/null 2>&1
command -v git >/dev/null 2>&1
command -v npm >/dev/null 2>&1
"$PYTHON" -m pip --version >/dev/null

TEMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/video-evidence-install.XXXXXX")"
WORKSPACE="$TEMP_ROOT/workspace"
CANDIDATE_ROOT="$TEMP_ROOT/candidate"
WHEEL_ROOT="$TEMP_ROOT/wheels"
INSTALL_ROOT="$TEMP_ROOT/installed"
SERVER_LOG="$TEMP_ROOT/server.log"
SERVER_PID=""

cleanup() {
  if [ -n "$SERVER_PID" ]; then
    kill "$SERVER_PID" >/dev/null 2>&1 || true
    wait "$SERVER_PID" >/dev/null 2>&1 || true
  fi
  rm -rf "$TEMP_ROOT"
}
trap cleanup EXIT

mkdir -p "$CANDIDATE_ROOT" "$WHEEL_ROOT"

# Reconstruct the exact Git candidate in a clean directory. This intentionally
# excludes ignored local build residue while including tracked and non-ignored
# untracked files, which also supports this repository before its first commit.
"$PYTHON" - "$ROOT" "$CANDIDATE_ROOT" <<'PY'
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath


root = Path(sys.argv[1]).resolve()
candidate = Path(sys.argv[2]).resolve()
listing = subprocess.check_output(
    [
        "git",
        "-C",
        str(root),
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
        "-z",
    ]
)
relative_paths = [item.decode("utf-8") for item in listing.split(b"\0") if item]
if not relative_paths:
    raise SystemExit("Git candidate file list is empty")

for relative_text in relative_paths:
    relative = PurePosixPath(relative_text)
    if relative.is_absolute() or ".." in relative.parts:
        raise SystemExit(f"unsafe Git candidate path: {relative_text}")
    source = root.joinpath(*relative.parts)
    target = candidate.joinpath(*relative.parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
        os.symlink(os.readlink(source), target)
    elif source.is_file():
        shutil.copy2(source, target)
    else:
        raise SystemExit(f"Git candidate path is not a regular file: {relative_text}")

print(f"clean Git candidate copied: {len(relative_paths)} files")
PY

(cd "$CANDIDATE_ROOT/frontend" && npm ci && npm run build)

sh "$CANDIDATE_ROOT/scripts/verify-frontend-assets.sh" \
  "$CANDIDATE_ROOT/frontend/dist" \
  "$CANDIDATE_ROOT/src/video_analysis_mvp/frontend_dist"

"$PYTHON" -m pip wheel \
  --no-deps \
  --wheel-dir "$WHEEL_ROOT" \
  "$CANDIDATE_ROOT" >/dev/null

WHEEL_PATH="$(find "$WHEEL_ROOT" -maxdepth 1 -type f -name 'video_analysis_mvp-*.whl' -print)"
if [ -z "$WHEEL_PATH" ] || [ "$(printf '%s\n' "$WHEEL_PATH" | wc -l | tr -d ' ')" -ne 1 ]; then
  printf '%s\n' "candidate build must produce exactly one video-analysis-mvp wheel" >&2
  exit 1
fi

"$PYTHON" -m venv "$INSTALL_ROOT"
PYTHON="$INSTALL_ROOT/bin/python"
"$PYTHON" -m pip install "$WHEEL_PATH" >/dev/null

INSTALLED_FRONTEND="$("$PYTHON" - "$INSTALL_ROOT" <<'PY'
from pathlib import Path
import sys

import video_analysis_mvp

install_root = Path(sys.argv[1]).resolve()
package_root = Path(video_analysis_mvp.__file__).resolve().parent
try:
    package_root.relative_to(install_root)
except ValueError as exc:
    raise SystemExit(f"candidate wheel was not imported from install target: {package_root}") from exc

print(package_root / "frontend_dist")
PY
)"

sh "$CANDIDATE_ROOT/scripts/verify-frontend-assets.sh" \
  "$CANDIDATE_ROOT/frontend/dist" \
  "$INSTALLED_FRONTEND"

"$PYTHON" -m video_analysis_mvp.cli migrate --help >/dev/null

MIGRATION_SOURCE="$TEMP_ROOT/migration-source.mp4"
ffmpeg \
  -hide_banner \
  -loglevel error \
  -y \
  -f lavfi \
  -i "testsrc2=size=160x90:rate=12" \
  -f lavfi \
  -i "anullsrc=channel_layout=stereo:sample_rate=44100" \
  -t 4 \
  -shortest \
  -pix_fmt yuv420p \
  "$MIGRATION_SOURCE"

"$PYTHON" -m video_analysis_mvp.cli \
  --workspace "$WORKSPACE" \
  run "$MIGRATION_SOURCE" \
  --project-id migration-smoke \
  --profile research \
  --delivery-language en \
  --skip-asr >/dev/null

"$PYTHON" - "$WORKSPACE" <<'PY'
from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path

from video_analysis_mvp.cli import main
from video_analysis_mvp.schemas import dump_json, load_json


workspace = Path(sys.argv[1]).resolve()
project = workspace / "migration-smoke"
manifest_path = project / "project_manifest.json"
registry_path = project / "data" / "artifact_registry.json"
receipt_path = project / "data" / "migration_receipt.json"

readiness = load_json(project / "data" / "readiness.json")
readiness["schema_version"] = 2
dump_json(project / "data" / "readiness.json", readiness)
manifest = load_json(manifest_path)
manifest["report_generation"]["schema_version"] = 3
manifest["report_generation"]["source_receipts"].pop("audio_intelligence", None)
dump_json(manifest_path, manifest)

before = {
    manifest_path: manifest_path.read_bytes(),
    registry_path: registry_path.read_bytes(),
    receipt_path: None,
}
stdout = io.StringIO()
with contextlib.redirect_stdout(stdout):
    status = main(
        [
            "--workspace",
            str(workspace),
            "migrate",
            project.name,
        ]
    )
if status != 0 or json.loads(stdout.getvalue())["status"] != "migration_required":
    raise SystemExit("installed migration dry-run did not detect the legacy schema")
after = {
    manifest_path: manifest_path.read_bytes(),
    registry_path: registry_path.read_bytes(),
    receipt_path: receipt_path.read_bytes() if receipt_path.exists() else None,
}
if after != before:
    raise SystemExit("installed migration dry-run changed project metadata")

stdout = io.StringIO()
with contextlib.redirect_stdout(stdout):
    status = main(
        [
            "--workspace",
            str(workspace),
            "migrate",
            project.name,
            "--apply",
        ]
    )
if status != 0 or json.loads(stdout.getvalue())["status"] != "prepared":
    raise SystemExit("installed migration apply did not prepare re-Finalize")
registry = load_json(registry_path)
if any(
    item["scope"] in {"report", "client_export"} and item["state"] == "current"
    for item in registry["artifacts"]
):
    raise SystemExit("installed migration apply left current publication metadata")
if load_json(receipt_path).get("requires_finalize") is not True:
    raise SystemExit("installed migration apply did not write its receipt")

print("installed migrate dry-run/apply contract ok")
PY

PORT="$("$PYTHON" - <<'PY'
import socket
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
)"

"$PYTHON" -m video_analysis_mvp.cli \
  --workspace "$WORKSPACE" serve --host 127.0.0.1 --port "$PORT" \
  >"$SERVER_LOG" 2>&1 &
SERVER_PID="$!"

for _ in 1 2 3 4 5 6 7 8 9 10; do
  if curl -fsS "http://127.0.0.1:$PORT/" >"$TEMP_ROOT/index.html" 2>/dev/null; then
    break
  fi
  sleep 0.5
done

if ! grep -q "Video Evidence Workbench" "$TEMP_ROOT/index.html"; then
  cat "$SERVER_LOG" >&2
  printf '%s\n' "installed package did not serve the React workbench" >&2
  exit 1
fi

"$PYTHON" - "$CANDIDATE_ROOT/frontend/dist" "$PORT" <<'PY'
from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import quote
from urllib.request import urlopen


fresh = Path(sys.argv[1]).resolve()
port = int(sys.argv[2])
for expected in sorted(path for path in fresh.rglob("*") if path.is_file()):
    relative = expected.relative_to(fresh).as_posix()
    request_path = "/" if relative == "index.html" else f"/{quote(relative)}"
    with urlopen(f"http://127.0.0.1:{port}{request_path}", timeout=5) as response:
        served = response.read()
    if served != expected.read_bytes():
        raise SystemExit(f"installed wheel served byte drift: {relative}")

print("installed wheel served every fresh frontend file byte-for-byte")
PY

printf '%s\n' "install smoke ok: clean candidate, fresh build, packaged mirror, wheel, and served assets match"
