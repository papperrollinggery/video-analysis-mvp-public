#!/usr/bin/env sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
PYTHON="${PYTHON:-python3}"

"$PYTHON" - "$ROOT" <<'PY'
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
listed = subprocess.check_output(
    ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
    cwd=root,
)
candidates = sorted(
    item.decode("utf-8") for item in listed.split(b"\0") if item
)
forbidden_suffixes = {
    ".bin", ".ckpt", ".mov", ".mp4", ".onnx", ".pdf", ".pt", ".wav", ".xlsx"
}
generated = [
    relative for relative in candidates
    if Path(relative).suffix.lower() in forbidden_suffixes
]
if generated:
    raise SystemExit(f"release candidate contains generated/private media or documents: {generated}")

private_markers: list[bytes] = []
home = os.environ.get("HOME", "").strip()
if home and home not in {"/root", "/home/runner"}:
    private_markers.append(home.encode("utf-8"))
private_markers.extend(
    value.encode("utf-8")
    for value in os.environ.get("VEW_PRIVATE_MEDIA_MARKERS", "").split(os.pathsep)
    if value
)
leaks: list[str] = []
for relative in candidates:
    path = root / relative
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
        continue
    payload = path.read_bytes()
    if any(marker and marker in payload for marker in private_markers):
        leaks.append(relative)
if leaks:
    raise SystemExit(f"release candidate contains a private maintainer/media marker: {leaks}")

diagnostics = root / "test-results"
diagnostic_files: list[Path] = []
if diagnostics.exists():
    for path in diagnostics.rglob("*"):
        if path.is_symlink() or (not path.is_file() and not path.is_dir()):
            raise SystemExit(f"unsafe failure diagnostic entry: {path}")
        if path.is_file():
            diagnostic_files.append(path)
    if len(diagnostic_files) > 12:
        raise SystemExit("failure diagnostics exceed the 12-file limit")
    allowed = {".json", ".log", ".png", ".txt"}
    sizes = []
    for path in diagnostic_files:
        size = path.stat().st_size
        if path.suffix.lower() not in allowed or size > 8 * 1024 * 1024:
            raise SystemExit(f"failure diagnostic is unsupported or over 8 MiB: {path.name}")
        sizes.append(size)
    if sum(sizes) > 32 * 1024 * 1024:
        raise SystemExit("failure diagnostics exceed the 32 MiB total limit")

print(
    f"artifact cleanup policy ok: {len(candidates)} candidate files; "
    f"{len(diagnostic_files)} bounded diagnostic files"
)
PY
