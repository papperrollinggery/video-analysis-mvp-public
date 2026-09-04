from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

from .audio_providers import audio_adapter_capability
from .readiness import evaluate_project_readiness, vision_provider_capability
from .run_lifecycle import MAX_ACTIVE_ANALYSIS_RUNS, MIN_WORKSPACE_FREE_BYTES
from .schemas import StatusEnvelope
from .store import workspace_path
from .utils import require_tool

REQUIRED_PYTHON = {"pydantic": "pydantic", "PIL": "Pillow"}
REQUIRED_TOOLS = ["ffmpeg", "ffprobe", "yt-dlp"]
OPTIONAL_TOOLS = ["whisper", "wkhtmltopdf"]


def run_doctor(workspace: str | None = None, sample: str | None = None) -> StatusEnvelope:
    base = workspace_path(workspace)
    checks: list[str] = []
    missing_required: list[str] = []
    artifacts: dict[str, str] = {}

    checks.append(f"python: {sys.version.split()[0]}")
    for module, package in REQUIRED_PYTHON.items():
        if importlib.util.find_spec(module):
            checks.append(f"python package {package}: ok")
        else:
            checks.append(f"python package {package}: missing")
            missing_required.append(package)

    for tool in REQUIRED_TOOLS:
        try:
            path = require_tool(tool)
            checks.append(f"tool {tool}: {path}")
        except Exception:
            checks.append(f"tool {tool}: missing or not executable")
            missing_required.append(tool)

    for tool in OPTIONAL_TOOLS:
        try:
            path = require_tool(tool)
        except Exception:
            path = ""
        checks.append(f"optional tool {tool}: {path or 'missing'}")

    checks.append(f"workspace: {base}")
    checks.append(
        "background run limits: "
        f"max {MAX_ACTIVE_ANALYSIS_RUNS} active per workspace; "
        f"source bytes + {MIN_WORKSPACE_FREE_BYTES // (1024 * 1024)} MiB free-space reserve"
    )
    checks.append(
        "subprocess controls: bounded combined output, hard timeout, and process-group cancellation"
    )
    if base.exists():
        manifests = sorted(base.glob("*/project_manifest.json"))
        checks.append(f"projects: {len(manifests)}")
        if manifests:
            artifacts["latest_project_manifest"] = str(manifests[-1])
    else:
        checks.append("projects: workspace does not exist yet")

    if sample:
        sample_path = Path(sample).expanduser().resolve()
        artifacts["sample"] = str(sample_path)
        checks.append(f"sample: {'ok' if sample_path.exists() else 'missing'} {sample_path}")
        if not sample_path.exists():
            missing_required.append("sample")
        elif sample_path.stat().st_size > 500 * 1024 * 1024:
            checks.append("sample: over v1 500MB input target")

    provider_configured, provider_diagnostic = vision_provider_capability(base)
    checks.append(f"vision provider capability: {provider_diagnostic}")
    _audio_configured, audio_diagnostic = audio_adapter_capability(base)
    checks.append(f"advanced audio adapter capability: {audio_diagnostic}")
    checks.append(
        "current Codex task analysis: supported via codex prepare/apply without an additional API key; current model execution is not inferred from this capability check"
    )
    provider_ready_projects, human_ready_projects, export_ready_projects = _readiness_projects(base)
    if os.getenv("OPENAI_API_KEY"):
        checks.append("OPENAI_API_KEY: set")
    else:
        checks.append("OPENAI_API_KEY: missing (vision annotation optional)")
    if os.getenv("MINIMAX_API_KEY"):
        checks.append("MINIMAX_API_KEY: set")
    else:
        checks.append("MINIMAX_API_KEY: missing (MiniMax vision optional)")
    if provider_ready_projects:
        checks.append(
            f"evidence readiness: current provider-complete ({len(provider_ready_projects)} projects)"
        )
    if human_ready_projects:
        checks.append(
            f"evidence readiness: current all-shot human review complete ({len(human_ready_projects)} projects)"
        )
    if export_ready_projects:
        checks.append(
            f"evidence readiness: professional export allowed ({len(export_ready_projects)} projects with current v3 receipt)"
        )
    elif provider_configured:
        checks.append(
            "evidence readiness: provider access configured (capability only); export remains blocked until "
            "current complete provider annotation or all-shot human review and a current v3 readiness receipt"
        )
    else:
        checks.append(
            "evidence readiness: export blocked until current complete provider annotation or all-shot human review and a current v3 readiness receipt"
        )

    if missing_required:
        return StatusEnvelope(
            status="warning",
            summary="Doctor found missing required setup: " + ", ".join(sorted(set(missing_required))),
            next_actions=_install_hints(sorted(set(missing_required))),
            artifacts=artifacts,
            diagnostics=checks,
        )
    return StatusEnvelope(
        status="success",
        summary="Doctor checks passed for the required local-first pipeline.",
        next_actions=[
            "Run a local .mp4/.mov through `analyze-video run --skip-asr`.",
            "Inside a current Codex task, use `analyze-video codex prepare <project-id>` and follow the returned guide, then `codex apply`; this path needs no additional provider API key.",
            "Complete provider annotation or all-shot human review before treating annotations as structurally ready.",
        ],
        artifacts=artifacts,
        diagnostics=checks,
    )


def _readiness_projects(base: Path) -> tuple[list[str], list[str], list[str]]:
    provider_ready: list[str] = []
    human_ready: list[str] = []
    export_ready: list[str] = []
    if not base.exists():
        return provider_ready, human_ready, export_ready
    for manifest in sorted(base.glob("*/project_manifest.json")):
        project = manifest.parent
        try:
            data = evaluate_project_readiness(project, workspace_root=base)
        except Exception:
            continue
        if data.get("vision_annotation_complete"):
            provider_ready.append(project.name)
        if data.get("human_review_override"):
            human_ready.append(project.name)
        if data.get("professional_export_allowed"):
            export_ready.append(project.name)
    return provider_ready, human_ready, export_ready


def _install_hints(missing: list[str]) -> list[str]:
    hints = []
    if "pydantic" in missing:
        hints.append("Install Python deps: `python3 -m venv .venv && .venv/bin/python -m pip install -e .`")
    if "Pillow" in missing:
        hints.append("Install the declared image dependency: `python3 -m pip install 'Pillow>=12.3.0'`.")
    if "ffmpeg" in missing or "ffprobe" in missing:
        hints.append("Install media tools: macOS `brew install ffmpeg`; Debian/Ubuntu `sudo apt-get install ffmpeg`; use the equivalent package for your supported POSIX distribution.")
    if "yt-dlp" in missing:
        hints.append("Install URL ingest tool: `python3 -m pip install yt-dlp` or `brew install yt-dlp`")
    if "sample" in missing:
        hints.append("Pass an existing local .mp4/.mov with `analyze-video doctor --sample /path/to/video.mp4`.")
    if not hints:
        hints.append("Install missing tools, then rerun `analyze-video doctor`.")
    return hints
