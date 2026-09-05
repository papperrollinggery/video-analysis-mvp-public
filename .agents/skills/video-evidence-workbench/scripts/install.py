#!/usr/bin/env python3
"""Install this Skill and an explicitly supplied workbench wheel safely."""

from __future__ import annotations

import argparse
from email.parser import Parser
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import venv
import zipfile

EXPECTED_DISTRIBUTION = "video-analysis-mvp"
EXPECTED_VERSION = "0.3.0"
SKILL_NAME = "video-evidence-workbench"
SKILL_SOURCE = Path(__file__).resolve().parent.parent
EXTRA_MODULES = {"api": "fastapi", "export": "openpyxl", "pdf": "pypdf"}
MAX_METADATA_BYTES = 64 * 1024
MINIMUM_PIP_VERSION = (26, 2, 1)
PINNED_PIP_VERSION = ".".join(map(str, MINIMUM_PIP_VERSION))


def _normalise_distribution(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _safe_regular_file(path: Path, label: str) -> Path:
    candidate = path.expanduser()
    if candidate.is_symlink() or not candidate.is_file():
        raise ValueError(f"{label} must be a regular file: {candidate}")
    return candidate.resolve()


def wheel_metadata(wheel: Path) -> tuple[str, str]:
    wheel = _safe_regular_file(wheel, "wheel")
    if wheel.suffix != ".whl":
        raise ValueError(f"wheel must have a .whl suffix: {wheel}")
    try:
        with zipfile.ZipFile(wheel) as archive:
            names = [info for info in archive.infolist() if info.filename.endswith(".dist-info/METADATA")]
            if len(names) != 1:
                raise ValueError("wheel must contain exactly one .dist-info/METADATA")
            if names[0].file_size > MAX_METADATA_BYTES:
                raise ValueError("wheel METADATA exceeds the allowed size")
            message = Parser().parsestr(archive.read(names[0]).decode("utf-8"))
    except (OSError, UnicodeDecodeError, zipfile.BadZipFile) as error:
        raise ValueError(f"invalid wheel: {wheel}") from error
    name, version = message.get("Name"), message.get("Version")
    if not name or not version or _normalise_distribution(name) != EXPECTED_DISTRIBUTION:
        raise ValueError(f"wheel distribution must be {EXPECTED_DISTRIBUTION}, got {name!r}")
    # This installer deliberately supports one release only. Exact equality is
    # stricter than accepting arbitrary PEP 440 spellings and needs no bootstrap dependency.
    if version != EXPECTED_VERSION:
        raise ValueError(f"wheel version must be this Skill's {EXPECTED_VERSION}, got {version!r}")
    return name, version


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _skill_source_digest(root: Path) -> str:
    """Hash portable Skill content, excluding its generated runtime binding."""
    _reject_symlinks(root)
    digest = hashlib.sha256()
    for item in sorted(root.rglob("*")):
        if item.is_dir() or item.name == "runtime.json" or "__pycache__" in item.parts or item.suffix == ".pyc":
            continue
        relative = item.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        with item.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    return digest.hexdigest()


def _mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"directory cannot be a symlink: {path}")


def _normalise_extras(extras: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    requested = tuple(sorted(set(extras)))
    unknown = set(requested).difference(EXTRA_MODULES)
    if unknown:
        raise ValueError(f"unsupported extras: {', '.join(sorted(unknown))}")
    return requested


def _pip_version(python: Path) -> tuple[int, int, int]:
    result = subprocess.run(
        [str(python), "-I", "-m", "pip", "--version"], check=False, capture_output=True, text=True
    )
    match = re.match(r"^pip (\d+)\.(\d+)(?:\.(\d+))?\s", result.stdout)
    if result.returncode != 0 or match is None:
        raise RuntimeError("managed runtime cannot report a valid pip version")
    return tuple(int(part or 0) for part in match.groups())


def _ensure_safe_pip(python: Path) -> None:
    if _pip_version(python) >= MINIMUM_PIP_VERSION:
        return
    try:
        subprocess.run(
            [
                str(python), "-I", "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
                "--upgrade", f"pip=={PINNED_PIP_VERSION}",
            ],
            check=True,
        )
    except subprocess.CalledProcessError as error:
        raise RuntimeError(f"managed runtime pip upgrade to {PINNED_PIP_VERSION} failed") from error
    if _pip_version(python) < MINIMUM_PIP_VERSION:
        raise RuntimeError(f"managed runtime pip must be at least {PINNED_PIP_VERSION}")


def _probe_runtime(python: Path, expected_version: str, extras: tuple[str, ...]) -> dict[str, object]:
    modules = [EXTRA_MODULES[extra] for extra in extras]
    code = (
        "import importlib, importlib.metadata as md, json; modules = "
        + repr(modules)
        + "; payload={'version': md.version('video-analysis-mvp'), "
        "'module': importlib.import_module('video_analysis_mvp').__file__, "
        "'extras': {name: importlib.import_module(name).__file__ for name in modules}}; "
        "print(json.dumps(payload, sort_keys=True))"
    )
    result = subprocess.run(
        [str(python), "-I", "-c", code],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or len(result.stdout) > 64 * 1024:
        raise RuntimeError("installed runtime did not report the wheel's video-analysis-mvp version")
    try:
        payload = json.loads(result.stdout)
    except ValueError as error:
        raise RuntimeError("installed runtime returned invalid verification data") from error
    if not isinstance(payload, dict) or payload.get("version") != expected_version:
        raise RuntimeError("installed runtime did not report the wheel's video-analysis-mvp version")
    return payload


def _verify_runtime(python: Path, expected_version: str, extras: tuple[str, ...]) -> None:
    _probe_runtime(python, expected_version, extras)


def ensure_runtime(wheel: Path, version: str, runtime_home: Path, extras: tuple[str, ...] = ()) -> tuple[Path, str]:
    extras = _normalise_extras(extras)
    digest = _sha256(wheel)
    extras_tag = "base" if not extras else "-".join(extras)
    runtime_dir = runtime_home / f"{version}-{digest[:16]}-{extras_tag}"
    executable = runtime_dir / "bin" / "analyze-video"
    python = runtime_dir / "bin" / "python"
    if os.path.lexists(runtime_dir):
        if runtime_dir.is_symlink():
            raise ValueError(f"runtime path is a symlink: {runtime_dir}")
        if executable.is_file() and os.access(executable, os.X_OK) and python.is_file():
            _ensure_safe_pip(python)
            _verify_runtime(python, version, extras)
            return executable, digest
        raise RuntimeError(f"runtime directory exists but is incomplete: {runtime_dir}")
    _mkdir(runtime_home)
    try:
        # A venv records absolute paths in its scripts, so create it at its final digest path.
        venv.EnvBuilder(with_pip=True, clear=False, symlinks=(os.name == "posix")).create(str(runtime_dir))
        _ensure_safe_pip(python)
        wheel_requirement = str(wheel) if not extras else f"{wheel}[{','.join(extras)}]"
        subprocess.run(
            [str(python), "-I", "-m", "pip", "install", "--disable-pip-version-check", "--no-input", wheel_requirement],
            check=True,
        )
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise RuntimeError("wheel installation did not create bin/analyze-video")
        _verify_runtime(python, version, extras)
    except Exception:
        if os.path.lexists(runtime_dir):
            if runtime_dir.is_symlink():
                runtime_dir.unlink()
            else:
                shutil.rmtree(runtime_dir, ignore_errors=True)
        raise
    return executable, digest


def _reject_symlinks(root: Path) -> None:
    for item in root.rglob("*"):
        if item.is_symlink():
            raise ValueError(f"Skill source contains a symlink: {item}")


def install_skill(
    skills_dir: Path, executable: Path, version: str, wheel_digest: str, extras: tuple[str, ...] = ()
) -> Path | None:
    extras = _normalise_extras(extras)
    source_digest = _skill_source_digest(SKILL_SOURCE)
    _mkdir(skills_dir)
    target = skills_dir / SKILL_NAME
    if os.path.lexists(target) and target.is_symlink():
        raise ValueError(f"existing Skill path is a symlink: {target}")
    if target.exists() and not target.is_dir():
        raise ValueError(f"existing Skill path is not a directory: {target}")
    runtime = {
        "schema": "video-evidence-workbench-runtime/v1",
        "distribution": EXPECTED_DISTRIBUTION,
        "version": version,
        "wheel_sha256": wheel_digest,
        "extras": list(extras),
        "executable": str(executable),
        "python": str(executable.parent / "python"),
        "skill_sha256": source_digest,
    }
    if target.is_dir() and (target / "runtime.json").is_file() and not (target / "runtime.json").is_symlink():
        try:
            if (
                json.loads((target / "runtime.json").read_text(encoding="utf-8")) == runtime
                and _skill_source_digest(target) == source_digest
            ):
                return None
        except (OSError, ValueError):
            pass
    staging = Path(tempfile.mkdtemp(prefix=f".{SKILL_NAME}-", dir=skills_dir.parent)) / SKILL_NAME
    backup: Path | None = None
    try:
        shutil.copytree(
            SKILL_SOURCE,
            staging,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "runtime.json"),
        )
        (staging / "runtime.json").write_text(json.dumps(runtime, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        backup_root = skills_dir.parent / f".{SKILL_NAME}-backups"
        if target.exists():
            _mkdir(backup_root)
            backup = backup_root / f"{time.time_ns()}-{wheel_digest[:12]}-{secrets.token_hex(6)}"
            os.replace(target, backup)
        try:
            os.replace(staging, target)
        except Exception:
            if backup is not None and not target.exists():
                os.replace(backup, target)
            raise
        return backup
    finally:
        staging_parent = staging.parent
        if staging_parent.exists():
            shutil.rmtree(staging_parent, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", required=True, type=Path, help="explicit local video-analysis-mvp wheel")
    parser.add_argument("--skills-dir", type=Path, default=Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser() / "skills")
    parser.add_argument("--runtime-home", type=Path, default=Path("~/.local/share/video-evidence-workbench/runtimes").expanduser())
    parser.add_argument("--extras", action="append", choices=sorted(EXTRA_MODULES), default=[], help="install optional api/export/pdf support")
    args = parser.parse_args(argv)
    try:
        wheel = _safe_regular_file(args.wheel, "wheel")
        _, version = wheel_metadata(wheel)
        extras = _normalise_extras(args.extras)
        runtime_home = args.runtime_home.expanduser().absolute()
        skills_dir = args.skills_dir.expanduser().absolute()
        executable, digest = ensure_runtime(wheel, version, runtime_home, extras)
        backup = install_skill(skills_dir, executable, version, digest, extras)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"installation failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps({"skill": str(skills_dir / SKILL_NAME), "backup": str(backup) if backup else None, "runtime": str(executable), "extras": list(extras)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
