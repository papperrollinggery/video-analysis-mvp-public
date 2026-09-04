from __future__ import annotations

import copy
import errno
import hashlib
import json
import math
import mimetypes
import os
import re
import secrets
import stat
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qs, quote, unquote

from .artifacts import (
    PROFESSIONAL_EXPORT_IDS,
    PROFESSIONAL_EXPORT_RELATIVE_PATHS,
    artifact_path,
    iter_workspace_artifacts,
    load_artifact_registry,
    mark_artifacts_stale,
)
from .boundary_review import build_boundary_review_receipt, validate_boundary_review_receipt
from .config import load_runtime_config, mask_secret, save_runtime_config
from .doctor import run_doctor
from .paths import ProjectPaths, resolve_project_root
from .readiness import canonical_readiness_payload, evaluate_project_readiness, read_frame_evidence
from .safe_io import advisory_file_lock, read_regular_bytes
from .schemas import CanonicalMediaPackage, ProjectManifest, Shot
from .store import find_projects
from .synthesis import verify_report_generation_manifest
from .visual import visual_generation_binding


JsonDict = dict[str, Any]


_PROJECT_LOCKS: dict[str, threading.RLock] = {}
_PROJECT_LOCKS_GUARD = threading.Lock()
MAX_PROJECT_JSON_BYTES = 8 * 1024 * 1024
MAX_DELIVERABLE_PREVIEW_BYTES = 60_000
SHOT_REVIEW_TEXT_FIELDS = frozenset(
    {
        "story_beat",
        "content_summary",
        "content_summary_zh",
        "subject",
        "subject_zh",
        "action",
        "action_zh",
        "shot_scale",
        "camera_angle",
        "camera_motion",
        "composition",
        "onscreen_text",
        "dialogue",
        "review_notes",
    }
)
SHOT_REVIEW_FIELDS = frozenset(
    {
        *SHOT_REVIEW_TEXT_FIELDS,
        "visual_confidence",
        "readiness_status",
        "boundary_reviewed",
        "expected_shot_digest",
    }
)
SHOT_REVIEW_STATUS_VALUES = frozenset({"blocked", "ready", "rejected"})
MAX_SHOT_REVIEW_TEXT_BYTES = 8 * 1024
REPORT_INVALIDATION_SCHEMA_VERSION = 1
FINALIZATION_REQUIRED_REASON = "Finalize the current human review to publish a bound report generation."


class ApiError(Exception):
    def __init__(self, status: int, message: str, details: Any | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.details = details


def dispatch_api(root: Path, method: str, path: str, query: str, body: bytes) -> tuple[int, JsonDict]:
    parts = [part for part in path.strip("/").split("/") if part]
    if not parts or parts[0] != "api":
        raise ApiError(404, "API route not found")
    body_data = _parse_json_body(body)
    query_data = {key: values[-1] for key, values in parse_qs(query).items()}
    try:
        return _dispatch(root, method.upper(), parts[1:], query_data, body_data)
    except ApiError:
        raise
    except Exception as exc:
        raise ApiError(500, "API request failed", str(exc)) from exc


def ensure_workspace_api_files(root: Path) -> list[JsonDict]:
    initialized: list[JsonDict] = []
    for project in find_projects(str(root)):
        project_root = _project_root(root, project.project_id)
        ensure_project_data(root, project_root)
        initialized.append({"project_id": project.project_id, "data_dir": str(project_root / "data")})
    return initialized


def _dispatch(root: Path, method: str, parts: list[str], query: JsonDict, body: JsonDict) -> tuple[int, JsonDict]:
    if parts == ["intake", "validate"] and method == "POST":
        return 200, validate_intake(root, body)

    if parts == ["runs"] and method == "GET":
        from .run_lifecycle import list_analysis_runs

        try:
            return 200, {"runs": list_analysis_runs(root, project_id=str(query.get("project_id") or "") or None)}
        except ValueError as exc:
            raise ApiError(400, str(exc)) from exc

    if parts == ["runs"] and method == "POST":
        return 202, create_analysis_run_from_intake(root, body)

    if len(parts) == 2 and parts[0] == "runs" and method == "GET":
        from .run_lifecycle import read_analysis_run

        try:
            return 200, read_analysis_run(root, parts[1])
        except FileNotFoundError:
            raise ApiError(404, "Analysis run not found") from None
        except ValueError as exc:
            raise ApiError(400, str(exc)) from exc

    if len(parts) == 3 and parts[0] == "runs" and parts[2] == "retry" and method == "POST":
        from .run_lifecycle import RunAdmissionError, retry_analysis_run, validate_run_id

        try:
            validate_run_id(parts[1])
        except ValueError as exc:
            raise ApiError(400, str(exc)) from exc
        try:
            return 202, retry_analysis_run(root, parts[1])
        except FileNotFoundError:
            raise ApiError(404, "Analysis run not found") from None
        except RunAdmissionError as exc:
            raise ApiError(429, str(exc)) from exc
        except ValueError as exc:
            raise ApiError(409, str(exc)) from exc

    if len(parts) == 3 and parts[0] == "runs" and parts[2] == "cancel" and method == "POST":
        from .run_lifecycle import cancel_analysis_run, validate_run_id

        try:
            validate_run_id(parts[1])
        except ValueError as exc:
            raise ApiError(400, str(exc)) from exc
        try:
            return 202, cancel_analysis_run(root, parts[1])
        except FileNotFoundError:
            raise ApiError(404, "Analysis run not found") from None
        except ValueError as exc:
            raise ApiError(409, str(exc)) from exc

    if parts == ["projects"] and method == "GET":
        return 200, {"projects": [_project_summary(root, project) for project in find_projects(str(root))]}

    if parts == ["projects"] and method == "POST":
        return 201, create_project_from_intake(root, body)

    if len(parts) == 2 and parts[0] == "projects" and method == "GET":
        project = _project_root(root, parts[1])
        return 200, _project_detail(root, project)

    if len(parts) >= 3 and parts[0] == "projects":
        project = _project_root(root, parts[1])
        tail = parts[2:]
        if tail == ["workspace"] and method == "GET":
            return 200, workspace_snapshot_payload(root, project)
        if tail == ["audio"] and method == "GET":
            return 200, _audio_review_action(project, "list", None, query, body)
        if len(tail) == 3 and tail[:2] == ["audio", "events"] and method == "GET":
            return 200, _audio_review_action(project, "show", tail[2], query, body)
        if len(tail) == 4 and tail[:2] == ["audio", "events"] and tail[3] == "review" and method == "PATCH":
            return 200, _audio_review_action(project, "apply", tail[2], query, body)
        if tail == ["codex"] and method == "GET":
            return 200, _codex_analysis_action(project, "status", body)
        if len(tail) == 2 and tail[0] == "codex" and tail[1] in {"prepare", "apply"} and method == "POST":
            return 200, _codex_analysis_action(project, tail[1], body)
        if tail == ["exports"] and method in {"GET", "POST"}:
            return 200, _client_export_action(project, "status" if method == "GET" else "generate", body)
        if tail == ["exports", "state"] and method == "GET":
            return 200, _client_export_action(project, "state", body)
        if len(tail) == 2 and tail[0] == "exports" and tail[1] in {"cancel", "save", "recover"} and method == "POST":
            return 200, _client_export_action(project, tail[1], body)
        if len(tail) == 3 and tail[:2] == ["exports", "saved"] and method == "DELETE":
            if body:
                raise ApiError(400, "Saved export DELETE does not accept a request body")
            return 200, _client_export_action(project, "delete", {"version_id": tail[2]})
        if tail == ["canvas"] and method == "GET":
            return 200, ensure_canvas_graph(root, project)
        if tail == ["canvas", "viewport"] and method == "PATCH":
            return 200, update_canvas_viewport(root, project, body)
        if tail == ["canvas", "nodes"] and method == "POST":
            return 201, create_canvas_node(root, project, body)
        if len(tail) == 3 and tail[:2] == ["canvas", "nodes"] and method == "PATCH":
            return 200, update_canvas_node(root, project, tail[2], body)
        if len(tail) == 3 and tail[:2] == ["canvas", "nodes"] and method == "DELETE":
            return 200, delete_canvas_node(root, project, tail[2])
        if tail == ["canvas", "edges"] and method == "POST":
            return 201, create_canvas_edge(root, project, body)
        if len(tail) == 3 and tail[:2] == ["canvas", "edges"] and method == "DELETE":
            return 200, delete_canvas_edge(root, project, tail[2])
        if tail == ["media"] and method == "GET":
            return 200, ensure_media_timeline(root, project)
        if tail == ["media", "review-video"] and method == "GET":
            return 200, review_video_payload(root, project)
        if tail == ["media", "frames"] and method == "GET":
            return 200, frame_at_time(root, project, query.get("time") or 0.0)
        if tail == ["media", "frame-markers"] and method == "POST":
            return 201, create_frame_marker(root, project, body)
        if tail == ["media", "segments"] and method == "POST":
            return 201, create_media_segment(root, project, body)
        if len(tail) == 2 and tail[0] == "shots" and method == "PATCH":
            return 200, update_shot_review(root, project, tail[1], body)
        if tail == ["report"] and method == "POST":
            return 200, regenerate_project_report(root, project)
        if tail == ["deliverables"] and method == "GET":
            return 200, deliverables_payload(root, project)
        if len(tail) == 3 and tail[0] == "deliverables" and tail[2] == "preview" and method == "GET":
            return 200, deliverable_preview(root, project, tail[1])
        if tail == ["readiness"] and method == "GET":
            return 200, {"project_id": project.name, "readiness": readiness_payload(project)}

    if parts == ["runtime", "doctor"] and method == "GET":
        return 200, {"doctor": run_doctor(str(root)).model_dump(mode="json")}

    if parts == ["settings", "runtime"] and method == "GET":
        return 200, runtime_settings_payload(root)

    if parts == ["settings", "runtime"] and method == "PATCH":
        try:
            save_runtime_config(root, {key: str(value) for key, value in body.items() if value is not None})
        except ValueError as exc:
            raise ApiError(400, str(exc)) from exc
        return 200, runtime_settings_payload(root)

    raise ApiError(404, "API route not found")


def ensure_project_data(root: Path, project: Path) -> None:
    _require_project(project)
    timeline_path = project / "data" / "media_timeline.json"
    graph_path = project / "data" / "canvas_graph.json"
    # Validate every destination before the first write so initialization cannot
    # partially succeed through a hostile symlinked data directory.
    _validate_project_write_target(project, timeline_path)
    _validate_project_write_target(project, graph_path)
    with project_write_lock(project):
        timeline = ensure_media_timeline(root, project)
        graph = ensure_canvas_graph(root, project)
        _write_json(project, timeline_path, timeline)
        _write_json(project, graph_path, graph)


def validate_intake(root: Path, body: JsonDict) -> JsonDict:
    source = str(body.get("source") or body.get("source_url") or body.get("url") or "").strip()
    max_seconds = _intake_max_duration(body)
    checks: list[JsonDict] = []
    duration: float | None = None
    resolution = ""
    source_ready = bool(source)
    source_detail = "A video source is required. No sample data will be substituted."
    canonical_source = ""

    if source:
        canonical_source = _canonical_intake_source(root, source)
        path = Path(canonical_source)
        source_ready = path.exists() and path.is_file()
        source_detail = str(path)
        if source_ready:
            try:
                from .media import ffprobe_metadata, parse_video_metadata

                duration, _frame_rate, resolution, _aspect = parse_video_metadata(ffprobe_metadata(path))
            except Exception as exc:
                source_detail = f"{path} ({exc})"
    checks.append({"label": "Video source is available", "status": "ready" if source_ready else "blocked", "detail": source_detail})

    duration_ready = True
    duration_detail = "The duration will be measured from the local source during project creation."
    if duration is not None:
        duration_ready = 0 < duration <= max_seconds
        duration_detail = f"{duration:.2f}s / {resolution or 'unknown'}"
    checks.append({"label": "Duration is within the configured limit", "status": "ready" if duration_ready else "blocked", "detail": duration_detail})
    checks.append({"label": "Evidence workspace can be created", "status": "ready" if source_ready and duration_ready else "blocked"})
    return {
        "ready": all(item["status"] == "ready" for item in checks),
        "checks": checks,
        # The local React API accepts only canonical local paths.
        "normalized_source": _intake_source_receipt(canonical_source) if canonical_source else "",
    }


def create_project_from_intake(root: Path, body: JsonDict) -> JsonDict:
    source = str(body.get("source") or body.get("source_url") or body.get("url") or "").strip()
    if not source:
        raise ApiError(400, "POST /api/projects requires source")
    validation = validate_intake(root, body)
    if not validation["ready"]:
        raise ApiError(400, "Source failed intake validation", validation["checks"])
    canonical_source = _canonical_intake_source(root, source)
    max_duration_seconds = _intake_max_duration(body)
    from .pipeline import run_full_pipeline
    from .schemas import AnalysisProfile

    try:
        profile = AnalysisProfile(str(body.get("profile") or "research"))
    except ValueError:
        raise ApiError(400, "Unsupported analysis profile") from None
    result = run_full_pipeline(
        canonical_source,
        profile=profile,
        password=str(body.get("password") or "") or None,
        workspace=str(root),
        project_id=str(body.get("project_id") or "") or None,
        language=str(body.get("language") or "auto"),
        delivery_language=str(body.get("delivery_language") or "en"),
        skip_asr=_json_boolean(body, "skip_asr", default=True),
        with_vision=_json_boolean(body, "with_vision", default=False),
        max_duration_seconds=max_duration_seconds,
    ).model_dump(mode="json")
    manifest = result.get("artifacts", {}).get("project_manifest") if isinstance(result.get("artifacts"), dict) else ""
    project_id = Path(str(manifest)).parent.name if manifest else ""
    if project_id:
        ensure_project_data(root, _project_root(root, project_id))
    return {"project_id": project_id, "result": result}


def create_analysis_run_from_intake(root: Path, body: JsonDict) -> JsonDict:
    """Validate local intake, persist a run receipt, and return immediately."""
    source = str(body.get("source") or body.get("source_url") or body.get("url") or "").strip()
    if not source:
        raise ApiError(400, "POST /api/runs requires source")
    if body.get("password") not in (None, ""):
        raise ApiError(400, "Passwords are not accepted by the local background-run API")
    if _json_boolean(body, "with_vision", default=False):
        raise ApiError(400, "External vision is not available in background runs; invoke it explicitly after local analysis")
    validation = validate_intake(root, body)
    if not validation["ready"]:
        raise ApiError(400, "Source failed intake validation", validation["checks"])
    from .run_lifecycle import RunAdmissionError, start_analysis_run
    from .schemas import AnalysisProfile

    try:
        profile = AnalysisProfile(str(body.get("profile") or "research"))
        return start_analysis_run(
            root,
            {
                "source": _canonical_intake_source(root, source),
                "profile": profile.value,
                "project_id": str(body.get("project_id") or "") or None,
                "language": str(body.get("language") or "auto"),
                "delivery_language": str(body.get("delivery_language") or "en"),
                "skip_asr": _json_boolean(body, "skip_asr", default=True),
                "max_duration_seconds": _intake_max_duration(body),
            },
        )
    except RunAdmissionError as exc:
        raise ApiError(429, str(exc)) from exc
    except FileExistsError as exc:
        raise ApiError(409, str(exc)) from exc
    except ValueError as exc:
        raise ApiError(400, str(exc)) from exc


def _intake_max_duration(body: JsonDict) -> float:
    value = body.get("max_duration_seconds", 60)
    if value is None:
        value = 60
    return _finite_number(
        value,
        label="max_duration_seconds",
        minimum=0.001,
        maximum=86_400.0,
    )


def _is_http_source(source: str) -> bool:
    return source.lower().startswith(("http://", "https://"))


def _canonical_intake_source(root: Path, source: str) -> str:
    if _is_http_source(source):
        raise ApiError(
            400,
            "HTTP(S) URL ingest is disabled in the local React API; URL ingest is available only through the CLI for a trusted operator.",
        )
    path = Path(source).expanduser()
    if not path.is_absolute():
        path = root.expanduser().resolve().parent / path
    return str(path.resolve())


def _intake_source_receipt(source: str) -> str:
    return source


def _json_boolean(body: JsonDict, field: str, *, default: bool) -> bool:
    """Accept a missing field as default; reject every explicit non-boolean value."""
    if field not in body:
        return default
    value = body[field]
    if type(value) is not bool:
        raise ApiError(400, f"{field} must be a JSON boolean")
    return value


def normalize_canvas_graph(
    root: Path,
    project: Path,
    graph: JsonDict,
    *,
    current_readiness: JsonDict | None = None,
    media_timeline: JsonDict | None = None,
    deliverables: JsonDict | None = None,
) -> bool:
    before = json.dumps(graph, sort_keys=True, ensure_ascii=False)
    derived = derive_canvas_graph(
        root,
        project,
        current_readiness=current_readiness,
        media_timeline=media_timeline,
        deliverables=deliverables,
    )
    is_ads = _project_profile(project, _load_media_package(project)) == "ads"
    graph.setdefault("schema_version", 1)
    graph["project_id"] = project.name
    graph.setdefault("version", derived.get("version", "graph_001"))
    graph.setdefault("viewport", derived.get("viewport", {"x": 0, "y": 0, "zoom": 0.75}))
    graph.setdefault("manual_nodes", [])
    graph.setdefault("manual_edges", [])
    nodes = graph.setdefault("nodes", [])
    edges = graph.setdefault("edges", [])
    existing_by_id = {str(item.get("id")): item for item in nodes if isinstance(item, dict)}
    for derived_node in derived.get("nodes", []):
        if not isinstance(derived_node, dict):
            continue
        node_id = str(derived_node.get("id") or "")
        existing = existing_by_id.get(node_id)
        if existing is None:
            nodes.append(derived_node)
            existing_by_id[node_id] = derived_node
            continue
        if existing.get("source") != "manual":
            existing.setdefault("position", derived_node.get("position"))
            existing.setdefault("size", derived_node.get("size"))
            existing["type"] = derived_node.get("type", existing.get("type"))
            existing_data = existing.setdefault("data", {})
            if isinstance(existing_data, dict) and isinstance(derived_node.get("data"), dict):
                existing_data.update(derived_node["data"])
    existing_edge_ids = {str(item.get("id")) for item in edges if isinstance(item, dict)}
    for edge in derived.get("edges", []):
        if isinstance(edge, dict) and str(edge.get("id")) not in existing_edge_ids:
            edges.append(edge)
            existing_edge_ids.add(str(edge.get("id")))
    removed_node_ids = {
        str(item.get("id"))
        for item in nodes
        if isinstance(item, dict)
        and (
            item.get("source") == "generation_stub"
            or (
                item.get("type") in {"image_generation", "generated_image"}
                and isinstance(item.get("data"), dict)
                and item["data"].get("mock") is True
            )
        )
    }
    if not is_ads:
        removed_node_ids.update(
            str(item.get("id"))
            for item in nodes
            if isinstance(item, dict)
            and item.get("type") in {"prompt", "branch", "keeper_decision"}
        )
        for item in nodes:
            if isinstance(item, dict) and isinstance(item.get("data"), dict):
                item["data"].pop("prompt", None)
                item["data"].pop("remake_tip", None)
    if removed_node_ids:
        graph["nodes"] = [item for item in nodes if str(item.get("id")) not in removed_node_ids]
        edges = [
            item
            for item in edges
            if str(item.get("source")) not in removed_node_ids
            and str(item.get("target")) not in removed_node_ids
        ]
    graph["edges"] = _dedupe_edges([item for item in edges if isinstance(item, dict)])
    after = json.dumps(graph, sort_keys=True, ensure_ascii=False)
    changed = before != after
    if changed:
        # Normalization also runs for read-only GETs. Use the deterministic
        # derived receipt instead of request time so repeated reads are stable.
        graph["updated_at"] = derived.get("updated_at")
    return changed


def _project_summary(root: Path, project: ProjectManifest) -> JsonDict:
    project_root = _project_root(root, project.project_id)
    readiness = readiness_payload(project_root)
    media = _load_media_package(project_root)
    timeline = ensure_media_timeline(root, project_root)
    return {
        "project_id": project.project_id,
        "profile": _jsonable(project.profile),
        "source": project.source,
        "status": project.status,
        "root_path": str(project_root),
        "readiness": readiness,
        "media": {
            "review_video": _media_summary(root, project_root, media, readiness),
            "shot_count": len(timeline.get("shot_boundaries") or []),
            "keyframe_count": len(timeline.get("keyframes") or []),
        },
        "links": {
            "self": f"/api/projects/{quote(project.project_id)}",
            "workspace": f"/projects/{quote(project.project_id)}",
            "canvas": f"/api/projects/{quote(project.project_id)}/canvas",
            "media": f"/api/projects/{quote(project.project_id)}/media",
            "deliverables": f"/api/projects/{quote(project.project_id)}/deliverables",
        },
    }


def _project_detail(root: Path, project: Path) -> JsonDict:
    manifest = _load_manifest(project)
    canvas = ensure_canvas_graph(root, project)
    media = ensure_media_timeline(root, project)
    deliverables = deliverables_payload(root, project)
    return _project_detail_from_payloads(project, manifest, canvas, media, deliverables)


def _project_detail_from_payloads(
    project: Path,
    manifest: JsonDict,
    canvas: JsonDict,
    media: JsonDict,
    deliverables: JsonDict,
) -> JsonDict:
    return {
        "project_id": project.name,
        "manifest": manifest,
        "readiness": deliverables["readiness"],
        "canvas": {
            "version": canvas.get("version"),
            "node_count": len(canvas.get("nodes") or []),
            "edge_count": len(canvas.get("edges") or []),
            "href": f"/api/projects/{quote(project.name)}/canvas",
        },
        "media": {
            "review_video": media.get("review_video"),
            "shot_count": len(media.get("shot_boundaries") or []),
            "keyframe_count": len(media.get("keyframes") or []),
            "href": f"/api/projects/{quote(project.name)}/media",
        },
        "deliverables": {
            "artifact_count": len(deliverables.get("artifacts") or []),
            "href": f"/api/projects/{quote(project.name)}/deliverables",
        },
    }


def workspace_snapshot_payload(root: Path, project: Path) -> JsonDict:
    """Read one internally consistent workspace view under the generation lock."""
    # Every path that needs both locks acquires the in-process project lock
    # before the cross-process shots transaction lock.  Canvas mutations can
    # recompute readiness while holding the project lock, so reversing this
    # order here would deadlock a snapshot against a concurrent canvas edit.
    with project_write_lock(project):
        with advisory_file_lock(project / "data" / ".shots.lock", root=project):
            manifest = _load_manifest(project)
            current_readiness = evaluate_project_readiness(
                project,
                workspace_root=project.parent,
                _shots_lock_held=True,
            )
            generation_current, _generation_reasons = verify_report_generation_manifest(
                ProjectPaths(project),
                _shots_lock_held=True,
            )
            api_readiness = _publication_gated_readiness(
                _api_readiness(current_readiness),
                generation_current=generation_current,
            )
            media = ensure_media_timeline(
                root,
                project,
                current_readiness=current_readiness,
            )
            deliverables = deliverables_payload(
                root,
                project,
                readiness=api_readiness,
                generation_current=generation_current,
            )
            canvas = ensure_canvas_graph(
                root,
                project,
                current_readiness=current_readiness,
                media_timeline=media,
                deliverables=deliverables,
            )
            generation_id = _current_report_generation_id(
                project,
                generation_current=generation_current,
            )
            snapshot = {
                "generation_id": generation_id,
                "project": _project_detail_from_payloads(
                    project,
                    manifest,
                    canvas,
                    media,
                    deliverables,
                ),
                "canvas": canvas,
                "media": media,
                "deliverables": deliverables,
            }
            canonical = json.dumps(
                _jsonable(snapshot),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            return {
                "snapshot_id": f"sha256:{hashlib.sha256(canonical).hexdigest()}",
                **snapshot,
            }


def _current_report_generation_id(
    project: Path,
    *,
    generation_current: bool | None = None,
) -> str | None:
    if generation_current is None:
        generation_current, _reasons = verify_report_generation_manifest(ProjectPaths(project))
    if not generation_current:
        return None
    manifest = read_project_json(project, project / "project_manifest.json", {})
    generation = manifest.get("report_generation") if isinstance(manifest, dict) else None
    if not isinstance(generation, dict) or generation.get("state") != "committed":
        return None
    generation_id = generation.get("generation_id")
    if type(generation_id) is not str or generation.get("run_id") != generation_id:
        return None
    return generation_id


def ensure_canvas_graph(
    root: Path,
    project: Path,
    *,
    current_readiness: JsonDict | None = None,
    media_timeline: JsonDict | None = None,
    deliverables: JsonDict | None = None,
) -> JsonDict:
    path = project / "data" / "canvas_graph.json"
    graph = read_project_json(project, path, None)
    if graph is None:
        return derive_canvas_graph(
            root,
            project,
            current_readiness=current_readiness,
            media_timeline=media_timeline,
            deliverables=deliverables,
        )
    if not isinstance(graph, dict):
        raise ApiError(409, "Canvas graph must be a JSON object")
    normalize_canvas_graph(
        root,
        project,
        graph,
        current_readiness=current_readiness,
        media_timeline=media_timeline,
        deliverables=deliverables,
    )
    return graph


def derive_canvas_graph(
    root: Path,
    project: Path,
    *,
    current_readiness: JsonDict | None = None,
    media_timeline: JsonDict | None = None,
    deliverables: JsonDict | None = None,
) -> JsonDict:
    media = _load_media_package(project)
    shots = load_shots(project)
    lineage = load_lineage(project)
    decision = load_keeper_decision(project)
    profile = _project_profile(project, media)
    is_ads = profile == "ads"
    nodes: list[JsonDict] = []
    edges: list[JsonDict] = []

    media_summary = _media_summary(root, project, media, current_readiness)
    timeline = (
        media_timeline
        if media_timeline is not None
        else ensure_media_timeline(
            root,
            project,
            current_readiness=current_readiness,
        )
    )
    api_readiness = (
        _api_readiness(current_readiness)
        if current_readiness is not None
        else readiness_payload(project)
    )
    deliverables_data = (
        deliverables
        if deliverables is not None
        else deliverables_payload(
            root,
            project,
            readiness=api_readiness,
        )
    )
    timeline_shots = {
        str(item.get("shot_id")): item
        for item in timeline.get("shot_boundaries", [])
        if isinstance(item, dict) and item.get("shot_id")
    }
    nodes.append(
        _canvas_node(
            "source_asset_001",
            "source_asset",
            80,
            80,
            {
                "title": _source_title(media, project.name),
                "source": media.source if media else "",
                "source_type": _jsonable(media.source_type) if media else "unknown",
                "local_master_path": _path_payload(root, project, media.local_master_path if media else project / "ingest" / "master.mp4"),
            },
            source="derived",
        )
    )
    nodes.append(
        _canvas_node(
            "video_player_001",
            "video_player",
            380,
            80,
            {
                "title": "Review video",
                "review_video": media_summary,
                "playback": {"current_time": 0.0, "loop": False, "in": None, "out": None},
            },
            width=360,
            height=220,
            source="derived",
        )
    )
    nodes.append(
        _canvas_node(
            "keyframes_001",
            "keyframes",
            800,
            80,
            {
                "title": "Keyframes",
                "frames": timeline.get("keyframes", [])[:24],
                "status": "ready" if timeline.get("keyframes") else "pending",
            },
            width=320,
            height=180,
            source="derived",
        )
    )
    nodes.append(
        _canvas_node(
            "shot_sequence_001",
            "shot_sequence",
            1180,
            80,
            {
                "title": "Shot sequence",
                "shot_count": len(shots),
                "duration_seconds": media.duration_seconds if media else 0.0,
                "readiness": api_readiness,
            },
            width=300,
            height=160,
            source="derived",
        )
    )
    nodes.append(
        _canvas_node(
            "transcript_001",
            "transcript",
            80,
            360,
            {
                "title": "Transcript / audio context",
                "body": _transcript_summary(project),
                "segments": _load_transcript(project)[:12],
                "status": "review",
            },
            width=300,
            height=180,
            source="derived",
        )
    )
    nodes.append(
        _canvas_node(
            "export_artifacts_001",
            "export_artifacts",
            1180,
            360,
            {
                "title": "Deliverables",
                "artifacts": [item for item in deliverables_data.get("artifacts", [])],
            },
            width=320,
            height=180,
            source="derived",
        )
    )
    edges.extend(
        [
            _edge("edge_source_video", "source_asset_001", "video_player_001", "derived_from"),
            _edge("edge_video_keyframes", "video_player_001", "keyframes_001", "extracts"),
            _edge("edge_keyframes_shots", "keyframes_001", "shot_sequence_001", "orders"),
            _edge("edge_source_transcript", "source_asset_001", "transcript_001", "analyzes"),
            _edge("edge_video_shots", "video_player_001", "shot_sequence_001", "analyzes"),
            _edge("edge_shots_exports", "shot_sequence_001", "export_artifacts_001", "packages"),
        ]
    )

    lineage_nodes = {
        str(item.get("shot_id")): item
        for item in lineage.get("nodes", [])
        if isinstance(item, dict) and item.get("shot_id")
    }
    shot_node_ids: dict[str, str] = {}
    for index, shot in enumerate(shots):
        node_id = shot.shot_id
        shot_node_ids[shot.shot_id] = node_id
        x = 1520 + (index % 4) * 270
        y = 80 + (index // 4) * 190
        lineage_node = lineage_nodes.get(shot.shot_id, {})
        shot_payload = timeline_shots.get(shot.shot_id) or _shot_boundary_payload(
            root,
            project,
            shot,
            profile=profile,
        )
        node_data = {
            "title": lineage_node.get("title") or shot.content_summary or shot.visual_description or shot.shot_id,
            **shot_payload,
            "thumbnail": _keyframe_payload(root, project, shot.primary_frame_ref or shot.frame_ref),
            "frame_refs": [_keyframe_payload(root, project, ref) for ref in shot.frame_refs],
            "readiness_status": shot.readiness_status,
        }
        if is_ads:
            node_data["prompt"] = shot.prompt_en or shot.prompt_zh
        nodes.append(
            _canvas_node(
                node_id,
                "video_segment",
                x,
                y,
                node_data,
                width=240,
                height=150,
                source="derived",
            )
        )
        edges.append(_edge(f"edge_sequence_{shot.shot_id}", "shot_sequence_001", node_id, "contains"))

    branches = [item for item in lineage.get("branches", []) if is_ads and isinstance(item, dict)]
    prompt_nodes = [
        item
        for item in lineage.get("nodes", [])
        if is_ads and isinstance(item, dict) and item.get("type") == "prompt"
    ]
    for index, prompt in enumerate(prompt_nodes):
        prompt_id = str(prompt.get("id") or f"prompt_{index + 1:03d}")
        branch_name = str(prompt.get("branch") or "")
        nodes.append(
            _canvas_node(
                prompt_id,
                "prompt",
                420 + index * 280,
                680,
                {
                    "title": prompt.get("title") or f"{branch_name or 'branch'} prompt",
                    "branch": branch_name,
                    "status": prompt.get("status") or "draft",
                    "body": _prompt_body_for_branch(shots, branch_name),
                },
                source="derived",
            )
        )
        if shots:
            edges.append(_edge(f"edge_transcript_{prompt_id}", "transcript_001", prompt_id, "informs"))
            edges.append(_edge(f"edge_{shots[0].shot_id}_{prompt_id}", shots[0].shot_id, prompt_id, "uses_reference"))

    for index, branch in enumerate(branches):
        branch_id = str(branch.get("id") or f"branch_{index + 1:03d}")
        branch_name = str(branch.get("name") or branch_id)
        nodes.append(
            _canvas_node(
                branch_id,
                "branch",
                420 + index * 280,
                920,
                {
                    "title": branch_name,
                    "head_commit": branch.get("headCommit"),
                    "keeper": bool(branch.get("keeper")),
                },
                source="derived",
            )
        )
        prompt_id = _prompt_for_branch(prompt_nodes, branch_name)
        if prompt_id:
            edges.append(_edge(f"edge_{prompt_id}_{branch_id}", prompt_id, branch_id, "branches_to"))

    if is_ads:
        nodes.append(
            _canvas_node(
                "keeper_decision_001",
                "keeper_decision",
                1180,
                360,
                {
                    "title": "Keeper decision",
                    "keeper_branch": decision.get("keeper_branch") or None,
                    "reject_reason": decision.get("reject_reason") or None,
                    "revision_request": decision.get("revision_request") or None,
                    "updated_at": decision.get("updated_at"),
                },
                source="derived",
            )
        )
    if is_ads and decision.get("keeper_branch"):
        branch_id = _branch_id_for_name(branches, decision["keeper_branch"])
        if branch_id:
            edges.append(_edge(f"edge_{branch_id}_keeper", branch_id, "keeper_decision_001", "selected_as_keeper"))

    graph = {
        "schema_version": 1,
        "project_id": project.name,
        "version": "graph_001",
        "viewport": {"x": 0, "y": 0, "zoom": 0.75},
        "nodes": nodes,
        "edges": _dedupe_edges(edges),
        "manual_nodes": [],
        "manual_edges": [],
        "derived_from": {
            "lineage": "data/lineage.json",
            "shots": "data/shots.json",
            "media_package": "data/media_package.json",
            "readiness": "data/readiness.json",
            "reports": "reports/",
        },
        "updated_at": _derived_timestamp(project),
    }
    return graph


def update_canvas_viewport(root: Path, project: Path, body: JsonDict) -> JsonDict:
    with project_write_lock(project):
        graph = ensure_canvas_graph(root, project)
        before = json.dumps(graph, sort_keys=True, ensure_ascii=False)
        viewport = graph.setdefault("viewport", {})
        for key in ["x", "y", "zoom"]:
            if key in body:
                viewport[key] = _finite_number(
                    body[key],
                    label=f"viewport.{key}",
                    minimum=0.05 if key == "zoom" else -1_000_000,
                    maximum=8.0 if key == "zoom" else 1_000_000,
                )
        if before != json.dumps(graph, sort_keys=True, ensure_ascii=False):
            _touch_graph(graph)
            _write_json(project, project / "data" / "canvas_graph.json", graph)
        return graph


def create_canvas_node(root: Path, project: Path, body: JsonDict) -> JsonDict:
    with project_write_lock(project):
        return _create_canvas_node_locked(root, project, body)


def _create_canvas_node_locked(root: Path, project: Path, body: JsonDict) -> JsonDict:
    graph = ensure_canvas_graph(root, project)
    nodes = graph.setdefault("nodes", [])
    node_type = str(body.get("type") or "insight")
    node_id = str(body.get("id") or _next_id(node_type, [str(item.get("id")) for item in nodes if isinstance(item, dict)]))
    if _find_by_id(nodes, node_id):
        raise ApiError(409, f"Canvas node already exists: {node_id}")
    position = body.get("position") if isinstance(body.get("position"), dict) else {}
    size = body.get("size") if isinstance(body.get("size"), dict) else {}
    node = _canvas_node(
        node_id,
        node_type,
        _finite_number(position.get("x", body.get("x", 200)), label="position.x", minimum=-1_000_000, maximum=1_000_000),
        _finite_number(position.get("y", body.get("y", 200)), label="position.y", minimum=-1_000_000, maximum=1_000_000),
        body.get("data") if isinstance(body.get("data"), dict) else {"title": body.get("title") or node_type},
        width=_finite_integer(size.get("width", 240), label="size.width", minimum=1, maximum=10_000),
        height=_finite_integer(size.get("height", 140), label="size.height", minimum=1, maximum=10_000),
        source="manual",
    )
    for key in ["selected", "parent_id"]:
        if key in body:
            node[key] = body[key]
    nodes.append(node)
    graph.setdefault("manual_nodes", []).append(node_id)
    _touch_graph(graph)
    _write_json(project, project / "data" / "canvas_graph.json", graph)
    return {"node": node, "canvas": _canvas_meta(graph)}


def update_canvas_node(root: Path, project: Path, node_id: str, body: JsonDict) -> JsonDict:
    with project_write_lock(project):
        return _update_canvas_node_locked(root, project, node_id, body)


def _update_canvas_node_locked(root: Path, project: Path, node_id: str, body: JsonDict) -> JsonDict:
    graph = ensure_canvas_graph(root, project)
    node = _find_by_id(graph.get("nodes", []), node_id)
    if not node:
        raise ApiError(404, f"Canvas node not found: {node_id}")
    before = json.dumps(node, sort_keys=True, ensure_ascii=False)
    for key in ["type", "selected", "parent_id"]:
        if key in body:
            node[key] = body[key]
    if isinstance(body.get("position"), dict):
        target = node.setdefault("position", {})
        for key in ["x", "y"]:
            if key in body["position"]:
                target[key] = _finite_number(
                    body["position"][key],
                    label=f"position.{key}",
                    minimum=-1_000_000,
                    maximum=1_000_000,
                )
    if isinstance(body.get("size"), dict):
        target = node.setdefault("size", {})
        for key in ["width", "height"]:
            if key in body["size"]:
                target[key] = _finite_integer(
                    body["size"][key],
                    label=f"size.{key}",
                    minimum=1,
                    maximum=10_000,
                )
    if isinstance(body.get("data"), dict):
        target = node.setdefault("data", {})
        if isinstance(target, dict):
            target.update(body["data"])
        else:
            node["data"] = body["data"]
    if before != json.dumps(node, sort_keys=True, ensure_ascii=False):
        node["updated_at"] = _now()
        _touch_graph(graph)
        _write_json(project, project / "data" / "canvas_graph.json", graph)
    return {"node": node, "canvas": _canvas_meta(graph)}


def delete_canvas_node(root: Path, project: Path, node_id: str) -> JsonDict:
    with project_write_lock(project):
        return _delete_canvas_node_locked(root, project, node_id)


def _delete_canvas_node_locked(root: Path, project: Path, node_id: str) -> JsonDict:
    graph = ensure_canvas_graph(root, project)
    nodes = graph.setdefault("nodes", [])
    before = len(nodes)
    graph["nodes"] = [item for item in nodes if not (isinstance(item, dict) and item.get("id") == node_id)]
    if len(graph["nodes"]) == before:
        raise ApiError(404, f"Canvas node not found: {node_id}")
    graph["edges"] = [
        item
        for item in graph.get("edges", [])
        if isinstance(item, dict) and item.get("source") != node_id and item.get("target") != node_id
    ]
    graph["manual_nodes"] = [item for item in graph.get("manual_nodes", []) if item != node_id]
    _touch_graph(graph)
    _write_json(project, project / "data" / "canvas_graph.json", graph)
    return {"deleted": node_id, "canvas": _canvas_meta(graph)}


def create_canvas_edge(root: Path, project: Path, body: JsonDict) -> JsonDict:
    with project_write_lock(project):
        return _create_canvas_edge_locked(root, project, body)


def _create_canvas_edge_locked(root: Path, project: Path, body: JsonDict) -> JsonDict:
    graph = ensure_canvas_graph(root, project)
    source = str(body.get("source") or "")
    target = str(body.get("target") or "")
    if not source or not target:
        raise ApiError(400, "Canvas edge requires source and target")
    node_ids = {str(item.get("id")) for item in graph.get("nodes", []) if isinstance(item, dict)}
    if source not in node_ids or target not in node_ids:
        raise ApiError(400, "Canvas edge source/target must exist")
    edges = graph.setdefault("edges", [])
    edge_id = str(body.get("id") or _next_id("edge", [str(item.get("id")) for item in edges if isinstance(item, dict)]))
    if _find_by_id(edges, edge_id):
        raise ApiError(409, f"Canvas edge already exists: {edge_id}")
    edge = _edge(edge_id, source, target, str(body.get("type") or "relates_to"))
    edge["data"] = body.get("data") if isinstance(body.get("data"), dict) else {}
    edges.append(edge)
    graph.setdefault("manual_edges", []).append(edge_id)
    _touch_graph(graph)
    _write_json(project, project / "data" / "canvas_graph.json", graph)
    return {"edge": edge, "canvas": _canvas_meta(graph)}


def delete_canvas_edge(root: Path, project: Path, edge_id: str) -> JsonDict:
    with project_write_lock(project):
        return _delete_canvas_edge_locked(root, project, edge_id)


def _delete_canvas_edge_locked(root: Path, project: Path, edge_id: str) -> JsonDict:
    graph = ensure_canvas_graph(root, project)
    before = len(graph.get("edges", []))
    graph["edges"] = [item for item in graph.get("edges", []) if not (isinstance(item, dict) and item.get("id") == edge_id)]
    if len(graph["edges"]) == before:
        raise ApiError(404, f"Canvas edge not found: {edge_id}")
    graph["manual_edges"] = [item for item in graph.get("manual_edges", []) if item != edge_id]
    _touch_graph(graph)
    _write_json(project, project / "data" / "canvas_graph.json", graph)
    return {"deleted": edge_id, "canvas": _canvas_meta(graph)}


def ensure_media_timeline(
    root: Path,
    project: Path,
    *,
    current_readiness: JsonDict | None = None,
) -> JsonDict:
    path = project / "data" / "media_timeline.json"
    timeline = derive_media_timeline(
        root,
        project,
        current_readiness=current_readiness,
    )
    data = read_project_json(project, path, None)
    if data is None:
        return timeline
    if not isinstance(data, dict):
        raise ApiError(409, "Media timeline must be a JSON object")
    timeline["markers"] = data.get("markers") if isinstance(data.get("markers"), list) else []
    timeline["segments"] = data.get("segments") if isinstance(data.get("segments"), list) else []
    timeline["updated_at"] = data.get("updated_at") or timeline.get("updated_at")
    return timeline


def derive_media_timeline(
    root: Path,
    project: Path,
    *,
    current_readiness: JsonDict | None = None,
) -> JsonDict:
    media = _load_media_package(project)
    shots = load_shots(project)
    profile = _project_profile(project, media)
    current = current_readiness if current_readiness is not None else evaluate_project_readiness(
        project,
        workspace_root=project.parent,
    )
    readiness_by_shot = {
        str(item.get("shot_id")): item
        for item in current.get("shot_results", [])
        if isinstance(item, dict) and item.get("shot_id")
    }
    shot_boundaries: list[JsonDict] = []
    keyframes: list[JsonDict] = []
    for shot in shots:
        shot_payload = _shot_boundary_payload(
            root,
            project,
            shot,
            profile=profile,
            readiness_result=readiness_by_shot.get(shot.shot_id),
        )
        shot_boundaries.append(shot_payload)
        keyframes.extend(shot_payload["keyframes"])
    return {
        "schema_version": 1,
        "project_id": project.name,
        "review_video": _media_summary(root, project, media, current),
        "source": {
            "source": media.source if media else "",
            "source_type": _jsonable(media.source_type) if media else "unknown",
            "local_master": _path_payload(root, project, media.local_master_path if media else project / "ingest" / "master.mp4"),
        },
        "shot_boundaries": shot_boundaries,
        "keyframes": keyframes,
        "markers": [],
        "segments": [],
        "updated_at": _derived_timestamp(project),
    }


def review_video_payload(root: Path, project: Path) -> JsonDict:
    media = ensure_media_timeline(root, project).get("review_video") or {}
    return {"project_id": project.name, "review_video": media}


def frame_at_time(root: Path, project: Path, seconds: object) -> JsonDict:
    timeline = ensure_media_timeline(root, project)
    review = timeline.get("review_video") if isinstance(timeline.get("review_video"), dict) else {}
    duration = _finite_number(
        review.get("duration_seconds") or 0.0,
        label="duration",
        minimum=0.0,
        maximum=86_400.0,
    )
    seconds = _finite_number(
        seconds,
        label="time",
        minimum=0.0,
        maximum=duration if duration > 0 else 86_400.0,
    )
    nearest = _nearest_keyframe(timeline, seconds)
    return {
        "project_id": project.name,
        "time": seconds,
        "frame": nearest,
        "extracted": False,
        "fallback_keyframe": nearest,
        "error": None,
    }


def create_frame_marker(root: Path, project: Path, body: JsonDict) -> JsonDict:
    with project_write_lock(project):
        return _create_frame_marker_locked(root, project, body)


def _create_frame_marker_locked(root: Path, project: Path, body: JsonDict) -> JsonDict:
    timeline = ensure_media_timeline(root, project)
    markers = timeline.setdefault("markers", [])
    marker_id = str(body.get("id") or _next_id("frame_marker", [str(item.get("id")) for item in markers if isinstance(item, dict)]))
    review = timeline.get("review_video") if isinstance(timeline.get("review_video"), dict) else {}
    duration = _finite_number(review.get("duration_seconds") or 0.0, label="duration", minimum=0.0, maximum=86_400.0)
    seconds = _finite_number(
        body.get("time", body.get("source_frame_time", 0.0)) or 0.0,
        label="time",
        minimum=0.0,
        maximum=duration if duration > 0 else 86_400.0,
    )
    shot = _shot_at_time(timeline, seconds)
    frame = frame_at_time(root, project, seconds)
    marker = {
        "id": marker_id,
        "time": seconds,
        "label": str(body.get("label") or f"Frame {seconds:.3f}s"),
        "shot_id": body.get("shot_id") or (shot or {}).get("id"),
        "frame_ref": (frame.get("frame") or {}).get("relative_path") if isinstance(frame.get("frame"), dict) else None,
        "frame": frame.get("frame"),
        "canvas_node_id": marker_id,
        "created_at": _now(),
    }
    markers.append(marker)
    timeline["updated_at"] = _now()
    _write_json(project, project / "data" / "media_timeline.json", timeline)
    graph = ensure_canvas_graph(root, project)
    if not _find_by_id(graph.get("nodes", []), marker_id):
        graph.setdefault("nodes", []).append(
            _canvas_node(
                marker_id,
                "frame_marker",
                1180,
                80 + len(markers) * 170,
                {"title": marker["label"], "marker": marker},
                source="marker",
            )
        )
        source = str(marker.get("shot_id") or "video_player_001")
        if not _find_by_id(graph.get("nodes", []), source):
            source = "video_player_001"
        graph.setdefault("edges", []).append(_edge(f"edge_{source}_{marker_id}", source, marker_id, "marks_frame"))
        _touch_graph(graph)
        _write_json(project, project / "data" / "canvas_graph.json", graph)
    return {"marker": marker, "canvas": _canvas_meta(graph)}


def create_media_segment(root: Path, project: Path, body: JsonDict) -> JsonDict:
    with project_write_lock(project):
        return _create_media_segment_locked(root, project, body)


def _create_media_segment_locked(root: Path, project: Path, body: JsonDict) -> JsonDict:
    timeline = ensure_media_timeline(root, project)
    segments = timeline.setdefault("segments", [])
    segment_id = str(body.get("id") or _next_id("segment", [str(item.get("id")) for item in segments if isinstance(item, dict)]))
    review = timeline.get("review_video") if isinstance(timeline.get("review_video"), dict) else {}
    duration = _finite_number(review.get("duration_seconds") or 0.0, label="duration", minimum=0.0, maximum=86_400.0)
    maximum = duration if duration > 0 else 86_400.0
    start = _finite_number(body.get("start_time", body.get("in", 0.0)) or 0.0, label="start_time", minimum=0.0, maximum=maximum)
    end = _finite_number(body.get("end_time", body.get("out", start)) or start, label="end_time", minimum=0.0, maximum=maximum)
    if end <= start:
        raise ApiError(400, "Segment end_time must be greater than start_time")
    canvas_node_id = str(body.get("canvas_node_id") or f"video_{segment_id}")
    segment = {
        "id": segment_id,
        "start_time": start,
        "end_time": end,
        "duration": round(end - start, 3),
        "label": str(body.get("label") or f"Segment {start:.3f}-{end:.3f}s"),
        "shot_ids": [item["id"] for item in _shots_overlapping(timeline, start, end)],
        "canvas_node_id": canvas_node_id,
        "created_at": _now(),
    }
    segments.append(segment)
    timeline["updated_at"] = _now()
    _write_json(project, project / "data" / "media_timeline.json", timeline)
    graph = ensure_canvas_graph(root, project)
    if not _find_by_id(graph.get("nodes", []), canvas_node_id):
        graph.setdefault("nodes", []).append(
            _canvas_node(
                canvas_node_id,
                "video_segment",
                1480,
                80 + len(segments) * 170,
                {"title": segment["label"], "segment": segment},
                source="segment",
            )
        )
        graph.setdefault("edges", []).append(_edge(f"edge_video_{canvas_node_id}", "video_player_001", canvas_node_id, "clips_segment"))
        _touch_graph(graph)
        _write_json(project, project / "data" / "canvas_graph.json", graph)
    return {"segment": segment, "canvas": _canvas_meta(graph)}


def deliverables_payload(
    root: Path,
    project: Path,
    *,
    readiness: JsonDict | None = None,
    generation_current: bool | None = None,
) -> JsonDict:
    if generation_current is None:
        generation_current, _generation_reasons = verify_report_generation_manifest(ProjectPaths(project))
    current_readiness = _publication_gated_readiness(
        readiness if readiness is not None else readiness_payload(
            project,
            generation_current=generation_current,
        ),
        generation_current=generation_current,
    )
    artifacts = []
    seen: set[str] = set()
    for group, artifact_id, label, path in _deliverable_specs(
        project,
        readiness=current_readiness,
        generation_current=generation_current,
    ):
        if artifact_id in seen:
            continue
        seen.add(artifact_id)
        info = _deliverable_file_info(project, path)
        present = info is not None and info.st_size > 0
        sensitive = is_professional_export_path(project, path) or artifact_id in PROFESSIONAL_EXPORT_IDS
        blocked = sensitive and not current_readiness.get("professional_export_allowed")
        artifact = {
            "id": artifact_id,
            "group": group,
            "label": label,
            "path": str(path),
            "relative_path": _rel_to_root(root, path) if present else str(path.name),
            # The URL is a receipt, not an authorization decision. Both preview
            # and download handlers independently re-evaluate the current gate.
            "url": _file_url(root, path) if present and not blocked else None,
            "content_type": mimetypes.guess_type(path.name)[0] or _content_type_for_path(path),
            "present": present,
            "size_bytes": info.st_size if info is not None else 0,
            "preview_url": f"/api/projects/{quote(project.name)}/deliverables/{quote(artifact_id)}/preview",
        }
        if blocked:
            artifact["readiness_status"] = "blocked"
        elif group == "draft":
            artifact["readiness_status"] = "unverified"
        else:
            artifact["readiness_status"] = "available" if present else "missing"
        artifacts.append(artifact)
    return {
        "project_id": project.name,
        "readiness": current_readiness,
        "artifacts": artifacts,
        "export": {
            "allowed": bool(current_readiness.get("professional_export_allowed")),
            "blocked_reasons": current_readiness.get("reasons") or [],
        },
    }


def deliverable_preview(root: Path, project: Path, artifact_id: str) -> JsonDict:
    # Report generation, gate authorization, and descriptor-backed reads share
    # one transaction boundary.  Otherwise a publisher could replace the
    # report after it was authorized but before the preview opened it.
    with advisory_file_lock(project / "data" / ".shots.lock", root=project):
        current_readiness = evaluate_project_readiness(
            project,
            workspace_root=project.parent,
            _shots_lock_held=True,
        )
        generation_current, _generation_reasons = verify_report_generation_manifest(
            ProjectPaths(project),
            _shots_lock_held=True,
        )
        api_readiness = _publication_gated_readiness(
            _api_readiness(current_readiness),
            generation_current=generation_current,
        )
        matches = [
            item
            for item in _deliverable_specs(
                project,
                readiness=api_readiness,
                generation_current=generation_current,
            )
            if item[1] == artifact_id
        ]
        if not matches:
            raise ApiError(404, f"Deliverable not found: {artifact_id}")
        paths = {str(item[3]) for item in matches}
        if len(paths) != 1:
            raise ApiError(409, f"Deliverable id is ambiguous: {artifact_id}")
        _group, _artifact_id, label, path = matches[0]
        sensitive = artifact_id in PROFESSIONAL_EXPORT_IDS or any(
            is_professional_export_path(project, item[3]) for item in matches
        )
        if sensitive and not api_readiness.get("professional_export_allowed"):
            raise ApiError(403, "Professional export is blocked until current readiness checks pass")
        info = _deliverable_file_info(project, path)
        if info is None or info.st_size <= 0:
            raise ApiError(404, f"Deliverable file missing: {artifact_id}")
        content_type = mimetypes.guess_type(path.name)[0] or _content_type_for_path(path)
        if path.suffix.lower() in {".json", ".md", ".txt", ".csv", ".html"}:
            try:
                raw = read_regular_bytes(
                    path,
                    root=project,
                    max_bytes=MAX_DELIVERABLE_PREVIEW_BYTES + 1,
                )
            except ValueError as exc:
                if "exceeds" not in str(exc):
                    raise ApiError(404, f"Deliverable file is unsafe: {artifact_id}") from None
                # Bounded preview intentionally permits larger source files
                # while still reading only a safe descriptor's prefix.
                from .media import _open_regular_no_symlinks

                with _open_regular_no_symlinks(path) as descriptor:
                    raw = os.read(descriptor, MAX_DELIVERABLE_PREVIEW_BYTES + 1)
            truncated = len(raw) > MAX_DELIVERABLE_PREVIEW_BYTES
            text = raw[:MAX_DELIVERABLE_PREVIEW_BYTES].decode("utf-8", errors="replace")
            return {
                "id": artifact_id,
                "label": label,
                "content_type": content_type,
                "text": text,
                "truncated": truncated,
                "url": _file_url(root, path),
            }
        return {
            "id": artifact_id,
            "label": label,
            "content_type": content_type,
            "url": _file_url(root, path),
            "binary": True,
        }


def readiness_payload(
    project: Path,
    *,
    current_readiness: JsonDict | None = None,
    generation_current: bool | None = None,
) -> JsonDict:
    current = current_readiness if current_readiness is not None else evaluate_project_readiness(
        project,
        workspace_root=project.parent,
    )
    if generation_current is None:
        generation_current, _generation_reasons = verify_report_generation_manifest(ProjectPaths(project))
    return _publication_gated_readiness(
        _api_readiness(current),
        generation_current=generation_current,
    )


def _publication_gated_readiness(readiness: JsonDict, *, generation_current: bool) -> JsonDict:
    """Expose delivery readiness only for a current committed report package."""
    result = dict(readiness)
    if generation_current or result.get("professional_export_allowed") is not True:
        return result
    result["evidence_status"] = result.get("status")
    result["status"] = "blocked"
    result["professional_export_allowed"] = False
    reasons = list(result.get("reasons") or [])
    if FINALIZATION_REQUIRED_REASON not in reasons:
        reasons.append(FINALIZATION_REQUIRED_REASON)
    result["reasons"] = reasons
    result["summary"] = "Human review is ready; finalize the package before professional export."
    checks = [dict(item) for item in result.get("checks") or [] if isinstance(item, dict)]
    for item in checks:
        if item.get("id") == "readiness":
            item["status"] = "blocked"
    result["checks"] = checks
    return result


def runtime_settings_payload(root: Path) -> JsonDict:
    from .run_lifecycle import MAX_ACTIVE_ANALYSIS_RUNS, MIN_WORKSPACE_FREE_BYTES

    config = load_runtime_config(root)
    return {
        "workspace_path": str(root),
        "vision_provider": config.vision_provider,
        "openai": {
            "api_key_configured": bool(config.openai_api_key),
            "api_key_masked": mask_secret(config.openai_api_key),
            "base_url": config.openai_base_url,
            "model": config.openai_model,
        },
        "minimax": {
            "api_key_configured": bool(config.minimax_api_key),
            "api_key_masked": mask_secret(config.minimax_api_key),
            "api_host": config.minimax_api_host,
        },
        "bridgedeck": {
            "base_url": config.bridgedeck_base_url,
            "model": config.bridgedeck_model,
            "authentication": "local_bridge_owned",
            "upstream_token_limit_enforced": False,
            "live_inference_verified": False,
        },
        "audio_adapter": {
            "configured": bool(config.audio_adapter_executable),
            "timeout_seconds": config.audio_adapter_timeout_seconds,
            "live_inference_verified": False,
            "baseline_fallback": True,
        },
        "resource_limits": {
            "max_active_analysis_runs": MAX_ACTIVE_ANALYSIS_RUNS,
            "workspace_free_space_reserve_bytes": MIN_WORKSPACE_FREE_BYTES,
            "analysis_subprocess_cancellation": "process_group",
            "export_renderer_log_limit_bytes": 2 * 1024 * 1024,
        },
        "readiness_rules": {
            "critical_empty_rate_max": 0.2,
            "average_visual_confidence_min": 0.65,
            "low_boundary_confidence_rate_max": 0.3,
        },
    }


def load_shots(project: Path) -> list[Shot]:
    path = project / "data" / "shots.json"
    data = read_project_json(project, path, None)
    if data is None:
        return []
    try:
        if not isinstance(data, list):
            raise ApiError(409, "Shot receipt must be a JSON array")
        return [Shot.model_validate(item) for item in data]
    except ApiError:
        raise
    except Exception:
        raise ApiError(409, "Shot receipt failed schema validation") from None


def update_shot_review(root: Path, project: Path, shot_id: str, body: JsonDict) -> JsonDict:
    """Persist one explicit local-operator review with optimistic concurrency.

    Review writes deliberately do not regenerate reports. The caller receives a
    fail-closed readiness snapshot and must invoke the report endpoint as a
    separate, observable step. This keeps a saved human correction recoverable
    even when report generation later fails.
    """
    unknown = sorted(set(body) - SHOT_REVIEW_FIELDS)
    if unknown:
        raise ApiError(400, "Shot review contains unsupported fields", unknown)
    expected_digest = body.get("expected_shot_digest")
    if type(expected_digest) is not str or not re.fullmatch(r"sha256:[0-9a-f]{64}", expected_digest):
        raise ApiError(400, "expected_shot_digest must be a sha256 edit version")
    status = body.get("readiness_status")
    if type(status) is not str or status not in SHOT_REVIEW_STATUS_VALUES:
        raise ApiError(400, "readiness_status must be blocked, ready, or rejected")
    text_updates: dict[str, str] = {}
    for field in SHOT_REVIEW_TEXT_FIELDS:
        if field not in body:
            continue
        value = body[field]
        if type(value) is not str:
            raise ApiError(400, f"{field} must be a string")
        normalized = value.strip()
        if len(normalized.encode("utf-8")) > MAX_SHOT_REVIEW_TEXT_BYTES:
            raise ApiError(400, f"{field} exceeds {MAX_SHOT_REVIEW_TEXT_BYTES} UTF-8 bytes")
        text_updates[field] = normalized
    confidence = None
    if "visual_confidence" in body:
        if isinstance(body["visual_confidence"], bool):
            raise ApiError(400, "visual_confidence must be a number")
        confidence = _finite_number(
            body["visual_confidence"],
            label="visual_confidence",
            minimum=0.0,
            maximum=1.0,
        )
    boundary_reviewed = body.get("boundary_reviewed")
    if "boundary_reviewed" in body and type(boundary_reviewed) is not bool:
        raise ApiError(400, "boundary_reviewed must be a JSON boolean")

    with project_write_lock(project):
        with advisory_file_lock(project / "data" / ".shots.lock", root=project):
            shots = load_shots(project)
            selected = next((shot for shot in shots if shot.shot_id == shot_id), None)
            if selected is None:
                raise ApiError(404, f"Shot not found: {shot_id}")
            current_digest = _shot_edit_version(selected)
            if current_digest != expected_digest:
                raise ApiError(
                    409,
                    "Shot changed after it was loaded; refresh before saving",
                    {"current_shot_digest": current_digest},
                )
            boundary_receipt = None
            if type(boundary_reviewed) is bool:
                try:
                    binding = visual_generation_binding(ProjectPaths(project), shots)
                except ValueError as exc:
                    raise ApiError(409, "Current visual generation cannot accept a boundary review", str(exc)) from None
                existing_review = validate_boundary_review_receipt(project, shots, binding)
                reviewed_ids = (
                    set(existing_review["reviewed_shot_ids"])
                    if existing_review["valid"]
                    else set()
                )
                if boundary_reviewed:
                    reviewed_ids.add(shot_id)
                else:
                    reviewed_ids.discard(shot_id)
                boundary_receipt = build_boundary_review_receipt(project, shots, binding, reviewed_ids)
            # Invalidate the publication marker before changing review state.
            # This keeps the project fail-closed even if a later write fails,
            # and prevents byte-identical restoration from reviving an older
            # package without an explicit report finalization.
            _invalidate_report_for_review(project, shot_id)
            for field, value in text_updates.items():
                setattr(selected, field, value)
            if confidence is not None:
                selected.visual_confidence = confidence
                selected.confidence = max(selected.confidence, confidence)
            selected.readiness_status = status
            selected.annotation_source = "human"
            if "review_notes" not in text_updates:
                selected.review_notes = "operator reviewed in the primary workspace"
            changed = write_project_json(project, project / "data" / "shots.json", shots)
            if boundary_receipt is not None:
                write_project_json(
                    project,
                    project / "data" / "boundary_review.json",
                    boundary_receipt,
                )
            saved_digest = _shot_edit_version(selected)

    current = evaluate_project_readiness(project, workspace_root=root)
    readiness_by_shot = {
        str(item.get("shot_id")): item
        for item in current.get("shot_results", [])
        if isinstance(item, dict) and item.get("shot_id")
    }
    return {
        "project_id": project.name,
        "shot_id": shot_id,
        "review_saved": True,
        "changed": changed,
        "report_regeneration_required": True,
        "shot": _shot_boundary_payload(
            root,
            project,
            selected,
            profile=_project_profile(project),
            readiness_result=readiness_by_shot.get(shot_id),
        ),
        "readiness": _publication_gated_readiness(
            _api_readiness(current),
            generation_current=False,
        ),
        "saved_shot_digest": saved_digest,
    }


def _audio_review_action(project: Path, action: str, event_id: str | None, query: JsonDict, body: JsonDict) -> JsonDict:
    from .audio_review import apply_audio_review, get_audio_event, read_audio_review

    if event_id is not None:
        # Decode only this segment once; the service validates the resulting ID.
        # Decoding the full path could turn escaped slashes into route structure.
        try:
            event_id = unquote(event_id, errors="strict")
        except UnicodeDecodeError as exc:
            raise ApiError(400, "Invalid audio event identifier", {"code": "invalid_event_id"}) from exc
    paths = ProjectPaths(project)
    if action == "apply":
        if query:
            raise ApiError(400, "Audio review mutations do not accept query options", {"code": "invalid_query"})
        return apply_audio_review(paths, event_id, body)
    if body:
        raise ApiError(400, "Audio queries do not accept a request body", {"code": "invalid_query"})
    if action == "show":
        if set(query) - {"expected_generation_id"}:
            raise ApiError(400, "Single-event lookup accepts only expected_generation_id", {"code": "invalid_query"})
        return get_audio_event(paths, event_id, query.get("expected_generation_id"))
    return read_audio_review(paths, query)


def _codex_analysis_action(project: Path, action: str, body: JsonDict) -> JsonDict:
    from .codex_analysis import CodexAnalysisConflict, apply_codex_analysis, codex_analysis_status, prepare_codex_analysis

    try:
        paths = ProjectPaths(project)
        if action == "apply":
            return apply_codex_analysis(paths, body)
        if body:
            raise ApiError(400, "Codex prepare/status does not accept configuration or credentials")
        return prepare_codex_analysis(paths) if action == "prepare" else codex_analysis_status(paths)
    except CodexAnalysisConflict as exc:
        raise ApiError(409, str(exc)) from exc
    except (OSError, ValueError) as exc:
        raise ApiError(400, str(exc)) from exc


def _client_export_action(project: Path, action: str, body: JsonDict) -> JsonDict:
    from .export_service import (
        ClientExportConflict,
        ClientExportError,
        cancel_client_export,
        delete_saved_export,
        generate_client_export,
        pdf_runtime_from_environment,
        read_export_center,
        read_export_state,
        recover_client_exports,
        save_current_export,
    )

    paths = ProjectPaths(project)
    try:
        if action == "status":
            if body:
                raise ApiError(400, "Export status does not accept a request body")
            return read_export_center(paths)
        if action == "state":
            if body:
                raise ApiError(400, "Export state does not accept a request body")
            # This atomic state-file read intentionally avoids the long export
            # transaction lock so a separate request can observe rendering and
            # submit cooperative cancellation while a renderer is running.
            return read_export_state(paths)
        if action == "generate":
            if type(body) is not dict or set(body) != {"formats", "settings", "idempotency_key"}:
                raise ApiError(400, "Export request fields are invalid")
            if type(body["settings"]) is not dict:
                raise ApiError(400, "Export settings must be an object")
            return generate_client_export(
                paths,
                formats=body["formats"],
                settings=body["settings"],
                idempotency_key=body["idempotency_key"],
                pdf_runtime=pdf_runtime_from_environment(),
            )
        if action == "cancel":
            if type(body) is not dict or set(body) != {"request_digest"}:
                raise ApiError(400, "Export cancel fields are invalid")
            return cancel_client_export(paths, body["request_digest"])
        if action in {"save", "delete"}:
            if type(body) is not dict or set(body) != {"version_id"}:
                raise ApiError(400, "Export version fields are invalid")
            return (
                save_current_export(paths, body["version_id"])
                if action == "save"
                else delete_saved_export(paths, body["version_id"])
            )
        if body:
            raise ApiError(400, "Export recovery does not accept a request body")
        return recover_client_exports(paths)
    except ApiError:
        raise
    except ClientExportConflict as exc:
        raise ApiError(409, str(exc)) from exc
    except ClientExportError as exc:
        raise ApiError(422, str(exc)) from exc
    except ValueError as exc:
        raise ApiError(400, str(exc)) from exc


def _invalidate_report_for_review(
    project: Path, shot_id: str, *, reason: str = "shot_review_saved", audio_event_id: str | None = None
) -> None:
    """Remove the publication commit record before persisting a human review."""
    path = project / "project_manifest.json"
    manifest = read_project_json(project, path, None)
    if not isinstance(manifest, dict):
        raise ApiError(409, "Project manifest must be a JSON object")
    try:
        validated = ProjectManifest.model_validate(manifest)
    except Exception:
        raise ApiError(409, "Project manifest failed schema validation") from None
    if validated.project_id != project.name:
        raise ApiError(409, "Project manifest does not match the project directory")
    paths = ProjectPaths(project)
    try:
        # Missing registries are valid legacy state. A malformed registry must
        # block mutation before the previous publication is invalidated.
        load_artifact_registry(paths)
    except ValueError as exc:
        raise ApiError(409, "Artifact registry failed validation", str(exc)) from None
    invalidated = dict(manifest)
    invalidated["status"] = "review_pending"
    invalidated["artifacts"] = {}
    invalidated.pop("report_generation", None)
    invalidated["report_invalidation"] = {
        "schema_version": REPORT_INVALIDATION_SCHEMA_VERSION,
        "reason": reason,
        "shot_id": shot_id,
        "requires_finalize": True,
    }
    if audio_event_id is not None:
        invalidated["report_invalidation"]["audio_event_id"] = audio_event_id
    write_project_json(project, path, invalidated)
    try:
        mark_artifacts_stale(
            paths,
            scopes={"report", "client_export"},
            reason=reason,
        )
    except ValueError as exc:
        # The manifest is already review_pending, so no stale report can be
        # authorized even if the secondary registry update failed.
        raise ApiError(409, "Artifact registry update failed", str(exc)) from None


def regenerate_project_report(root: Path, project: Path) -> JsonDict:
    """Regenerate a committed package and return one fresh workspace snapshot."""
    from .pipeline import run_report

    with project_write_lock(project):
        readiness = evaluate_project_readiness(
            project,
            workspace_root=root,
            require_persisted_receipt=False,
        )
        if readiness.get("professional_export_allowed") is not True:
            raise ApiError(
                409,
                "Project is not ready to finalize",
                {"reasons": list(readiness.get("reasons", []))},
            )
        result = run_report(project.name, workspace=str(root)).model_dump(mode="json")
        workspace = workspace_snapshot_payload(root, project)
        final_readiness = workspace["project"].get("readiness") or {}
        if final_readiness.get("professional_export_allowed") is not True:
            raise ApiError(
                409,
                "Project changed during finalization or remains blocked",
                {"reasons": list(final_readiness.get("reasons", []))},
            )
        return {
            "project_id": project.name,
            "report_regenerated": True,
            "result": result,
            "workspace": workspace,
        }


def load_lineage(project: Path) -> JsonDict:
    path = project / "data" / "lineage.json"
    data = read_project_json(project, path, {})
    if not isinstance(data, dict):
        raise ApiError(409, "Lineage receipt must be a JSON object")
    return data


def load_keeper_decision(project: Path) -> JsonDict:
    data = read_project_json(project, project / "data" / "keeper_decision.json", {})
    if not isinstance(data, dict):
        raise ApiError(409, "Keeper decision receipt must be a JSON object")
    return data


def _load_manifest(project: Path) -> JsonDict:
    return load_project_manifest(project).model_dump(mode="json")


def _load_media_package(project: Path) -> CanonicalMediaPackage | None:
    path = project / "data" / "media_package.json"
    data = read_project_json(project, path, None)
    if data is None:
        return None
    try:
        return CanonicalMediaPackage.model_validate(data)
    except Exception:
        raise ApiError(409, "Media receipt failed schema validation") from None


def _project_profile(project: Path, media: CanonicalMediaPackage | None = None) -> str:
    try:
        profile = load_project_manifest(project).profile
        return str(getattr(profile, "value", profile))
    except ApiError as exc:
        if exc.status != 404:
            raise
    if media is not None:
        profile = media.analysis_profile
        return str(getattr(profile, "value", profile))
    return "research"


def _media_summary(
    root: Path,
    project: Path,
    media: CanonicalMediaPackage | None,
    readiness: JsonDict | None = None,
) -> JsonDict:
    if not media:
        return {
            "path": None,
            "url": None,
            "duration_seconds": 0.0,
            "frame_rate": 0.0,
            "resolution": "unknown",
            "width": 0,
            "height": 0,
            "aspect_ratio": 0.0,
        }
    width, height = _parse_resolution(media.resolution)
    current = readiness or evaluate_project_readiness(project, workspace_root=project.parent)
    binding = current.get("media_binding") if isinstance(current.get("media_binding"), dict) else {}
    media_bound = binding.get("status") == "bound"
    payload = _path_payload(root, project, media.review_copy_path)
    if not media_bound:
        payload["url"] = None
    return {
        **payload,
        "duration_seconds": media.duration_seconds,
        "frame_rate": media.frame_rate,
        "resolution": media.resolution,
        "width": width,
        "height": height,
        "aspect_ratio": media.aspect_ratio,
        "audio": _path_payload(root, project, media.audio_path),
        "status": media.status,
        "binding_status": binding.get("status") or "unbound",
        "binding_valid": media_bound,
    }


def _source_title(media: CanonicalMediaPackage | None, fallback: str) -> str:
    if not media:
        return fallback
    yt = media.metadata.get("yt_dlp") if isinstance(media.metadata, dict) else {}
    if isinstance(yt, dict) and yt.get("title"):
        return str(yt["title"])
    return Path(media.source).name or fallback


def _path_payload(root: Path, project: Path, value: str | Path) -> JsonDict:
    path = _safe_project_path(project, value)
    if path is None:
        return {
            "path": None,
            "relative_path": None,
            "url": None,
            "present": False,
            "boundary_error": "path outside project root",
        }
    return {
        "path": str(path),
        "relative_path": _rel_to_root(root, path),
        "url": _file_url(root, path) if path.is_file() else None,
        "present": path.is_file(),
    }


def _safe_project_path(project: Path, value: str | Path) -> Path | None:
    project_root = project.resolve()
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = project_root / candidate
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError):
        return None
    return resolved if resolved.is_relative_to(project_root) else None


def _path_from_payload(payload: JsonDict, project: Path) -> Path | None:
    value = payload.get("path")
    return _safe_project_path(project, str(value)) if value else None


def _keyframe_payload(root: Path, project: Path, frame_ref: str | None) -> JsonDict | None:
    if not frame_ref:
        return None
    return _path_payload(root, project, project / "assets" / "keyframes" / frame_ref)


def _shot_keyframes(root: Path, project: Path, shot: Shot) -> list[JsonDict]:
    refs = shot.frame_refs or [shot.primary_frame_ref or shot.frame_ref]
    times = {
        refs[0]: shot.start_time if refs else shot.start_time,
        shot.primary_frame_ref or shot.frame_ref: shot.start_time + max(shot.duration, 0.0) / 2,
        refs[-1]: max(shot.start_time, shot.end_time),
    }
    items: list[JsonDict] = []
    for ref in refs:
        if not ref:
            continue
        kind = "mid"
        if ref.endswith("_start.jpg"):
            kind = "start"
        elif ref.endswith("_end.jpg"):
            kind = "end"
        items.append(
            {
                "id": f"{shot.shot_id}_{kind}",
                "shot_id": shot.shot_id,
                "kind": kind,
                "time": round(float(times.get(ref, shot.start_time)), 3),
                "frame_ref": ref,
                **(_keyframe_payload(root, project, ref) or {}),
            }
        )
    return items


def _shot_boundary_payload(
    root: Path,
    project: Path,
    shot: Shot,
    *,
    profile: str = "research",
    readiness_result: JsonDict | None = None,
) -> JsonDict:
    keyframes = _shot_keyframes(root, project, shot)
    visual = _preferred_evidence_text(
        shot.content_summary,
        shot.visual_description,
        shot.action,
        shot.content_summary_zh,
        shot.action_zh,
    )
    story_beat = _story_beat_label(shot.story_beat)
    current_result = readiness_result if isinstance(readiness_result, dict) else None
    payload = {
        "id": shot.shot_id,
        "edit_version": _shot_edit_version(shot),
        "canvas_node_id": shot.shot_id,
        "shot_id": shot.shot_id,
        "shot_no": shot.shot_no,
        "start_time": shot.start_time,
        "end_time": shot.end_time,
        "duration": shot.duration,
        "timecode": _timecode_range(shot.start_time, shot.end_time),
        "raw_timecode": shot.timecode,
        "story_beat": story_beat,
        "story_beat_raw": shot.story_beat,
        "annotation_source": shot.annotation_source,
        "annotation_verification": _annotation_verification(shot, current_result),
        "boundary_confidence": shot.boundary_confidence,
        "primary_frame_ref": shot.primary_frame_ref or shot.frame_ref,
        "keyframes": keyframes,
        "shot_size": _shot_size_text(shot.shot_scale),
        "angle": _angle_text(shot.camera_angle),
        "sound": _sound_text(shot),
        "visual_content": visual or None,
        "meaning": _meaning_text(shot, story_beat, visual),
        "rhythm": _rhythm_text(shot),
        "readiness_status": (
            "ready" if current_result.get("professional_ready") is True else "blocked"
        ) if current_result is not None else shot.readiness_status,
        "readiness_reasons": (
            list(current_result.get("reasons") or [])
            if current_result is not None
            else list(shot.readiness_reasons)
        ),
        "visual_confidence": shot.visual_confidence,
        "review_fields": {
            field: getattr(shot, field)
            for field in sorted(SHOT_REVIEW_TEXT_FIELDS)
        } | {
            "visual_confidence": shot.visual_confidence,
            "readiness_status": shot.readiness_status,
            "boundary_reviewed": (
                current_result.get("boundary_reviewed") is True
                if current_result is not None
                else False
            ),
        },
    }
    if profile == "ads":
        payload["remake_tip"] = shot.remake_notes or shot.remake_notes_zh
        payload["prompt"] = shot.prompt_en or shot.prompt_zh
    return payload


def _shot_edit_version(shot: Shot) -> str:
    canonical = json.dumps(
        shot.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def _annotation_verification(shot: Shot, readiness_result: JsonDict | None = None) -> str:
    if readiness_result is not None:
        state = str(readiness_result.get("annotation_state") or "").strip().lower()
        if readiness_result.get("agent_submission_verified") is True and state == "agent_submission_bound":
            return "agent_submission_bound"
        if (
            readiness_result.get("provider_receipt_verified") is True
            or state == "provider_receipt_verified"
        ):
            return "provider_receipt_verified"
        if readiness_result.get("human_assertion") is True or state == "human_assertion":
            return "human_reviewed"
        return "unverified"
    story_beat = (shot.story_beat or "").strip().lower()
    if story_beat.startswith("heuristic_unverified:"):
        return "unverified"
    source = (shot.annotation_source or "").strip().lower()
    readiness = (shot.readiness_status or "").strip().lower()
    return "human_reviewed" if source == "human" and readiness == "ready" else "unverified"


def _preferred_evidence_text(*values: str) -> str:
    for value in values:
        text = (value or "").strip()
        normalized = text.lower()
        if text and normalized not in {"unknown", "tbd", "n/a", "none"} and not normalized.startswith("to annotate"):
            return text
    return ""


def _timecode_range(start: float, end: float) -> str:
    return f"{_mmss(start)}-{_mmss(end)}"


def _mmss(seconds: float) -> str:
    safe = max(0, int(round(seconds)))
    return f"{safe // 60:02d}:{safe % 60:02d}"


def _story_beat_label(value: str) -> str:
    normalized = (value or "").strip().lower().replace("_", " ").replace("-", " ")
    labels = {
        "hook": "Hook",
        "problem": "Problem",
        "setup": "Setup",
        "demo": "Demo",
        "proof": "Proof",
        "payoff": "Payoff",
        "reaction": "Reaction",
        "motif": "Visual motif",
        "motif payoff": "Motif payoff",
        "product reveal": "Product reveal",
        "cta": "Call to action",
    }
    return labels.get(normalized, value or "Needs review")


def _sound_text(shot: Shot) -> str | None:
    if shot.dialogue:
        return f"Dialogue / voice-over: {shot.dialogue}"
    if shot.audio_notes and "No transcript available" not in shot.audio_notes:
        return shot.audio_notes
    if shot.sound_design and shot.sound_design != "unknown":
        return shot.sound_design
    return None


def _shot_size_text(value: str) -> str:
    raw = (value or "").strip()
    normalized = raw.lower()
    if not raw or normalized in {"unknown", "tbd"}:
        return "Needs review"
    if "extreme wide" in normalized or "aerial" in normalized:
        return "Extreme wide"
    if "wide" in normalized:
        return "Wide"
    if "close" in normalized:
        return "Close-up"
    if "medium" in normalized:
        return "Medium"
    if "detail" in normalized:
        return "Detail"
    return raw


def _angle_text(value: str) -> str | None:
    raw = (value or "").strip()
    normalized = raw.lower()
    if not raw or normalized in {"unknown", "tbd"}:
        return None
    return raw


def _meaning_text(shot: Shot, story_beat: str, visual: str) -> str | None:
    if shot.direction_notes:
        return shot.direction_notes
    if shot.direction_notes_zh:
        return shot.direction_notes_zh
    return None


def _rhythm_text(shot: Shot) -> str | None:
    if shot.rhythm_notes and shot.rhythm_notes not in {"pending audio sync", "sparse rhythm activity"}:
        return shot.rhythm_notes
    return None


def _load_transcript(project: Path) -> list[JsonDict]:
    data = read_project_json(project, project / "data" / "transcript.json", [])
    if not isinstance(data, list):
        raise ApiError(409, "Transcript receipt must be a JSON array")
    return [item for item in data if isinstance(item, dict)]


def _transcript_summary(project: Path) -> str:
    segments = _load_transcript(project)
    if not segments:
        return "No transcript is available; use shot, rhythm, and audio analysis as context."
    text = " ".join(str(item.get("text") or "").strip() for item in segments[:6]).strip()
    return text or "The transcript is empty; audio content needs human review."


def _prompt_body_for_branch(shots: list[Shot], branch_name: str) -> str:
    first = shots[0] if shots else None
    base = first.prompt_en or first.prompt_zh if first else ""
    branch_label = {
        "safer": "Safer version: retain the original structure while improving rhythm and clarity.",
        "stronger_hook": "Stronger hook: move the clearest conflict and viewing reason into the opening three seconds.",
        "premium_style": "Premium version: improve visual credibility and brand quality.",
    }.get(branch_name, "Remake prompt: generate a creative branch from the current shot context.")
    return f"{branch_label}\n{base}".strip()


def _nearest_keyframe(timeline: JsonDict, seconds: float) -> JsonDict | None:
    keyframes = [item for item in timeline.get("keyframes", []) if isinstance(item, dict)]
    if not keyframes:
        return None
    return min(keyframes, key=lambda item: abs(float(item.get("time") or 0.0) - seconds))


def _shot_at_time(timeline: JsonDict, seconds: float) -> JsonDict | None:
    for shot in timeline.get("shot_boundaries", []):
        if isinstance(shot, dict) and float(shot.get("start_time") or 0.0) <= seconds <= float(shot.get("end_time") or 0.0):
            return shot
    return None


def _shots_overlapping(timeline: JsonDict, start: float, end: float) -> list[JsonDict]:
    return [
        shot
        for shot in timeline.get("shot_boundaries", [])
        if isinstance(shot, dict)
        and float(shot.get("start_time") or 0.0) < end
        and float(shot.get("end_time") or 0.0) > start
    ]


def _deliverable_specs(
    project: Path,
    *,
    readiness: JsonDict | None = None,
    generation_current: bool | None = None,
) -> list[tuple[str, str, str, Path]]:
    manifest = _load_manifest(project)
    profile = str(manifest.get("profile") or "research").strip().lower()
    media = _load_media_package(project)
    if media is not None and _profile_value(media.analysis_profile) != profile:
        raise ApiError(409, "Project manifest and media package profiles do not match")

    dynamic_ids = {"media_package", "project_manifest", "vision_annotations"}
    canonical = [
        (
            str(spec.group),
            spec.artifact_id,
            str(spec.label),
            artifact_path(project, spec.artifact_id),
        )
        for spec in iter_workspace_artifacts(profile)
        if spec.artifact_id not in dynamic_ids
    ]

    if generation_current is None:
        generation_current, _generation_reasons = verify_report_generation_manifest(ProjectPaths(project))
    specs: list[tuple[str, str, str, Path]] = [
        ("provenance", "project_manifest", "Project manifest", artifact_path(project, "project_manifest")),
    ]
    if media is not None and media.project_id == project.name:
        specs.append(("provenance", "media_package", "Media package receipt", artifact_path(project, "media_package")))
    if readiness is not None:
        current = readiness
    else:
        try:
            current = evaluate_project_readiness(project, workspace_root=project.parent)
        except Exception:
            current = {"stored_readiness_valid": False, "vision_annotation_complete": False}
    if (
        generation_current
        and current.get("stored_readiness_valid") is True
        and _manifest_declares_canonical_artifact(
            project,
            manifest,
            "readiness_json",
            artifact_path(project, "readiness_json"),
        )
    ):
        specs.append(("gate", "readiness_json", "Current readiness receipt", artifact_path(project, "readiness_json")))
        if (
            isinstance(current.get("boundary_review_binding"), dict)
            and _manifest_declares_canonical_artifact(
                project,
                manifest,
                "boundary_review_json",
                artifact_path(project, "boundary_review_json"),
            )
        ):
            specs.append(
                (
                    "gate",
                    "boundary_review_json",
                    "Bound human boundary review",
                    artifact_path(project, "boundary_review_json"),
                )
            )
        if (
            _lineage_matches_current_readiness(project)
            and _manifest_declares_canonical_artifact(
                project,
                manifest,
                "lineage_json",
                artifact_path(project, "lineage_json"),
            )
        ):
            specs.append(("provenance", "lineage_json", "Current lineage graph", artifact_path(project, "lineage_json")))
    if current.get("vision_annotation_complete") is True and current.get("stored_readiness_valid") is True:
        specs.append(
            ("provenance", "vision_annotations", "Current vision provider receipt", artifact_path(project, "vision_annotations"))
        )
    for spec in canonical:
        if generation_current and _manifest_declares_canonical_artifact(project, manifest, spec[1], spec[3]):
            specs.append(spec)
    return specs


def is_current_project_file(project: Path, path: Path) -> bool:
    """Authorize only current manifest-bound evidence, media, and keyframes."""
    try:
        project_root = Path(os.path.abspath(os.fspath(project)))
        candidate = Path(os.path.abspath(os.fspath(path)))
        candidate.relative_to(project_root)
        allowed = {
            Path(os.path.abspath(os.fspath(spec_path)))
            for _group, _artifact_id, _label, spec_path in _deliverable_specs(project)
            if _deliverable_file_info(project, spec_path) is not None
        }
        media = _load_media_package(project)
        manifest = _load_manifest(project)
        profile = str(manifest.get("profile") or "research").strip().lower()
        if media is not None and media.project_id == project.name and _profile_value(media.analysis_profile) == profile:
            current = evaluate_project_readiness(
                project,
                workspace_root=project.parent,
                require_persisted_receipt=False,
            )
            if (current.get("media_binding") or {}).get("status") == "bound":
                review = _confined_project_path(project, media.review_copy_path)
                if review is not None and _deliverable_file_info(project, review) is not None:
                    allowed.add(review)
            for shot in load_shots(project):
                for reference in dict.fromkeys([shot.frame_ref, shot.primary_frame_ref, *shot.frame_refs]):
                    if not reference:
                        continue
                    try:
                        evidence = read_frame_evidence(project, reference)
                        allowed.add(Path(str(evidence["path"])))
                    except Exception:
                        continue
        return candidate in allowed
    except (ApiError, OSError, ValueError):
        return False


def _manifest_declares_canonical_artifact(project: Path, manifest: JsonDict, key: str, canonical: Path) -> bool:
    artifacts = manifest.get("artifacts") if isinstance(manifest.get("artifacts"), dict) else {}
    value = artifacts.get(key)
    if type(value) is not str or not value.strip():
        return False
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = project / candidate
    try:
        project_root = _normalize_system_prefix(project)
        candidate = _normalize_system_prefix(candidate)
        canonical = _normalize_system_prefix(canonical)
        candidate.relative_to(project_root)
    except (OSError, ValueError):
        return False
    return candidate == canonical


def _normalize_system_prefix(path: Path) -> Path:
    """Normalize only an OS-level prefix alias such as macOS /var."""
    absolute = Path(os.path.abspath(os.fspath(path)))
    if len(absolute.parts) < 2:
        return absolute
    first = Path(absolute.anchor) / absolute.parts[1]
    try:
        info = first.lstat()
    except OSError:
        return absolute
    if not stat.S_ISLNK(info.st_mode):
        return absolute
    return first.resolve(strict=True).joinpath(*absolute.parts[2:])


def _lineage_matches_current_readiness(project: Path) -> bool:
    try:
        stored = read_project_json(project, project / "data" / "readiness.json", None)
        lineage = read_project_json(project, project / "data" / "lineage.json", None)
        if type(stored) is not dict or type(lineage) is not dict or type(lineage.get("readiness")) is not dict:
            return False
        return canonical_readiness_payload(lineage["readiness"]) == canonical_readiness_payload(stored)
    except Exception:
        return False


def _confined_project_path(project: Path, value: str) -> Path | None:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = project / candidate
    try:
        project_root = Path(os.path.abspath(os.fspath(project)))
        candidate = Path(os.path.abspath(os.fspath(candidate)))
        candidate.relative_to(project_root)
        return candidate
    except (OSError, ValueError):
        return None


def _profile_value(value: object) -> str:
    return str(getattr(value, "value", value)).strip().lower()


def is_professional_export_path(project: Path, path: Path) -> bool:
    """Classify by the canonical project asset, independent of manifest aliases."""
    try:
        project_root = Path(os.path.abspath(os.fspath(project)))
        candidate = Path(os.path.abspath(os.fspath(path)))
        relative = candidate.relative_to(project_root)
    except (OSError, ValueError):
        return False
    return tuple(relative.parts) in PROFESSIONAL_EXPORT_RELATIVE_PATHS


def _deliverable_file_info(project: Path, path: Path) -> os.stat_result | None:
    """Return metadata only for a confined, regular, non-symlink project file."""
    try:
        project_root = Path(os.path.abspath(os.fspath(project)))
        candidate = Path(os.path.abspath(os.fspath(path)))
        relative = candidate.relative_to(project_root)
        # Resolve the ambient project root once (macOS /var -> /private/var is
        # normal), then reject resolution changes below that trusted root.
        expected = project_root.resolve() / relative
        if candidate.resolve() != expected:
            return None
        info = candidate.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            return None
        return info
    except (OSError, ValueError):
        return None


def _api_readiness(data: JsonDict) -> JsonDict:
    result = dict(data)
    raw_status = str(result.get("status") or "blocked")
    score = _readiness_score(result)
    result["raw_status"] = raw_status
    result["score"] = score
    if raw_status == "ready":
        result["status"] = "ready"
    elif raw_status == "needs_review":
        result["status"] = "review"
    elif raw_status in {"blocked", "error"}:
        result["status"] = "blocked"
    else:
        result["status"] = raw_status
    result.setdefault("reasons", [])
    if result["status"] == "ready":
        result["summary"] = "Shot evidence passed the structured gate; factual conclusions still require comparison with the source video."
    elif result["status"] == "review":
        result["summary"] = "Shot data is available and awaiting human review."
    else:
        result["summary"] = "Additional analysis evidence is required."
    result["checks"] = [
        {
            "id": "media",
            "label": "Video and keyframes",
            "status": "ready" if _media_evidence_ready(result) else "blocked",
        },
        {"id": "shots", "label": "Shot data", "status": "ready" if result.get("shot_count", 0) else "blocked"},
        {"id": "readiness", "label": "Delivery review", "status": result["status"]},
    ]
    return result


def _media_evidence_ready(data: JsonDict) -> bool:
    binding = data.get("media_binding")
    if not isinstance(binding, dict) or binding.get("status") != "bound":
        return False
    shot_count = data.get("shot_count")
    results = data.get("shot_results")
    if not isinstance(shot_count, int) or shot_count <= 0 or not isinstance(results, list) or len(results) != shot_count:
        return False
    for item in results:
        if not isinstance(item, dict):
            return False
        reasons = item.get("reasons")
        if not isinstance(reasons, list):
            return False
        if any(
            "frame reference" in str(reason).lower() or "primary frame" in str(reason).lower()
            for reason in reasons
        ):
            return False
    return True


def _readiness_score(data: JsonDict) -> float:
    if data.get("score") is not None:
        try:
            return round(max(0.0, min(1.0, float(data["score"]))), 3)
        except (TypeError, ValueError):
            pass
    avg = float(data.get("average_visual_confidence") or 0.0)
    empty_penalty = float(data.get("critical_empty_rate") or 0.0) * 0.25
    boundary_penalty = float(data.get("low_boundary_confidence_rate") or 0.0) * 0.15
    if avg <= 0:
        avg = 0.5 if data.get("shot_count") else 0.0
    return round(max(0.0, min(1.0, avg - empty_penalty - boundary_penalty)), 3)


def _canvas_node(
    node_id: str,
    node_type: str,
    x: float,
    y: float,
    data: JsonDict,
    width: int = 260,
    height: int = 160,
    source: str = "derived",
) -> JsonDict:
    timestamp = None if source == "derived" else _now()
    return {
        "id": node_id,
        "type": node_type,
        "position": {"x": x, "y": y},
        "size": {"width": width, "height": height},
        "data": data,
        "source": source,
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def _edge(edge_id: str, source: str, target: str, edge_type: str) -> JsonDict:
    return {"id": edge_id, "source": source, "target": target, "type": edge_type}


def _append_edge(edges: list[JsonDict], edge: JsonDict) -> None:
    if not any(item.get("id") == edge["id"] for item in edges if isinstance(item, dict)):
        edges.append(edge)


def _dedupe_edges(edges: list[JsonDict]) -> list[JsonDict]:
    result: list[JsonDict] = []
    seen: set[str] = set()
    for edge in edges:
        edge_id = str(edge.get("id") or "")
        if edge_id and edge_id not in seen:
            result.append(edge)
            seen.add(edge_id)
    return result


def _canvas_meta(graph: JsonDict) -> JsonDict:
    return {
        "project_id": graph.get("project_id"),
        "version": graph.get("version"),
        "node_count": len(graph.get("nodes") or []),
        "edge_count": len(graph.get("edges") or []),
        "updated_at": graph.get("updated_at"),
    }


def _touch_graph(graph: JsonDict) -> None:
    graph["updated_at"] = _now()
    graph["version"] = _next_graph_version(str(graph.get("version") or "graph_000"))


def _next_graph_version(value: str) -> str:
    match = re.search(r"(\d+)$", value)
    number = int(match.group(1)) + 1 if match else 1
    return f"graph_{number:03d}"


def _prompt_for_branch(prompt_nodes: list[JsonDict], branch_name: str) -> str | None:
    for prompt in prompt_nodes:
        if str(prompt.get("branch") or "") == branch_name:
            return str(prompt.get("id"))
    return None


def _branch_id_for_name(branches: list[JsonDict], branch_name: str) -> str | None:
    for branch in branches:
        if str(branch.get("name") or "") == branch_name:
            return str(branch.get("id"))
    return None


def _lineage_head(lineage: JsonDict) -> str | None:
    commits = [item for item in lineage.get("commits", []) if isinstance(item, dict)]
    if not commits:
        return None
    return str(commits[-1].get("id"))


def _find_by_id(items: Any, item_id: str) -> JsonDict | None:
    if not item_id or not isinstance(items, list):
        return None
    for item in items:
        if isinstance(item, dict) and str(item.get("id")) == item_id:
            return item
    return None


def _next_id(prefix: str, existing_ids: list[str]) -> str:
    pattern = re.compile(rf"^{re.escape(prefix)}_(\d+)$")
    highest = 0
    for item in existing_ids:
        match = pattern.match(item)
        if match:
            highest = max(highest, int(match.group(1)))
    return f"{prefix}_{highest + 1:03d}"


def _project_root(root: Path, project_id: str) -> Path:
    try:
        workspace = root.expanduser().resolve()
        project = resolve_project_root(project_id, root)
    except ValueError as exc:
        raise ApiError(400, "Invalid project id") from exc
    if project != workspace / project_id or (workspace / project_id).is_symlink():
        raise ApiError(400, "Invalid project id")
    _require_project(project)
    return project


def validated_project_root(root: Path, project_id: str) -> Path:
    return _project_root(root, project_id)


def load_project_manifest(project: Path) -> ProjectManifest:
    path = project / "project_manifest.json"
    try:
        raw = read_project_json(project, path, None)
        if not isinstance(raw, dict):
            raise ValueError("manifest must be a JSON object")
        manifest = ProjectManifest.model_validate(raw)
    except ApiError as exc:
        if exc.status == 409:
            raise ApiError(404, f"Project not found: {project.name}") from None
        raise
    except Exception:
        raise ApiError(404, f"Project not found: {project.name}") from None
    if manifest.project_id != project.name:
        raise ApiError(404, f"Project not found: {project.name}")
    return manifest


def _require_project(project: Path) -> ProjectManifest:
    return load_project_manifest(project)


def _parse_json_body(body: bytes) -> JsonDict:
    if len(body) > 1024 * 1024:
        raise ApiError(413, "Request body exceeds 1 MiB", {"code": "request_too_large"})
    if not body:
        return {}
    try:
        data = json.loads(
            body.decode("utf-8"), parse_constant=_reject_json_constant,
            object_pairs_hook=_unique_request_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError, OverflowError) as exc:
        raise ApiError(400, "Request body must be valid JSON", str(exc)) from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ApiError(400, "Request body must be a JSON object")
    return data


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Non-finite JSON number is not allowed: {value}")


def _unique_request_object(pairs: list[tuple[str, Any]]) -> JsonDict:
    result: JsonDict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key is not allowed: {key}")
        result[key] = value
    return result


def _finite_number(
    value: object,
    *,
    label: str,
    minimum: float,
    maximum: float,
) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ApiError(400, f"{label} must be a number") from None
    if not math.isfinite(number) or number < minimum or number > maximum:
        raise ApiError(400, f"{label} must be between {minimum:g} and {maximum:g}")
    return number


def _finite_integer(
    value: object,
    *,
    label: str,
    minimum: int,
    maximum: int,
) -> int:
    number = _finite_number(value, label=label, minimum=float(minimum), maximum=float(maximum))
    if not number.is_integer():
        raise ApiError(400, f"{label} must be an integer")
    return int(number)


_MISSING = object()


@contextmanager
def project_write_lock(project: Path) -> Iterator[None]:
    key = os.path.abspath(os.fspath(project))
    with _PROJECT_LOCKS_GUARD:
        lock = _PROJECT_LOCKS.setdefault(key, threading.RLock())
    with lock:
        yield


def _project_relative_target(project: Path, path: Path) -> tuple[Path, Path, Path]:
    project_path = Path(os.path.abspath(os.fspath(project)))
    target = path if path.is_absolute() else project_path / path
    target_path = Path(os.path.abspath(os.fspath(target)))
    try:
        relative = target_path.relative_to(project_path)
    except ValueError:
        raise ApiError(409, "Project data path escapes the project root") from None
    if not relative.parts:
        raise ApiError(409, "Project data path must name a file")
    return project_path, target_path, relative


@contextmanager
def _project_parent_descriptor(
    project: Path,
    path: Path,
    *,
    create: bool,
) -> Iterator[tuple[int | None, str, Path]]:
    project_path, target_path, relative = _project_relative_target(project, path)
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        current = project_path
        if current.is_symlink() or not current.is_dir():
            raise ApiError(409, "Unsafe project data path")
        for part in relative.parts[:-1]:
            current = current / part
            try:
                info = current.lstat()
            except FileNotFoundError:
                if not create:
                    raise
                current.mkdir(mode=0o700)
                info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise ApiError(409, "Unsafe project data path")
        yield None, relative.name, target_path
        return

    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_flags |= getattr(os, "O_CLOEXEC", 0)
    descriptors: list[int] = []
    try:
        try:
            current_fd = os.open(project_path, directory_flags)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise ApiError(409, "Unsafe project data path") from None
            raise
        descriptors.append(current_fd)
        for part in relative.parts[:-1]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=current_fd)
                except FileExistsError:
                    pass
            try:
                next_fd = os.open(part, directory_flags, dir_fd=current_fd)
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise ApiError(409, "Unsafe project data path") from None
                raise
            descriptors.append(next_fd)
            current_fd = next_fd
        yield current_fd, relative.name, target_path
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _entry_stat(parent_fd: int | None, name: str, path: Path) -> os.stat_result | None:
    try:
        info = (
            os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if parent_fd is not None
            else path.lstat()
        )
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ApiError(409, "Unsafe project data path")
    return info


def _validate_project_write_target(project: Path, path: Path) -> None:
    _project_relative_target(project, path)
    try:
        with _project_parent_descriptor(project, path, create=False) as (parent_fd, name, target):
            _entry_stat(parent_fd, name, target)
    except FileNotFoundError:
        return


def _read_project_bytes(project: Path, path: Path) -> bytes | None:
    try:
        with _project_parent_descriptor(project, path, create=False) as (parent_fd, name, target):
            info = _entry_stat(parent_fd, name, target)
            if info is None:
                return None
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            if parent_fd is not None:
                flags |= getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(name, flags, dir_fd=parent_fd)
            else:
                descriptor = os.open(target, flags)
            try:
                opened = os.fstat(descriptor)
                if not stat.S_ISREG(opened.st_mode):
                    raise ApiError(409, "Unsafe project data path")
                if opened.st_size > MAX_PROJECT_JSON_BYTES:
                    raise ApiError(413, "Project JSON receipt is too large")
                with os.fdopen(descriptor, "rb", closefd=False) as handle:
                    raw = handle.read(MAX_PROJECT_JSON_BYTES + 1)
                if len(raw) > MAX_PROJECT_JSON_BYTES:
                    raise ApiError(413, "Project JSON receipt is too large")
                return raw
            finally:
                os.close(descriptor)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise ApiError(409, "Unsafe project data path") from None
        raise


def _project_file_mtime(project: Path, path: Path) -> float | None:
    try:
        with _project_parent_descriptor(project, path, create=False) as (parent_fd, name, target):
            info = _entry_stat(parent_fd, name, target)
            return info.st_mtime if info is not None else None
    except FileNotFoundError:
        return None


def read_project_json(project: Path, path: Path, default: Any) -> Any:
    raw = _read_project_bytes(project, path)
    if raw is None:
        return default if default is _MISSING else copy.deepcopy(default)
    try:
        return json.loads(
            raw.decode("utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        relative = _project_relative_target(project, path)[2]
        raise ApiError(409, f"Invalid JSON receipt: {relative.as_posix()}") from None


def _atomic_write_project_bytes(project: Path, path: Path, payload: bytes) -> None:
    with _project_parent_descriptor(project, path, create=True) as (parent_fd, name, target):
        _entry_stat(parent_fd, name, target)
        temporary_name = f".{name}.{secrets.token_hex(12)}.tmp"
        temporary_path = target.parent / temporary_name
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        if parent_fd is not None:
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(temporary_name, flags, 0o600, dir_fd=parent_fd)
        else:
            descriptor = os.open(temporary_path, flags, 0o600)
        temporary_exists = True
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            # A destination symlink introduced after the initial check is still
            # replaced rather than followed, but reject it when observable.
            _entry_stat(parent_fd, name, target)
            if parent_fd is not None:
                os.replace(
                    temporary_name,
                    name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
                try:
                    os.fsync(parent_fd)
                except OSError as exc:
                    if exc.errno not in {errno.EINVAL, getattr(errno, "ENOTSUP", errno.EINVAL)}:
                        raise
            else:
                os.replace(temporary_path, target)
            temporary_exists = False
        finally:
            if temporary_exists:
                try:
                    if parent_fd is not None:
                        os.unlink(temporary_name, dir_fd=parent_fd)
                    else:
                        temporary_path.unlink()
                except FileNotFoundError:
                    pass


def _write_json(project: Path, path: Path, data: Any) -> bool:
    value = _jsonable(data)
    with project_write_lock(project):
        existing = read_project_json(project, path, _MISSING)
        if existing is not _MISSING and existing == value:
            return False
        try:
            payload = json.dumps(
                value,
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError):
            raise ApiError(400, "Project JSON contains an unsupported value") from None
        _atomic_write_project_bytes(project, path, payload)
    return True


def write_project_json(project: Path, path: Path, data: Any) -> bool:
    _require_project(project)
    return _write_json(project, path, data)


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    return value


def _rel_to_root(root: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except Exception:
        return str(path)


def _file_url(root: Path, path: Path) -> str:
    return f"/files/{quote(_rel_to_root(root, path))}"


def _parse_resolution(value: str) -> tuple[int, int]:
    try:
        left, right = value.lower().split("x", 1)
        return int(left), int(right)
    except Exception:
        return 0, 0


def _content_type_for_path(path: Path) -> str:
    if path.suffix == ".html":
        return "text/html"
    if path.suffix == ".json":
        return "application/json"
    if path.suffix == ".csv":
        return "text/csv"
    if path.suffix == ".md":
        return "text/markdown"
    if path.suffix == ".svg":
        return "image/svg+xml"
    return "application/octet-stream"


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_") or "artifact"


def _csv(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    return [item for item in str(value or "").split(",") if item]


def _derived_timestamp(project: Path) -> str | None:
    candidates = [
        project / "project_manifest.json",
        project / "data" / "media_package.json",
        project / "data" / "shots.json",
        project / "data" / "lineage.json",
        project / "data" / "keeper_decision.json",
        project / "data" / "readiness.json",
        project / "data" / "transcript.json",
    ]
    mtimes = [mtime for path in candidates if (mtime := _project_file_mtime(project, path)) is not None]
    if not mtimes:
        return None
    return datetime.fromtimestamp(max(mtimes), UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
