from __future__ import annotations

from pathlib import Path

from .paths import DEFAULT_WORKSPACE, ProjectPaths, project_paths
from .schemas import (
    AnalysisProfile,
    CanonicalMediaPackage,
    ProjectManifest,
    dump_json,
    load_json,
)


def workspace_path(value: str | None = None) -> Path:
    return Path(value).expanduser().resolve() if value else DEFAULT_WORKSPACE


def get_project_paths(project_id: str, workspace: str | None = None) -> ProjectPaths:
    return project_paths(project_id, workspace_path(workspace))


def write_manifest(
    paths: ProjectPaths,
    media: CanonicalMediaPackage,
    status: str,
    artifacts: dict[str, str],
) -> ProjectManifest:
    manifest = ProjectManifest(
        project_id=media.project_id,
        profile=AnalysisProfile(media.analysis_profile),
        root_path=str(paths.root),
        source=media.source,
        status=status,
        artifacts=artifacts,
    )
    dump_json(paths.manifest, manifest)
    return manifest


def load_media(paths: ProjectPaths) -> CanonicalMediaPackage:
    return CanonicalMediaPackage.model_validate(load_json(paths.data / "media_package.json"))


def find_projects(workspace: str | None = None) -> list[ProjectManifest]:
    base = workspace_path(workspace)
    if not base.exists():
        return []
    projects: list[ProjectManifest] = []
    for manifest in sorted(base.glob("*/project_manifest.json"), reverse=True):
        try:
            projects.append(ProjectManifest.model_validate(load_json(manifest)))
        except Exception:
            continue
    return projects
