#!/usr/bin/env sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
PYTHON="$ROOT/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
  PYTHON="python3"
fi
WORKSPACE="$(mktemp -d "${TMPDIR:-/tmp}/video-evidence-demo-smoke.XXXXXX")"
PROJECT_ID="ci-demo"

cleanup() {
  rm -rf "$WORKSPACE"
}
trap cleanup EXIT HUP INT TERM

VIDEO_ANALYSIS_DEMO_PROJECT_ID="$PROJECT_ID" sh "$ROOT/scripts/run-demo.sh" "$WORKSPACE"

"$PYTHON" - "$WORKSPACE/$PROJECT_ID" "$ROOT/examples/demo/expected-receipt.json" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

project = Path(sys.argv[1])
expected_path = Path(sys.argv[2])

with (project / "project_manifest.json").open(encoding="utf-8") as handle:
    manifest = json.load(handle)
with (project / "data" / "readiness.json").open(encoding="utf-8") as handle:
    readiness = json.load(handle)
with (project / "demo_receipt.json").open(encoding="utf-8") as handle:
    receipt = json.load(handle)
with expected_path.open(encoding="utf-8") as handle:
    expected = json.load(handle)

if manifest.get("project_id") != "ci-demo" or manifest.get("status") != "reported":
    raise SystemExit("demo manifest project/status mismatch")
if manifest.get("profile") != "research":
    raise SystemExit("demo profile mismatch")
if readiness.get("status") != "blocked" or readiness.get("professional_export_allowed") is not False:
    raise SystemExit("demo readiness must fail closed before review")
if receipt.get("schema_version") != expected.get("schema_version"):
    raise SystemExit("demo receipt schema mismatch")
for key, value in expected.get("invariants", {}).items():
    if key == "profile" and receipt.get("profile") != value:
        raise SystemExit("demo receipt profile mismatch")
    if key == "external_vision" and receipt.get("pipeline", {}).get("external_vision") != value:
        raise SystemExit("demo receipt vision mode mismatch")
    if key == "asr" and receipt.get("pipeline", {}).get("asr") != value:
        raise SystemExit("demo receipt ASR mode mismatch")
    if key == "readiness_status" and receipt.get("readiness", {}).get("status") != value:
        raise SystemExit("demo receipt readiness mismatch")
    if key == "professional_export_allowed" and receipt.get("readiness", {}).get(key) is not value:
        raise SystemExit("demo receipt export gate mismatch")
receipt_artifacts = set(receipt.get("artifacts", {}).values())
for relative in expected.get("required_artifacts", []):
    if relative not in receipt_artifacts:
        raise SystemExit(f"demo receipt omitted required artifact: {relative}")
    path = project / relative
    if not path.is_file() or path.stat().st_size == 0:
        raise SystemExit(f"missing demo artifact: {relative}")
if (project / "data" / "vision_annotations.json").exists():
    raise SystemExit("demo unexpectedly produced a vision-provider receipt")
PY

printf '%s\n' "demo smoke ok: real synthetic-media pipeline produced a blocked evidence package"
