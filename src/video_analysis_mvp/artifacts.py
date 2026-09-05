from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

from .paths import ProjectPaths
from .safe_io import advisory_file_lock, atomic_write_text, read_regular_bytes

ARTIFACT_REGISTRY_SCHEMA_VERSION = 1
MAX_ARTIFACT_REGISTRY_BYTES = 2 * 1024 * 1024
ARTIFACT_SCOPES = frozenset({"analysis", "review", "report", "client_export"})
ARTIFACT_KINDS = frozenset({"file", "directory", "manifest", "package"})
ARTIFACT_STATES = frozenset(
    {"staging", "current", "stale", "saved", "failed", "cancelled", "superseded"}
)
ARTIFACT_RETENTION_CLASSES = frozenset({"project", "current", "saved", "transient"})
ARTIFACT_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9._:-]{0,127}")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
GENERATION_ID_PATTERN = re.compile(r"[^\x00-\x1f\x7f/\\]{1,256}")
REGISTRY_ROOT_KEYS = frozenset({"schema_version", "project_id", "revision", "artifacts"})
REGISTRY_RECORD_KEYS = frozenset(
    {
        "artifact_id",
        "scope",
        "kind",
        "relative_path",
        "state",
        "retention",
        "generation_id",
        "source_generation_id",
        "digest",
        "stale_reason",
    }
)
REGISTRY_DIGEST_KEYS = frozenset({"algorithm", "sha256", "size_bytes"})
ALLOWED_STATE_TRANSITIONS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "staging": frozenset({"current", "failed", "cancelled"}),
        "current": frozenset({"stale", "superseded"}),
        "stale": frozenset({"superseded"}),
        "saved": frozenset(),
        "failed": frozenset(),
        "cancelled": frozenset(),
        "superseded": frozenset(),
    }
)
_UNSET = object()


@dataclass(frozen=True, slots=True)
class ArtifactSpec:
    artifact_id: str
    relative_path: str
    kind: str
    scope: str
    group: str | None = None
    label: str | None = None
    profiles: frozenset[str] | None = None
    report_member: bool = False
    list_in_workspace: bool = False
    professional_export: bool = False


def _spec(
    artifact_id: str,
    relative_path: str,
    *,
    kind: str = "file",
    scope: str,
    group: str | None = None,
    label: str | None = None,
    profiles: frozenset[str] | None = None,
    report_member: bool = False,
    list_in_workspace: bool = False,
    professional_export: bool = False,
) -> ArtifactSpec:
    return ArtifactSpec(
        artifact_id=artifact_id,
        relative_path=relative_path,
        kind=kind,
        scope=scope,
        group=group,
        label=label,
        profiles=profiles,
        report_member=report_member,
        list_in_workspace=list_in_workspace,
        professional_export=professional_export,
    )


ADS_PROFILE = frozenset({"ads"})

# This catalog contains identity and path policy only. Runtime existence,
# readiness, generation validity, and download authorization remain live facts.
ARTIFACT_SPECS: tuple[ArtifactSpec, ...] = (
    _spec("media_package", "data/media_package.json", scope="analysis", group="provenance", label="Media package receipt", list_in_workspace=True),
    _spec("visual_generation", "data/visual_generation.json", scope="analysis"),
    _spec("audio_generation", "data/audio_generation.json", scope="analysis"),
    _spec("audio_intelligence", "data/audio_intelligence.json", scope="analysis"),
    _spec("audio_intelligence_generation", "data/audio_intelligence_generation.json", scope="analysis"),
    _spec("shots_json", "data/shots.json", scope="review"),
    _spec("scenes_json", "data/scenes.json", scope="analysis"),
    _spec("transcript_json", "data/transcript.json", scope="analysis"),
    _spec("beats_json", "data/beats.json", scope="analysis"),
    _spec("music_profile_json", "data/music_profile.json", scope="analysis"),
    _spec("vision_annotations", "data/vision_annotations.json", scope="review", group="provenance", label="Current vision provider receipt", list_in_workspace=True),
    _spec("codex_analysis_request", "data/codex_analysis_request.json", scope="review", group="companion", label="Current Codex analysis request", list_in_workspace=True),
    _spec("codex_analysis_progress", "data/codex_analysis_progress.json", scope="review", group="companion", label="Checkpointed Codex analysis proposals"),
    _spec("overview_pdf", "reports/overview.pdf", scope="report", group="primary", label="Overview export", report_member=True, list_in_workspace=True),
    _spec("report_html", "reports/report.html", scope="report", group="primary", label="Shot analysis report", report_member=True, list_in_workspace=True),
    _spec("storyboard_html", "reports/storyboard.html", scope="report", group="primary", label="Storyboard", report_member=True, list_in_workspace=True),
    _spec("shot_list_csv", "reports/shot_list.csv", scope="report", group="primary", label="Shot list", report_member=True, list_in_workspace=True),
    _spec("profile_analysis_html", "reports/profile_analysis.html", scope="report", group="evidence", label="Profile analysis report", report_member=True, list_in_workspace=True, professional_export=True),
    _spec("shot_breakdown_csv", "reports/shot_breakdown.csv", scope="report", group="evidence", label="Shot breakdown", report_member=True, list_in_workspace=True),
    _spec("shot_table_csv", "reports/shot_table.csv", scope="report", group="evidence", label="Shot evidence table", report_member=True, list_in_workspace=True),
    _spec("lineage_json", "data/lineage.json", scope="report", group="provenance", label="Current lineage graph", report_member=True),
    _spec("readiness_json", "data/readiness.json", scope="review", group="gate", label="Current readiness receipt", report_member=True),
    _spec("boundary_review_json", "data/boundary_review.json", scope="review", group="gate", label="Bound human boundary review", report_member=True),
    _spec("transcript_srt", "reports/transcript.srt", scope="report", group="media", label="Transcript subtitles", report_member=True, list_in_workspace=True),
    _spec("music_rhythm_summary", "reports/music_rhythm_summary.json", scope="report", group="media", label="Music and rhythm summary", report_member=True, list_in_workspace=True),
    _spec("contact_sheet", "assets/contact_sheet.jpg", scope="analysis", group="media", label="Keyframe contact sheet", report_member=True, list_in_workspace=True),
    _spec("keyframes", "assets/keyframes", kind="directory", scope="analysis", report_member=True),
    _spec("project_manifest", "project_manifest.json", kind="manifest", scope="report", group="provenance", label="Project manifest", report_member=True, list_in_workspace=True),
    _spec("codex_handoff", "reports/codex_handoff.md", scope="report", group="companion", label="Codex handoff", report_member=True, list_in_workspace=True),
    _spec("visualization_dataset", "data/visualization_dataset.json", scope="report", group="companion", label="Visualization dataset", report_member=True, list_in_workspace=True),
    _spec("remake_brief", "reports/remake_brief.md", scope="report", group="draft", label="Creative remake brief · heuristic / unverified", profiles=ADS_PROFILE, report_member=True, list_in_workspace=True, professional_export=True),
    _spec("branch_board_html", "reports/branch_board.html", scope="report", group="draft", label="Creative branch board · heuristic / unverified", profiles=ADS_PROFILE, report_member=True, list_in_workspace=True),
    _spec("prompt_reverse_engineering", "reports/prompt_reverse_engineering.md", scope="report", group="draft", label="Draft prompt reverse engineering", profiles=ADS_PROFILE, report_member=True, list_in_workspace=True),
    _spec("model_prompt_pack", "reports/model_prompt_pack.json", scope="report", group="draft", label="Creative prompt pack · unverified", profiles=ADS_PROFILE, report_member=True, list_in_workspace=True, professional_export=True),
    _spec("revision_plan", "reports/revision_plan.md", scope="report", group="draft", label="Draft revision plan", profiles=ADS_PROFILE, report_member=True, list_in_workspace=True),
    _spec("client_export_dataset", "data/client_export_dataset.json", scope="client_export"),
    _spec("client_current_package", "reports/client/current", kind="package", scope="client_export"),
    _spec("client_breakdown_xlsx", "reports/client/current/client_breakdown.xlsx", scope="client_export"),
    _spec("client_breakdown_pdf", "reports/client/current/client_breakdown.pdf", scope="client_export"),
    _spec("client_export_receipt", "reports/client/current/export_receipt.json", scope="client_export"),
)


def _validate_static_catalog(specs: Iterable[ArtifactSpec]) -> None:
    ids: set[str] = set()
    paths: set[str] = set()
    for spec in specs:
        if ARTIFACT_ID_PATTERN.fullmatch(spec.artifact_id) is None:
            raise RuntimeError(f"Invalid artifact id in static catalog: {spec.artifact_id}")
        if spec.artifact_id in ids:
            raise RuntimeError(f"Duplicate artifact id in static catalog: {spec.artifact_id}")
        _validate_relative_path(spec.relative_path)
        if spec.relative_path in paths:
            raise RuntimeError(f"Duplicate artifact path in static catalog: {spec.relative_path}")
        if spec.kind not in ARTIFACT_KINDS or spec.scope not in ARTIFACT_SCOPES:
            raise RuntimeError(f"Invalid artifact policy in static catalog: {spec.artifact_id}")
        if spec.list_in_workspace and (not spec.group or not spec.label):
            raise RuntimeError(f"Workspace artifact lacks display metadata: {spec.artifact_id}")
        ids.add(spec.artifact_id)
        paths.add(spec.relative_path)


def artifact_spec(artifact_id: str) -> ArtifactSpec:
    try:
        return ARTIFACT_BY_ID[artifact_id]
    except KeyError:
        raise ValueError(f"Unknown artifact id: {artifact_id}") from None


def artifact_path(root: Path, artifact_id: str) -> Path:
    spec = artifact_spec(artifact_id)
    return root.joinpath(*PurePosixPath(spec.relative_path).parts)


def artifact_allowed_for_profile(spec: ArtifactSpec, profile: str) -> bool:
    normalized = profile.strip().lower()
    return spec.profiles is None or normalized in spec.profiles


def iter_report_artifacts(profile: str) -> Iterator[ArtifactSpec]:
    return (
        spec
        for spec in ARTIFACT_SPECS
        if spec.report_member and artifact_allowed_for_profile(spec, profile)
    )


def iter_workspace_artifacts(profile: str) -> Iterator[ArtifactSpec]:
    return (
        spec
        for spec in ARTIFACT_SPECS
        if spec.list_in_workspace and artifact_allowed_for_profile(spec, profile)
    )


def empty_artifact_registry(project_id: str) -> dict[str, Any]:
    return {
        "schema_version": ARTIFACT_REGISTRY_SCHEMA_VERSION,
        "project_id": project_id,
        "revision": 0,
        "artifacts": [],
    }


def load_artifact_registry(paths: ProjectPaths) -> dict[str, Any]:
    path = paths.data / "artifact_registry.json"
    try:
        raw = read_regular_bytes(path, root=paths.root, max_bytes=MAX_ARTIFACT_REGISTRY_BYTES)
    except FileNotFoundError:
        return empty_artifact_registry(paths.root.name)
    try:
        payload = json.loads(raw.decode("utf-8"), parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"Artifact registry is unreadable: {exc}") from None
    return validate_artifact_registry(payload, project_id=paths.root.name)


def validate_artifact_registry(payload: Any, *, project_id: str) -> dict[str, Any]:
    if type(payload) is not dict or set(payload) != REGISTRY_ROOT_KEYS:
        raise ValueError("Artifact registry must contain exactly schema_version, project_id, revision, and artifacts")
    if (
        type(payload.get("schema_version")) is not int
        or payload.get("schema_version") != ARTIFACT_REGISTRY_SCHEMA_VERSION
    ):
        raise ValueError("Artifact registry schema version is unsupported")
    if type(payload.get("project_id")) is not str or payload.get("project_id") != project_id:
        raise ValueError("Artifact registry project_id does not match the project directory")
    revision = payload.get("revision")
    if type(revision) is not int or revision < 0:
        raise ValueError("Artifact registry revision must be a non-negative integer")
    records = payload.get("artifacts")
    if type(records) is not list:
        raise ValueError("Artifact registry artifacts must be a list")
    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    for index, item in enumerate(records):
        try:
            record = validate_artifact_record(item)
        except ValueError as exc:
            raise ValueError(f"Artifact registry entry {index} is invalid: {exc}") from None
        artifact_id = record["artifact_id"]
        relative_path = record["relative_path"]
        if artifact_id in seen_ids:
            raise ValueError(f"Artifact registry contains duplicate artifact_id: {artifact_id}")
        if relative_path in seen_paths:
            raise ValueError(f"Artifact registry contains duplicate relative_path: {relative_path}")
        seen_ids.add(artifact_id)
        seen_paths.add(relative_path)
        normalized.append(record)
    return {
        "schema_version": ARTIFACT_REGISTRY_SCHEMA_VERSION,
        "project_id": project_id,
        "revision": revision,
        "artifacts": sorted(normalized, key=lambda item: item["artifact_id"]),
    }


def validate_artifact_record(record: Any) -> dict[str, Any]:
    if type(record) is not dict or set(record) != REGISTRY_RECORD_KEYS:
        raise ValueError("entry keys do not match schema version 1")
    artifact_id = record.get("artifact_id")
    if type(artifact_id) is not str or ARTIFACT_ID_PATTERN.fullmatch(artifact_id) is None:
        raise ValueError("artifact_id is invalid")
    scope = record.get("scope")
    kind = record.get("kind")
    state = record.get("state")
    retention = record.get("retention")
    if type(scope) is not str or scope not in ARTIFACT_SCOPES:
        raise ValueError("scope is unsupported")
    if type(kind) is not str or kind not in ARTIFACT_KINDS:
        raise ValueError("kind is unsupported")
    if type(state) is not str or state not in ARTIFACT_STATES:
        raise ValueError("state is unsupported")
    if type(retention) is not str or retention not in ARTIFACT_RETENTION_CLASSES:
        raise ValueError("retention is unsupported")
    relative_path = record.get("relative_path")
    if type(relative_path) is not str:
        raise ValueError("relative_path must be a string")
    _validate_relative_path(relative_path)
    _validate_scope_path(scope, relative_path)
    static_spec = ARTIFACT_BY_ID.get(artifact_id)
    if static_spec is not None:
        if relative_path != static_spec.relative_path:
            raise ValueError("registered path does not match the static artifact catalog")
        if kind != static_spec.kind:
            raise ValueError("registered kind does not match the static artifact catalog")
        allowed_scopes = {static_spec.scope}
        if static_spec.report_member:
            allowed_scopes.add("report")
        if scope not in allowed_scopes:
            raise ValueError("registered scope does not match the static artifact catalog")
    elif not _is_saved_client_artifact(scope, kind, state, retention, relative_path):
        raise ValueError("unknown artifact ids are allowed only for saved client-export versions")
    if state == "saved" and not _is_saved_client_artifact(
        scope, kind, state, retention, relative_path
    ):
        raise ValueError("saved artifacts must be versioned below reports/client/saved")
    generation_id = _validate_generation_id(record.get("generation_id"), "generation_id")
    source_generation_id = _validate_generation_id(
        record.get("source_generation_id"), "source_generation_id"
    )
    digest = _validate_registry_digest(record.get("digest"))
    stale_reason = record.get("stale_reason")
    if stale_reason is not None and (
        type(stale_reason) is not str
        or not stale_reason.strip()
        or len(stale_reason.encode("utf-8")) > 1024
        or "\x00" in stale_reason
    ):
        raise ValueError("stale_reason is invalid")
    if state == "stale" and stale_reason is None:
        raise ValueError("stale entries require stale_reason")
    if state != "stale" and stale_reason is not None:
        raise ValueError("stale_reason is allowed only for stale entries")
    if state in {"current", "saved", "stale", "superseded"} and digest is None:
        raise ValueError(f"{state} entries require a digest")
    if state in {"current", "saved", "stale", "superseded"} and generation_id is None:
        raise ValueError(f"{state} entries require generation_id")
    if (
        scope in {"report", "client_export"}
        and state in {"current", "saved", "stale", "superseded"}
        and source_generation_id is None
    ):
        raise ValueError(f"{scope} {state} entries require source_generation_id")
    if state == "saved" and retention != "saved":
        raise ValueError("saved entries require saved retention")
    if retention == "saved" and state != "saved":
        raise ValueError("saved retention is allowed only for saved entries")
    return {
        "artifact_id": artifact_id,
        "scope": scope,
        "kind": kind,
        "relative_path": relative_path,
        "state": state,
        "retention": retention,
        "generation_id": generation_id,
        "source_generation_id": source_generation_id,
        "digest": digest,
        "stale_reason": stale_reason,
    }


def register_artifact(
    paths: ProjectPaths,
    record: Any,
    *,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    validated_record = validate_artifact_record(record)
    with advisory_file_lock(paths.data / ".artifact-registry.lock", root=paths.root):
        registry = load_artifact_registry(paths)
        _require_registry_revision(registry, expected_revision)
        records = list(registry["artifacts"])
        matches = [
            item for item in records if item["artifact_id"] == validated_record["artifact_id"]
        ]
        if matches:
            raise ValueError(f"Artifact is already registered: {validated_record['artifact_id']}")
        path_owner = next(
            (
                item["artifact_id"]
                for item in records
                if item["relative_path"] == validated_record["relative_path"]
                and item["artifact_id"] != validated_record["artifact_id"]
            ),
            None,
        )
        if path_owner is not None:
            raise ValueError(
                f"Artifact path is already registered by another id: {path_owner}"
            )
        records.append(validated_record)
        updated = _next_registry(registry, records)
        _write_registry_unlocked(paths, updated)
        return updated


def replace_client_current_artifacts(
    paths: ProjectPaths,
    records: Iterable[Any],
    *,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    """Atomically replace metadata for the stable client/current package slot."""
    validated = [validate_artifact_record(record) for record in records]
    if not validated or any(
        record["scope"] != "client_export"
        or record["state"] != "current"
        or record["retention"] != "current"
        or record["artifact_id"] not in ARTIFACT_BY_ID
        or not record["relative_path"].startswith("reports/client/current")
        for record in validated
    ):
        raise ValueError("Client current replacement records are invalid")
    ids = [record["artifact_id"] for record in validated]
    paths_by_record = [record["relative_path"] for record in validated]
    if len(ids) != len(set(ids)) or len(paths_by_record) != len(set(paths_by_record)):
        raise ValueError("Client current replacement records must be unique")
    with advisory_file_lock(paths.data / ".artifact-registry.lock", root=paths.root):
        registry = load_artifact_registry(paths)
        _require_registry_revision(registry, expected_revision)
        retained = [
            record
            for record in registry["artifacts"]
            if not (
                record["scope"] == "client_export"
                and record["retention"] == "current"
            )
        ]
        updated = _next_registry(registry, [*retained, *validated])
        _write_registry_unlocked(paths, updated)
        return updated


def remove_saved_artifact(paths: ProjectPaths, artifact_id: str) -> dict[str, Any]:
    with advisory_file_lock(paths.data / ".artifact-registry.lock", root=paths.root):
        registry = load_artifact_registry(paths)
        matches = [record for record in registry["artifacts"] if record["artifact_id"] == artifact_id]
        if not matches:
            return registry
        if len(matches) != 1 or matches[0]["state"] != "saved" or matches[0]["retention"] != "saved":
            raise ValueError("Only a saved client-export artifact can be removed")
        updated = _next_registry(
            registry,
            [record for record in registry["artifacts"] if record["artifact_id"] != artifact_id],
        )
        _write_registry_unlocked(paths, updated)
        return updated


def clear_client_current_artifacts(paths: ProjectPaths) -> dict[str, Any]:
    """Remove only stable-slot client/current metadata during failed recovery."""
    with advisory_file_lock(paths.data / ".artifact-registry.lock", root=paths.root):
        registry = load_artifact_registry(paths)
        retained = [
            record
            for record in registry["artifacts"]
            if not (
                record["scope"] == "client_export"
                and record["retention"] == "current"
            )
        ]
        if len(retained) == len(registry["artifacts"]):
            return registry
        updated = _next_registry(registry, retained)
        _write_registry_unlocked(paths, updated)
        return updated


def transition_artifact(
    paths: ProjectPaths,
    artifact_id: str,
    target_state: str,
    *,
    stale_reason: str | None = None,
    expected_revision: int | None = None,
    generation_id: Any = _UNSET,
    source_generation_id: Any = _UNSET,
    digest: Any = _UNSET,
    retention: Any = _UNSET,
) -> dict[str, Any]:
    if type(target_state) is not str or target_state not in ARTIFACT_STATES:
        raise ValueError(f"Unsupported artifact target state: {target_state}")
    if target_state != "stale" and stale_reason is not None:
        raise ValueError("stale_reason is allowed only for stale transitions")
    with advisory_file_lock(paths.data / ".artifact-registry.lock", root=paths.root):
        registry = load_artifact_registry(paths)
        _require_registry_revision(registry, expected_revision)
        records = list(registry["artifacts"])
        for index, current in enumerate(records):
            if current["artifact_id"] != artifact_id:
                continue
            if current["state"] == target_state:
                requested = {
                    "generation_id": generation_id,
                    "source_generation_id": source_generation_id,
                    "digest": digest,
                    "retention": retention,
                }
                conflicts = [
                    key
                    for key, value in requested.items()
                    if value is not _UNSET and current[key] != value
                ]
                if target_state == "stale" and current["stale_reason"] != stale_reason:
                    conflicts.append("stale_reason")
                if conflicts:
                    raise ValueError(
                        "Idempotent artifact transition payload conflicts with current state: "
                        + ", ".join(conflicts)
                    )
                return registry
            if target_state not in ALLOWED_STATE_TRANSITIONS[current["state"]]:
                raise ValueError(f"Invalid artifact state transition: {current['state']} -> {target_state}")
            replacement = dict(current)
            replacement["state"] = target_state
            replacement["stale_reason"] = stale_reason if target_state == "stale" else None
            if generation_id is not _UNSET:
                replacement["generation_id"] = generation_id
            if source_generation_id is not _UNSET:
                replacement["source_generation_id"] = source_generation_id
            if digest is not _UNSET:
                replacement["digest"] = digest
            if retention is not _UNSET:
                replacement["retention"] = retention
            records[index] = validate_artifact_record(replacement)
            updated = _next_registry(registry, records)
            _write_registry_unlocked(paths, updated)
            return updated
        raise ValueError(f"Artifact is not registered: {artifact_id}")


def mark_artifacts_stale(
    paths: ProjectPaths,
    *,
    scopes: Iterable[str],
    reason: str,
) -> dict[str, Any]:
    try:
        selected = frozenset(scopes)
    except TypeError:
        raise ValueError("Artifact scopes must be strings") from None
    if not selected or not selected.issubset(ARTIFACT_SCOPES):
        raise ValueError("At least one supported artifact scope is required")
    registry_path = paths.data / "artifact_registry.json"
    if not registry_path.exists():
        return empty_artifact_registry(paths.root.name)
    with advisory_file_lock(paths.data / ".artifact-registry.lock", root=paths.root):
        registry = load_artifact_registry(paths)
        records: list[dict[str, Any]] = []
        changed = False
        for current in registry["artifacts"]:
            if current["scope"] in selected and current["state"] == "current":
                replacement = dict(current)
                replacement["state"] = "stale"
                replacement["stale_reason"] = reason
                current = validate_artifact_record(replacement)
                changed = True
            records.append(current)
        if not changed:
            return registry
        updated = _next_registry(registry, records)
        _write_registry_unlocked(paths, updated)
        return updated


def record_committed_report_artifacts(
    paths: ProjectPaths,
    manifest: Any,
) -> dict[str, Any]:
    if type(manifest) is not dict:
        raise ValueError("Committed report manifest must be an object")
    generation = manifest.get("report_generation")
    artifacts = manifest.get("artifacts")
    if (
        type(generation) is not dict
        or generation.get("state") != "committed"
        or type(generation.get("generation_id")) is not str
        or type(generation.get("source_receipts")) is not dict
        or type(generation.get("artifact_digests")) is not dict
        or type(artifacts) is not dict
        or set(artifacts) != set(generation["artifact_digests"])
    ):
        raise ValueError("Report manifest is not a complete committed generation")
    generation_id = generation["generation_id"]
    source_generation_id = "sha256:" + hashlib.sha256(
        _canonical_json_bytes(generation["source_receipts"])
    ).hexdigest()
    new_records: dict[str, dict[str, Any]] = {}
    for artifact_id, declared_path in sorted(artifacts.items()):
        spec = artifact_spec(artifact_id)
        if not spec.report_member:
            raise ValueError(f"Report manifest contains a non-report artifact: {artifact_id}")
        if type(declared_path) is not str or _declared_project_relative_path(
            paths, declared_path
        ) != spec.relative_path:
            raise ValueError(f"Report artifact path is non-canonical: {artifact_id}")
        receipt = generation["artifact_digests"].get(artifact_id)
        if type(receipt) is not dict or receipt.get("path") != declared_path:
            raise ValueError(f"Report artifact receipt is missing or inconsistent: {artifact_id}")
        digest = {
            "algorithm": receipt.get("digest_mode"),
            "sha256": receipt.get("sha256"),
            "size_bytes": receipt.get("size_bytes"),
        }
        new_records[artifact_id] = validate_artifact_record(
            {
                "artifact_id": artifact_id,
                # These records describe membership in one committed report
                # generation even when the underlying bytes originated in an
                # analysis or review stage.
                "scope": "report",
                "kind": receipt.get("kind"),
                "relative_path": spec.relative_path,
                "state": "current",
                "retention": "project",
                "generation_id": generation_id,
                "source_generation_id": source_generation_id,
                "digest": digest,
                "stale_reason": None,
            }
        )
    with advisory_file_lock(paths.data / ".artifact-registry.lock", root=paths.root):
        registry = load_artifact_registry(paths)
        records: list[dict[str, Any]] = []
        for current in registry["artifacts"]:
            if current["artifact_id"] in new_records:
                continue
            if current["scope"] == "report" and current["state"] in {"current", "stale"}:
                replacement = dict(current)
                replacement["state"] = "superseded"
                replacement["stale_reason"] = None
                current = validate_artifact_record(replacement)
            elif current["scope"] == "client_export" and current["state"] == "current":
                replacement = dict(current)
                replacement["state"] = "stale"
                replacement["stale_reason"] = "source_report_generation_changed"
                current = validate_artifact_record(replacement)
            records.append(current)
        records.extend(new_records.values())
        updated = _next_registry(registry, records)
        _write_registry_unlocked(paths, updated)
        return updated


def _validate_relative_path(value: str) -> None:
    if not value or "\x00" in value or "\\" in value:
        raise ValueError("relative_path is empty or contains forbidden characters")
    path = PurePosixPath(value)
    if path.is_absolute() or value != path.as_posix():
        raise ValueError("relative_path must be canonical project-relative POSIX syntax")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("relative_path contains an unsafe segment")


def _declared_project_relative_path(paths: ProjectPaths, value: str) -> str:
    candidate = Path(value)
    if candidate.is_absolute():
        root = Path(os.path.abspath(os.fspath(paths.root)))
        absolute = Path(os.path.abspath(os.fspath(candidate)))
        try:
            relative = absolute.relative_to(root)
        except ValueError:
            raise ValueError("declared artifact path escapes the project root") from None
        result = relative.as_posix()
    else:
        result = value
    _validate_relative_path(result)
    return result


def _validate_scope_path(scope: str, relative_path: str) -> None:
    path = PurePosixPath(relative_path)
    first = path.parts[0]
    allowed_roots = {
        "analysis": frozenset({"assets", "data", "ingest"}),
        "review": frozenset({"data"}),
        "report": frozenset({"assets", "data", "reports", "project_manifest.json"}),
        "client_export": frozenset({"data", "reports"}),
    }
    if first not in allowed_roots[scope]:
        raise ValueError(f"relative_path is outside the {scope} scope")
    if scope == "client_export" and first == "reports" and path.parts[:2] != ("reports", "client"):
        raise ValueError("client_export report artifacts must live below reports/client")
    if scope == "client_export" and first == "data" and relative_path != "data/client_export_dataset.json":
        raise ValueError("client_export data artifacts must use the canonical dataset path")


def _is_saved_client_artifact(
    scope: str,
    kind: str,
    state: str,
    retention: str,
    relative_path: str,
) -> bool:
    parts = PurePosixPath(relative_path).parts
    return (
        scope == "client_export"
        and kind in {"file", "package"}
        and state == "saved"
        and retention == "saved"
        and len(parts) >= 4
        and parts[:3] == ("reports", "client", "saved")
    )


_validate_static_catalog(ARTIFACT_SPECS)
ARTIFACT_BY_ID: Mapping[str, ArtifactSpec] = MappingProxyType(
    {spec.artifact_id: spec for spec in ARTIFACT_SPECS}
)
REPORT_ARTIFACT_RELATIVE_PATHS: Mapping[str, str] = MappingProxyType(
    {spec.artifact_id: spec.relative_path for spec in ARTIFACT_SPECS if spec.report_member}
)
ADS_ONLY_REPORT_ARTIFACT_IDS = frozenset(
    spec.artifact_id for spec in ARTIFACT_SPECS if spec.report_member and spec.profiles == ADS_PROFILE
)
PROFESSIONAL_EXPORT_IDS = frozenset(
    spec.artifact_id for spec in ARTIFACT_SPECS if spec.professional_export
)
PROFESSIONAL_EXPORT_RELATIVE_PATHS = frozenset(
    PurePosixPath(spec.relative_path).parts for spec in ARTIFACT_SPECS if spec.professional_export
)


def _validate_generation_id(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if type(value) is not str or GENERATION_ID_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field} is invalid")
    return value


def _validate_registry_digest(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if type(value) is not dict or set(value) != REGISTRY_DIGEST_KEYS:
        raise ValueError("digest keys do not match schema version 1")
    algorithm = value.get("algorithm")
    digest = value.get("sha256")
    size = value.get("size_bytes")
    if (
        type(algorithm) is not str
        or not algorithm
        or len(algorithm) > 128
        or GENERATION_ID_PATTERN.fullmatch(algorithm) is None
    ):
        raise ValueError("digest algorithm is invalid")
    if type(digest) is not str or SHA256_PATTERN.fullmatch(digest) is None:
        raise ValueError("digest sha256 is invalid")
    if type(size) is not int or size < 0:
        raise ValueError("digest size_bytes must be a non-negative integer")
    return {"algorithm": algorithm, "sha256": digest, "size_bytes": size}


def _require_registry_revision(registry: dict[str, Any], expected: int | None) -> None:
    if expected is not None and (type(expected) is not int or expected < 0):
        raise ValueError("Expected artifact registry revision must be a non-negative integer")
    if expected is not None and registry["revision"] != expected:
        raise ValueError(
            f"Artifact registry revision conflict: expected {expected}, current {registry['revision']}"
        )


def _next_registry(registry: dict[str, Any], records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    return validate_artifact_registry(
        {
            "schema_version": ARTIFACT_REGISTRY_SCHEMA_VERSION,
            "project_id": registry["project_id"],
            "revision": registry["revision"] + 1,
            "artifacts": list(records),
        },
        project_id=registry["project_id"],
    )


def _write_registry_unlocked(paths: ProjectPaths, payload: dict[str, Any]) -> None:
    paths.ensure()
    atomic_write_text(
        paths.data / "artifact_registry.json",
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False),
        root=paths.root,
    )


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Unsupported JSON constant: {value}")
