#!/usr/bin/env sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
VERIFY="$ROOT/scripts/verify-frontend-assets.sh"
TEMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/video-evidence-assets-gate.XXXXXX")"
FRESH="$TEMP_ROOT/fresh"
PACKAGED="$TEMP_ROOT/packaged"

cleanup() {
  rm -rf "$TEMP_ROOT"
}
trap cleanup EXIT

make_fixture() {
  rm -rf "$FRESH" "$PACKAGED"
  mkdir -p "$FRESH/assets" "$PACKAGED/assets"
  printf '%s\n' '<!doctype html><script type="module" src="/assets/app.js"></script><link rel="stylesheet" href="/assets/app.css">' >"$FRESH/index.html"
  printf '%s\n' 'console.log("release gate fixture")' >"$FRESH/assets/app.js"
  printf '%s\n' ':root { color: #fff; }' >"$FRESH/assets/app.css"
  cp -R "$FRESH/." "$PACKAGED/"
}

expect_failure() {
  label="$1"
  if sh "$VERIFY" "$FRESH" "$PACKAGED" >"$TEMP_ROOT/$label.log" 2>&1; then
    printf '%s\n' "frontend asset gate accepted $label drift" >&2
    exit 1
  fi
}

make_fixture
sh "$VERIFY" "$FRESH" "$PACKAGED" >/dev/null

printf '%s\n' 'console.log("tampered fresh source")' >>"$FRESH/assets/app.js"
expect_failure "fresh-source"

make_fixture
printf '%s\n' 'console.log("tampered packaged mirror")' >>"$PACKAGED/assets/app.js"
expect_failure "packaged-mirror"

make_fixture
printf '%s\n' 'stale bundle' >"$PACKAGED/assets/stale.js"
expect_failure "stale-extra"

make_fixture
rm "$PACKAGED/assets/app.css"
expect_failure "missing-file"

printf '%s\n' "frontend asset gate self-test ok: normal pass; source, packaged, stale, and missing drift rejected"
