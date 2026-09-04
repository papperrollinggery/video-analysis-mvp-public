#!/usr/bin/env sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
if [ ! -x "$PYTHON" ]; then
  PYTHON="python3"
fi

PYTHONPATH="$ROOT/src" "$PYTHON" - <<'PY'
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from video_analysis_mvp.benchmark import run_audio_quality_benchmark

with tempfile.TemporaryDirectory(prefix="vew-audio-quality-") as directory:
    result = run_audio_quality_benchmark(Path(directory))
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
if not result["passed"]:
    raise SystemExit(1)
PY
