#!/usr/bin/env sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
if [ ! -x "$PYTHON" ]; then
  PYTHON="python3"
fi

PYTHONPATH="$ROOT/src" "$PYTHON" -m unittest -v \
  tests.test_export_templates \
  tests.test_export_xlsx \
  tests.test_export_pdf \
  tests.test_export_service \
  tests.test_cli_exports \
  tests.test_export_visual_runtime

npm --prefix "$ROOT/frontend" run build
npm --prefix "$ROOT/frontend" run test:e2e
sh "$ROOT/scripts/audit-test-artifacts.sh"
