from __future__ import annotations

import re
import stat
from dataclasses import dataclass
from pathlib import Path

from .safe_io import ensure_output_directory


DEFAULT_WORKSPACE = Path.cwd() / "analysis-projects"
PROJECT_ID_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
MAX_PROJECT_ID_LENGTH = 80


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug[:64] or "video"


def validate_project_id(project_id: str) -> str:
    if (
        not project_id
        or len(project_id) > MAX_PROJECT_ID_LENGTH
        or PROJECT_ID_PATTERN.fullmatch(project_id) is None
    ):
        raise ValueError("Invalid project id: expected a lowercase slug containing only letters, numbers, and hyphens")
    return project_id


def resolve_project_root(project_id: str, workspace: Path | None = None) -> Path:
    base = (workspace or DEFAULT_WORKSPACE).expanduser().resolve()
    candidate = (base / validate_project_id(project_id)).resolve()
    if candidate == base or not candidate.is_relative_to(base):
        raise ValueError("Invalid project id: resolved path must stay within the workspace")
    return candidate


@dataclass(frozen=True)
class ProjectPaths:
    root: Path

    @property
    def ingest(self) -> Path:
        return self.root / "ingest"

    @property
    def assets(self) -> Path:
        return self.root / "assets"

    @property
    def keyframes(self) -> Path:
        return self.assets / "keyframes"

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def reports(self) -> Path:
        return self.root / "reports"

    @property
    def manifest(self) -> Path:
        return self.root / "project_manifest.json"

    def ensure(self) -> None:
        workspace = self.root.parent
        workspace.mkdir(parents=True, exist_ok=True)
        workspace_info = workspace.lstat()
        if stat.S_ISLNK(workspace_info.st_mode) or not stat.S_ISDIR(workspace_info.st_mode):
            raise ValueError(f"Unsafe workspace directory: {workspace}")
        try:
            root_info = self.root.lstat()
        except FileNotFoundError:
            self.root.mkdir(mode=0o700)
            root_info = self.root.lstat()
        if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
            raise ValueError(f"Unsafe project directory: {self.root}")
        for path in [self.ingest, self.assets, self.keyframes, self.data, self.reports]:
            ensure_output_directory(path, root=self.root)


def project_paths(project_id: str, workspace: Path | None = None) -> ProjectPaths:
    paths = ProjectPaths(resolve_project_root(project_id, workspace))
    paths.ensure()
    return paths


def new_project_paths(project_id: str, workspace: Path | None = None) -> ProjectPaths:
    """Create a project root exactly once, rejecting every existing pathname.

    The root ``mkdir`` is the concurrency boundary.  A losing creator never
    enters or cleans the winner's directory, even when that directory is still
    empty while the winning ingest is starting.
    """
    root = resolve_project_root(project_id, workspace)
    workspace_root = root.parent
    workspace_root.mkdir(parents=True, exist_ok=True)
    workspace_info = workspace_root.lstat()
    if stat.S_ISLNK(workspace_info.st_mode) or not stat.S_ISDIR(workspace_info.st_mode):
        raise ValueError(f"Unsafe workspace directory: {workspace_root}")
    try:
        root.mkdir(mode=0o700, exist_ok=False)
    except FileExistsError:
        raise FileExistsError(f"Project already exists: {project_id}") from None

    paths = ProjectPaths(root)
    try:
        paths.ensure()
    except Exception:
        # Only empty directories created below our exclusive root are eligible
        # for rollback.  Never recursively remove content another actor added.
        for candidate in (paths.keyframes, paths.assets, paths.ingest, paths.data, paths.reports, root):
            try:
                candidate.rmdir()
            except OSError:
                pass
        raise
    return paths
