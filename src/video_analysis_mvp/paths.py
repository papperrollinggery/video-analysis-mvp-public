from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


DEFAULT_WORKSPACE = Path.cwd() / "analysis-projects"


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug[:64] or "video"


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
        for path in [self.ingest, self.assets, self.keyframes, self.data, self.reports]:
            path.mkdir(parents=True, exist_ok=True)


def project_paths(project_id: str, workspace: Path | None = None) -> ProjectPaths:
    base = workspace or DEFAULT_WORKSPACE
    paths = ProjectPaths(base / project_id)
    paths.ensure()
    return paths
