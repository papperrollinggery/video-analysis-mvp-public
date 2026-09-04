#!/usr/bin/env sh
set -eu

if [ "$#" -ne 2 ]; then
  printf '%s\n' "usage: $0 EXPECTED_DIST ACTUAL_DIST" >&2
  exit 64
fi

PYTHON="${PYTHON:-python3}"
EXPECTED_DIST="$1"
ACTUAL_DIST="$2"

"$PYTHON" - "$EXPECTED_DIST" "$ACTUAL_DIST" <<'PY'
from __future__ import annotations

import hashlib
import sys
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit


class AssetReferences(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.references: set[str] = set()

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        del tag
        for name, value in attrs:
            if name not in {"href", "src"} or value is None:
                continue
            path = unquote(urlsplit(value).path)
            if path.startswith("/assets/"):
                self.references.add(path.removeprefix("/"))


def inventory(root: Path) -> dict[str, str]:
    if not root.is_dir():
        raise SystemExit(f"frontend dist is not a directory: {root}")

    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise SystemExit(f"frontend dist must not contain symlinks: {root}/{relative}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise SystemExit(f"frontend dist contains a non-regular file: {root}/{relative}")
        files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def index_asset_references(root: Path, files: dict[str, str]) -> set[str]:
    index_path = root / "index.html"
    if "index.html" not in files:
        raise SystemExit(f"frontend dist is missing index.html: {root}")

    parser = AssetReferences()
    parser.feed(index_path.read_text(encoding="utf-8"))
    references = parser.references
    if not references:
        raise SystemExit(f"frontend index does not reference any /assets/ files: {index_path}")

    unsafe = sorted(
        reference
        for reference in references
        if PurePosixPath(reference).is_absolute()
        or ".." in PurePosixPath(reference).parts
    )
    if unsafe:
        raise SystemExit(f"frontend index contains unsafe asset references: {unsafe}")

    assets = {relative for relative in files if relative.startswith("assets/")}
    missing = sorted(references - assets)
    extra = sorted(assets - references)
    if missing or extra:
        raise SystemExit(
            "frontend index/asset drift: "
            f"missing={missing or 'none'} extra={extra or 'none'}"
        )
    return references


expected_root = Path(sys.argv[1]).resolve()
actual_root = Path(sys.argv[2]).resolve()
expected = inventory(expected_root)
actual = inventory(actual_root)

index_asset_references(expected_root, expected)
index_asset_references(actual_root, actual)

missing = sorted(set(expected) - set(actual))
extra = sorted(set(actual) - set(expected))
changed = sorted(
    relative
    for relative in set(expected) & set(actual)
    if expected[relative] != actual[relative]
)
if missing or extra or changed:
    raise SystemExit(
        "frontend dist byte drift: "
        f"missing={missing or 'none'} extra={extra or 'none'} "
        f"changed={changed or 'none'}"
    )

print(f"frontend assets match: {len(expected)} files")
PY
