from __future__ import annotations

from typing import Any

from .pipeline import run_audio, run_full_pipeline, run_ingest_only, run_report, run_visual
from .schemas import AnalysisProfile
from .store import find_projects

try:
    from fastapi import FastAPI
    from pydantic import BaseModel
except Exception as exc:  # pragma: no cover
    FastAPI = None  # type: ignore[assignment]
    BaseModel = object  # type: ignore[assignment,misc]
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None


if FastAPI is None:  # pragma: no cover
    raise RuntimeError("FastAPI is not installed. Install the api extra or use `analyze-video serve`.")


class RunRequest(BaseModel):
    source: str
    profile: AnalysisProfile = AnalysisProfile.ads
    password: str | None = None
    workspace: str | None = None
    project_id: str | None = None
    language: str = "auto"
    skip_asr: bool = True


class ProjectRequest(BaseModel):
    workspace: str | None = None


class AudioRequest(ProjectRequest):
    language: str = "auto"
    skip_asr: bool = True


app = FastAPI(title="Video Analysis MVP", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/projects")
def projects(workspace: str | None = None) -> list[dict[str, Any]]:
    return [project.model_dump(mode="json") for project in find_projects(workspace)]


@app.post("/projects")
def create_and_run(request: RunRequest) -> dict[str, Any]:
    return run_full_pipeline(
        request.source,
        profile=request.profile,
        password=request.password,
        workspace=request.workspace,
        project_id=request.project_id,
        language=request.language,
        skip_asr=request.skip_asr,
    ).model_dump(mode="json")


@app.post("/projects/ingest")
def ingest(request: RunRequest) -> dict[str, Any]:
    return run_ingest_only(
        request.source,
        profile=request.profile,
        password=request.password,
        workspace=request.workspace,
        project_id=request.project_id,
    ).model_dump(mode="json")


@app.post("/projects/{project_id}/analyze/visual")
def analyze_visual(project_id: str, request: ProjectRequest) -> dict[str, Any]:
    return run_visual(project_id, workspace=request.workspace).model_dump(mode="json")


@app.post("/projects/{project_id}/analyze/audio")
def analyze_audio(project_id: str, request: AudioRequest) -> dict[str, Any]:
    return run_audio(
        project_id,
        workspace=request.workspace,
        language=request.language,
        skip_asr=request.skip_asr,
    ).model_dump(mode="json")


@app.post("/projects/{project_id}/report")
def report(project_id: str, request: ProjectRequest) -> dict[str, Any]:
    return run_report(project_id, workspace=request.workspace).model_dump(mode="json")
