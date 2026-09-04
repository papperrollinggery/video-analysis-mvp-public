from __future__ import annotations

from .audio import analyze_audio
from .audio_intelligence import audio_intelligence_binding
from .media import DEFAULT_MAX_DURATION_SECONDS, create_project_id, ingest_source
from .paths import ProjectPaths, new_project_paths, project_paths
from .schemas import AnalysisProfile, CanonicalMediaPackage, StatusEnvelope, dump_json
from .store import load_media, workspace_path
from .synthesis import synthesize
from .visual import analyze_visual
from .vision import annotate_project_with_vision


def run_full_pipeline(
    source: str,
    profile: AnalysisProfile = AnalysisProfile.research,
    password: str | None = None,
    workspace: str | None = None,
    project_id: str | None = None,
    language: str = "auto",
    delivery_language: str = "zh",
    skip_asr: bool = False,
    asr_model: str | None = None,
    with_vision: bool = False,
    max_duration_seconds: float = DEFAULT_MAX_DURATION_SECONDS,
) -> StatusEnvelope:
    paths = new_project_paths(project_id or create_project_id(source), workspace_path(workspace))
    media = ingest_source(
        source,
        paths,
        profile,
        password=password,
        max_duration_seconds=max_duration_seconds,
    )
    set_delivery_language(paths, media, delivery_language)
    analyze_visual(media, paths)
    analyze_audio(media, paths, language=language, skip_asr=skip_asr, asr_model=asr_model)
    asr = audio_intelligence_binding(paths)["capabilities"]["asr"]
    audio_actions = [f"ASR: {asr['status']}. {asr['reason']}"] if asr["reason"] else []
    vision_result = annotate_project_with_vision(paths) if with_vision else None
    report = synthesize(paths)
    if vision_result is not None and vision_result.status != "success":
        return StatusEnvelope(
            status="warning",
            summary=f"Project {media.project_id} was analyzed, but configured vision annotation failed.",
            next_actions=vision_result.next_actions + audio_actions + [
                "Review the deterministic shot package before rerunning optional vision annotation."
            ],
            artifacts=report.artifacts,
            error=vision_result.error or vision_result.summary,
        )
    return StatusEnvelope(
        status="warning" if asr["status"] == "failed" else "success",
        summary=f"Project {media.project_id} baseline analysis completed. ASR: {asr['status']}; audio identities require evidence-backed review.",
        next_actions=audio_actions + [
            "Open report.html for evidence review.",
            "When working in Codex, run `analyze-video codex prepare <project-id>` with the same workspace, follow its built-in guide, then submit through `codex apply`; no extra API key is required.",
            "Run `analyze-video vision <project-id>` explicitly if external provider annotation is needed.",
            "Use the existing workspace review controls for human assertions, then explicitly Finalize the report; do not bypass the tool by editing readiness or receipt files.",
        ],
        artifacts=report.artifacts,
    )


def run_ingest_only(
    source: str,
    profile: AnalysisProfile = AnalysisProfile.research,
    password: str | None = None,
    workspace: str | None = None,
    project_id: str | None = None,
    max_duration_seconds: float = DEFAULT_MAX_DURATION_SECONDS,
) -> StatusEnvelope:
    paths = new_project_paths(project_id or create_project_id(source), workspace_path(workspace))
    media = ingest_source(
        source,
        paths,
        profile,
        password=password,
        max_duration_seconds=max_duration_seconds,
    )
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


def run_audio(project_id: str, workspace: str | None = None, language: str = "auto", skip_asr: bool = False, asr_model: str | None = None) -> StatusEnvelope:
    paths = project_paths(project_id, workspace_path(workspace))
    media = load_media(paths)
    transcript, beats, music = analyze_audio(media, paths, language=language, skip_asr=skip_asr, asr_model=asr_model)
    binding = audio_intelligence_binding(paths)
    asr = binding["capabilities"]["asr"]
    return StatusEnvelope(
        status="warning" if asr["status"] == "failed" else "success",
        summary=f"Generated input-bound PCM baseline, {len(beats)} onset candidates, and {len(transcript)} transcript segments. ASR: {asr['status']}. Music/SFX/VO identity remains unclassified.",
        next_actions=["Review capability status in audio_intelligence.json before interpreting empty transcripts as silence."] + ([asr["reason"]] if asr["reason"] else []),
        artifacts={
            "transcript": str(paths.data / "transcript.json"),
            "subtitles": str(paths.reports / "transcript.srt"),
            "beats": str(paths.data / "beats.json"),
            "music_profile": str(paths.data / "music_profile.json"),
            "audio_intelligence": str(paths.data / "audio_intelligence.json"),
        },
    )


def run_report(project_id: str, workspace: str | None = None, delivery_language: str | None = None) -> StatusEnvelope:
    paths = project_paths(project_id, workspace_path(workspace))
    if delivery_language:
        media = load_media(paths)
        set_delivery_language(paths, media, delivery_language)
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
    base_url: str | None = None,
) -> StatusEnvelope:
    paths = project_paths(project_id, workspace_path(workspace))
    return annotate_project_with_vision(paths, model=model, limit=limit, provider=provider, base_url=base_url)


def set_delivery_language(paths: ProjectPaths, media: CanonicalMediaPackage, delivery_language: str) -> None:
    media.metadata["delivery_language"] = "en" if delivery_language == "en" else "zh"
    dump_json(paths.data / "media_package.json", media)
