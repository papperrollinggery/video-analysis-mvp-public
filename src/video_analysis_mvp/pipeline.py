from __future__ import annotations

from pathlib import Path

from .audio import analyze_audio
from .media import create_project_id, ingest_source
from .paths import project_paths
from .schemas import AnalysisProfile, StatusEnvelope
from .store import load_media, workspace_path
from .synthesis import synthesize
from .visual import analyze_visual
from .vision import annotate_project_with_vision


def run_full_pipeline(
    source: str,
    profile: AnalysisProfile = AnalysisProfile.ads,
    password: str | None = None,
    workspace: str | None = None,
    project_id: str | None = None,
    language: str = "auto",
    skip_asr: bool = False,
) -> StatusEnvelope:
    paths = project_paths(project_id or create_project_id(source), workspace_path(workspace))
    media = ingest_source(source, paths, profile, password=password)
    analyze_visual(media, paths)
    analyze_audio(media, paths, language=language, skip_asr=skip_asr)
    report = synthesize(paths)
    return StatusEnvelope(
        status="success",
        summary=f"Project {media.project_id} analyzed successfully.",
        next_actions=[
            "Open report.html for client-facing review.",
            "Edit shots.json or transcript.json for human review, then regenerate the report.",
        ],
        artifacts=report.artifacts,
    )


def run_ingest_only(
    source: str,
    profile: AnalysisProfile = AnalysisProfile.ads,
    password: str | None = None,
    workspace: str | None = None,
    project_id: str | None = None,
) -> StatusEnvelope:
    paths = project_paths(project_id or create_project_id(source), workspace_path(workspace))
    media = ingest_source(source, paths, profile, password=password)
    return StatusEnvelope(
        status="success",
        summary=f"Project {media.project_id} ingested successfully.",
        next_actions=["Run visual/audio analysis or the full pipeline."],
        artifacts={
            "project": str(paths.root),
            "media_package": str(paths.data / "media_package.json"),
            "review_copy": media.review_copy_path,
            "audio_wav": media.audio_path,
        },
    )


def run_visual(project_id: str, workspace: str | None = None) -> StatusEnvelope:
    paths = project_paths(project_id, workspace_path(workspace))
    media = load_media(paths)
    shots, scenes = analyze_visual(media, paths)
    return StatusEnvelope(
        status="success",
        summary=f"Generated {len(shots)} shots and {len(scenes)} scenes.",
        artifacts={
            "shots": str(paths.data / "shots.json"),
            "scenes": str(paths.data / "scenes.json"),
            "contact_sheet": str(paths.assets / "contact_sheet.jpg"),
        },
    )


def run_audio(project_id: str, workspace: str | None = None, language: str = "auto", skip_asr: bool = False) -> StatusEnvelope:
    paths = project_paths(project_id, workspace_path(workspace))
    media = load_media(paths)
    transcript, beats, music = analyze_audio(media, paths, language=language, skip_asr=skip_asr)
    return StatusEnvelope(
        status="success",
        summary=f"Generated {len(transcript)} transcript segments, {len(beats)} beat events, and {len(music)} music profiles.",
        artifacts={
            "transcript": str(paths.data / "transcript.json"),
            "subtitles": str(paths.reports / "transcript.srt"),
            "beats": str(paths.data / "beats.json"),
            "music_profile": str(paths.data / "music_profile.json"),
        },
    )


def run_report(project_id: str, workspace: str | None = None) -> StatusEnvelope:
    paths = project_paths(project_id, workspace_path(workspace))
    report = synthesize(paths)
    return StatusEnvelope(
        status="success",
        summary=f"Regenerated report for {project_id}.",
        artifacts=report.artifacts,
    )


def run_vision(
    project_id: str,
    workspace: str | None = None,
    model: str | None = None,
    limit: int | None = None,
    provider: str | None = None,
) -> StatusEnvelope:
    paths = project_paths(project_id, workspace_path(workspace))
    return annotate_project_with_vision(paths, model=model, limit=limit, provider=provider)
