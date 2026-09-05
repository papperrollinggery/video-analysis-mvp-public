#!/usr/bin/env python3
"""Run the Video Evidence Workbench without changing the caller's directory."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib


SKILL_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_FILE = SKILL_ROOT / "runtime.json"
EXPECTED_VERSION = "0.3.0"


def _read_runtime() -> dict[str, object] | None:
    """Return an installed runtime only when its declared executable is usable."""
    if not RUNTIME_FILE.is_file() or RUNTIME_FILE.is_symlink():
        return None
    try:
        value = json.loads(RUNTIME_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    executable, python, version = value.get("executable"), value.get("python"), value.get("version")
    if value.get("distribution") != "video-analysis-mvp" or version != EXPECTED_VERSION:
        return None
    if not isinstance(executable, str) or not isinstance(python, str):
        return None
    path, interpreter = Path(executable), Path(python)
    if not path.is_file() or not os.access(path, os.X_OK) or not interpreter.is_file() or not os.access(interpreter, os.X_OK):
        return None
    return value


def _development_python() -> Path | None:
    """Locate only the virtualenv belonging to this checked-out Skill's repo."""
    repo_root = SKILL_ROOT.parents[2]
    pyproject = repo_root / "pyproject.toml"
    source = repo_root / "src" / "video_analysis_mvp" / "cli.py"
    try:
        project = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("project", {})
    except (OSError, ValueError):
        return None
    if project.get("name") != "video-analysis-mvp" or not source.is_file():
        return None
    python = repo_root / ".venv" / "bin" / "python"
    executable = repo_root / ".venv" / "bin" / "analyze-video"
    if python.is_file() and os.access(python, os.X_OK) and executable.is_file() and os.access(executable, os.X_OK):
        return python
    return None


def _executable() -> tuple[Path | None, str]:
    runtime = _read_runtime()
    if runtime is not None:
        return Path(str(runtime["python"])), "installed"
    # An invalid or dangling binding must not silently select an unrelated
    # global/development executable.
    if os.path.lexists(RUNTIME_FILE):
        return None, "invalid-runtime"
    development = _development_python()
    if development is not None:
        return development, "development"
    return None, "missing"


def _runtime_probe(python: Path) -> dict[str, object] | None:
    code = (
        "import importlib, importlib.metadata as md, json; "
        "print(json.dumps({'version': md.version('video-analysis-mvp'), "
        "'module': importlib.import_module('video_analysis_mvp').__file__}, sort_keys=True))"
    )
    result = subprocess.run([str(python), "-I", "-c", code], check=False, capture_output=True, text=True)
    if result.returncode != 0 or len(result.stdout) > 64 * 1024:
        return None
    try:
        value = json.loads(result.stdout)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    executable, source = _executable()
    if args == ["--runtime-info"]:
        payload: dict[str, object] = {"status": source}
        if source == "installed":
            binding = _read_runtime()
            payload["binding"] = binding
            if executable is not None:
                actual = _runtime_probe(executable)
                payload["actual"] = actual
                if actual is None or actual.get("version") != EXPECTED_VERSION or not isinstance(actual.get("module"), str):
                    payload["status"] = "invalid-runtime"
                    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
                    return 2
        elif executable is not None:
            payload["python"] = str(executable)
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0 if executable is not None else 1
    if executable is None:
        print(
            "video-evidence-workbench has no usable runtime. Install a local wheel with: "
            f"python3 -I {SKILL_ROOT / 'scripts' / 'install.py'} --wheel /absolute/package.whl",
            file=sys.stderr,
        )
        return 2
    # -I discards the calling project's PYTHONPATH and user site packages while
    # preserving cwd for relative --workspace arguments.
    return subprocess.call([str(executable), "-I", "-m", "video_analysis_mvp.cli", *args], cwd=os.getcwd())


if __name__ == "__main__":
    raise SystemExit(main())
