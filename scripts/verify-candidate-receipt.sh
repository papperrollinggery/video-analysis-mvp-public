#!/usr/bin/env sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
if [ ! -x "$PYTHON" ]; then
  PYTHON="python3"
fi

if [ "$#" -gt 1 ]; then
  printf '%s\n' "usage: scripts/verify-candidate-receipt.sh [GIT_REF]" >&2
  exit 2
fi
case "${1:-}" in
  -*)
    printf '%s\n' "error: GIT_REF must not start with '-'" >&2
    exit 2
    ;;
esac

VEW_ENFORCE_CANDIDATE_DIGEST=1 \
VEW_CANDIDATE_REF="${1:-}" \
PYTHONPATH="$ROOT/src" \
  "$PYTHON" -m unittest -v \
  tests.test_frontend_contract.FrontendContractTest.test_ui_acceptance_receipt_binds_current_assets_and_screenshots
