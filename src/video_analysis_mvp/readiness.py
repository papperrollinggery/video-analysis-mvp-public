from __future__ import annotations

from collections import OrderedDict
import hashlib
import json
import math
import os
import stat
import threading
from pathlib import Path
from typing import Any

from .artifacts import artifact_path
from .boundary_review import validate_boundary_review_receipt
from .bridge_vision import BRIDGE_PROVIDER_CONTRACT
from .config import (
    OFFICIAL_MINIMAX_ORIGINS,
    OFFICIAL_OPENAI_ORIGINS,
    VisionProvider,
    endpoint_origin,
    load_runtime_config,
    normalize_provider,
    resolve_provider_key,
    validate_bridgedeck_config,
)
from .image_evidence import MAX_IMAGE_BYTES, inspect_image_bytes
from .paths import ProjectPaths
from .safe_io import advisory_file_lock
from .schemas import CanonicalMediaPackage, Shot, dump_json, load_json
from .visual import visual_generation_binding


READINESS_SCHEMA_VERSION = 3
READINESS_BINDING_VERSION = "2.0"
VISION_RECEIPT_SCHEMA_VERSION = "1.0"
MEDIA_RECEIPT_SCHEMA_VERSION = "1.0"
PROVIDER_SOURCES = {"openai", "minimax_mcp", "codex", "bridgedeck"}
PROVIDER_SOURCE_TYPES = {
    "openai": "openai_chat_completions",
    "minimax_mcp": "minimax_mcp_understand_image",
    "codex": "codex_current_task_submission",
    "bridgedeck": "bridgedeck_responses",
}
BASE_CRITICAL_FIELDS = [
    "primary_frame_ref",
    "content_summary",
    "subject",
    "action",
    "shot_scale",
    "camera_angle",
    "camera_motion",
    "composition",
]
ADS_CRITICAL_FIELDS = [*BASE_CRITICAL_FIELDS[:1], "story_beat", *BASE_CRITICAL_FIELDS[1:]]
PLACEHOLDER_VALUES = {
    "",
    "unknown",
    "tbd",
    "to annotate",
    "to annotate from frame",
    "review required",
    "vision required",
    "vision model required",
}
TIMELINE_TOLERANCE_SECONDS = 0.001
MEDIA_DURATION_TOLERANCE_SECONDS = 0.05
MEDIA_FRAME_RATE_TOLERANCE = 0.01
MAX_BOUND_MEDIA_BYTES = 2 * 1024 * 1024 * 1024
VALIDATION_CACHE_MAX_ENTRIES = 256
_VALIDATION_CACHE: OrderedDict[tuple[Any, ...], dict[str, Any]] = OrderedDict()
_VALIDATION_CACHE_LOCK = threading.RLock()


def has_vision_key(workspace_root: Path) -> bool:
    try:
        config = load_runtime_config(workspace_root)
        provider = normalize_provider(os.getenv("VIDEO_ANALYSIS_VISION_PROVIDER") or config.vision_provider)
        return bool(resolve_provider_key(config, provider))
    except ValueError:
        return False


def vision_provider_capability(workspace_root: Path) -> tuple[bool, str]:
    """Report configured capability without pretending a model request ran."""
    try:
        config = load_runtime_config(workspace_root)
        requested = os.getenv("VIDEO_ANALYSIS_VISION_PROVIDER") or config.vision_provider
        provider = normalize_provider(requested)
    except ValueError as exc:
        return False, f"unsupported vision provider: {exc}"
    if provider == VisionProvider.bridgedeck.value:
        try:
            endpoint, _model = validate_bridgedeck_config(config.bridgedeck_base_url, config.bridgedeck_model)
        except ValueError as exc:
            return False, str(exc)
        return True, f"BridgeDeck account-scoped loopback route configured ({endpoint_origin(endpoint)}); no credentials forwarded; live inference and upstream token limits unverified"
    if provider == VisionProvider.openai.value:
        endpoint = config.openai_base_url
        configured_key = config.openai_api_key
        environment_key = os.getenv("OPENAI_API_KEY", "")
        environment_name = "OPENAI_API_KEY"
        official_origins = OFFICIAL_OPENAI_ORIGINS
    else:
        endpoint = config.minimax_api_host
        configured_key = config.minimax_api_key
        environment_key = os.getenv("MINIMAX_API_KEY", "")
        environment_name = "MINIMAX_API_KEY"
        official_origins = OFFICIAL_MINIMAX_ORIGINS
    try:
        origin = endpoint_origin(endpoint)
        key = resolve_provider_key(config, provider, selected_endpoint=endpoint)
    except ValueError as exc:
        return False, f"selected {provider} endpoint is invalid: {exc}"
    if key:
        source = "configured endpoint-bound key" if configured_key else f"ambient {environment_name} on an official origin"
        return True, f"{provider} credential eligible via {source} ({origin})"
    if environment_key and origin not in official_origins:
        return (
            False,
            f"ambient {environment_name} ignored for custom endpoint {origin}; configure a key bound to that exact endpoint",
        )
    return False, f"no eligible {provider} credential for selected endpoint {origin}"


def canonical_json_digest(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def canonical_shots_digest(shots: list[Shot]) -> str:
    return canonical_json_digest([shot.model_dump(mode="json") for shot in shots])


def canonical_readiness_payload(report: dict[str, Any]) -> dict[str, Any]:
    """Return the complete evidence decision, excluding local capability state.

    ``vision_key_configured`` and ``stored_readiness_valid`` are observations of
    the current process rather than evidence used to reach the readiness
    decision.  ``report_digest`` is self-referential and is therefore excluded.
    Every other field, including an unknown injected field, remains digest-bound.
    """
    excluded = {"report_digest", "stored_readiness_valid", "vision_key_configured"}
    return {key: value for key, value in report.items() if key not in excluded}


def evaluate_project_readiness(
    project_root: Path,
    *,
    workspace_root: Path | None = None,
    require_persisted_receipt: bool = True,
    _shots_lock_held: bool = False,
) -> dict[str, Any]:
    """Recompute readiness from current project evidence; never trust readiness.json as authority."""
    project = Path(os.path.abspath(os.fspath(project_root)))
    if _shots_lock_held:
        return _evaluate_project_readiness_locked(
            project,
            workspace_root=workspace_root,
            require_persisted_receipt=require_persisted_receipt,
        )
    with advisory_file_lock(project / "data" / ".shots.lock", root=project):
        return _evaluate_project_readiness_locked(
            project,
            workspace_root=workspace_root,
            require_persisted_receipt=require_persisted_receipt,
        )


def _evaluate_project_readiness_locked(
    project: Path,
    *,
    workspace_root: Path | None,
    require_persisted_receipt: bool,
) -> dict[str, Any]:
    try:
        raw_shots = load_json(project / "data" / "shots.json")
        if type(raw_shots) is not list:
            raise ValueError("shots.json must be a JSON array")
        shots = [_strict_shot(item) for item in raw_shots]
    except Exception as exc:
        return _invalid_project_report(
            project,
            workspace_root or project.parent,
            f"shots receipt is missing or invalid ({type(exc).__name__})",
        )

    media: CanonicalMediaPackage | None = None
    try:
        raw_media = load_json(project / "data" / "media_package.json")
        if type(raw_media) is not dict or any(
            _strict_number(raw_media.get(field), minimum=0.0) is None
            for field in ("duration_seconds", "frame_rate", "aspect_ratio")
        ):
            raise ValueError("media package numeric fields must be strict finite JSON numbers")
        media = CanonicalMediaPackage.model_validate(raw_media)
    except Exception:
        pass
    report = evaluate_readiness(
        shots,
        workspace_root=workspace_root or project.parent,
        project_root=project,
        media=media,
    )
    if require_persisted_receipt:
        persisted_reason = _persisted_readiness_problem(project, report)
        if persisted_reason:
            report["reasons"] = _unique([*report["reasons"], persisted_reason])
            report["status"] = "blocked"
            report["professional_export_allowed"] = False
            report["stored_readiness_valid"] = False
        else:
            report["stored_readiness_valid"] = True
    else:
        report["stored_readiness_valid"] = None
    return report


def evaluate_readiness(
    shots: list[Shot],
    workspace_root: Path | None = None,
    *,
    project_root: Path | None = None,
    media: CanonicalMediaPackage | None = None,
) -> dict[str, Any]:
    """Evaluate current evidence without mutating shot source records."""
    reasons: list[str] = []
    key_configured = has_vision_key(workspace_root) if workspace_root else False
    profile = _profile_value(media)
    critical_fields = ADS_CRITICAL_FIELDS if profile == "ads" else BASE_CRITICAL_FIELDS
    shot_count = len(shots)
    shots_digest: str | None = None
    try:
        shots_digest = canonical_shots_digest(shots)
    except (TypeError, ValueError):
        reasons.append("shots contain non-JSON or non-finite values")

    visual_result = _validate_visual_binding(project_root, shots)
    reasons.extend(visual_result["reasons"])
    boundary_review_result = validate_boundary_review_receipt(
        project_root,
        shots,
        visual_result.get("binding"),
    ) if project_root else {
        "valid": False,
        "present": False,
        "reviewed_shot_ids": set(),
        "binding": None,
        "reasons": [],
    }
    reasons.extend(boundary_review_result["reasons"])
    reviewed_boundary_ids: set[str] = boundary_review_result["reviewed_shot_ids"]
    media_result = _validate_media_binding(project_root, media) if project_root else _unbound_media_result(media)
    reasons.extend(media_result["reasons"])
    media_duration = media_result.get("duration_seconds")
    audio_review_result = _validate_audio_review(project_root)
    reasons.extend(audio_review_result["reasons"])

    primary_refs: list[str] = []
    frame_evidence: dict[tuple[str, str], dict[str, Any]] = {}
    shot_results: dict[str, dict[str, Any]] = {}
    empty_count = 0
    placeholder_count = 0
    detected_low_boundary_count = 0
    unreviewed_low_boundary_count = 0
    visual_confidence_total = 0.0
    previous: Shot | None = None
    seen_ids: set[str] = set()
    seen_numbers: set[int] = set()

    for index, shot in enumerate(shots):
        shot_reasons: list[str] = []
        shot_id = shot.shot_id.strip() if type(shot.shot_id) is str else ""
        duplicate_id = bool(shot_id and shot_id in seen_ids)
        result_key = shot_id if shot_id and not duplicate_id else f"index:{index}"
        if not shot_id:
            shot_reasons.append("shot_id must be non-empty")
        elif duplicate_id:
            shot_reasons.append("shot_id must be unique")
        seen_ids.add(shot_id)
        if isinstance(shot.shot_no, bool) or not isinstance(shot.shot_no, int) or shot.shot_no <= 0:
            shot_reasons.append("shot_no must be a positive integer")
        elif shot.shot_no in seen_numbers:
            shot_reasons.append("shot_no must be unique")
        seen_numbers.add(shot.shot_no)

        start = _strict_number(shot.start_time, minimum=0.0)
        end = _strict_number(shot.end_time, minimum=0.0)
        duration = _strict_number(shot.duration, minimum=0.0)
        if start is None or end is None or duration is None:
            shot_reasons.append("timeline values must be finite JSON numbers")
        else:
            if start >= end:
                shot_reasons.append("timeline requires 0 <= start_time < end_time")
            expected_duration = end - start
            if duration <= 0 or abs(duration - expected_duration) > max(TIMELINE_TOLERANCE_SECONDS, expected_duration * 0.001):
                shot_reasons.append("duration must match end_time - start_time")
            if isinstance(media_duration, (int, float)) and end > media_duration + MEDIA_DURATION_TOLERANCE_SECONDS:
                shot_reasons.append("shot end_time exceeds bound media duration")
            if previous is not None:
                previous_start = _strict_number(previous.start_time, minimum=0.0)
                previous_end = _strict_number(previous.end_time, minimum=0.0)
                if previous_start is not None and start < previous_start:
                    shot_reasons.append("shots must be ordered by start_time")
                if shot.shot_no <= previous.shot_no:
                    shot_reasons.append("shots must be ordered by shot_no")
                if previous_end is not None and start < previous_end - TIMELINE_TOLERANCE_SECONDS:
                    shot_reasons.append("overlapping shots are not allowed")
        previous = shot

        boundary_is_low = (shot.boundary_confidence or "low").strip().lower() == "low"
        boundary_reviewed = shot_id in reviewed_boundary_ids
        if boundary_is_low:
            detected_low_boundary_count += 1
            if not boundary_reviewed:
                unreviewed_low_boundary_count += 1
                shot_reasons.append("low boundary confidence requires explicit human boundary review")
        confidence = _strict_number(shot.visual_confidence, minimum=0.0, maximum=1.0)
        fallback_confidence = _strict_number(shot.confidence, minimum=0.0, maximum=1.0)
        if confidence is None or fallback_confidence is None:
            shot_reasons.append("confidence values must be finite JSON numbers between 0 and 1")
            confidence = 0.0
        visual_confidence_total += confidence

        for field in critical_fields:
            value = _field_value(shot, field)
            if _is_placeholder(value):
                empty_count += 1
                shot_reasons.append(f"missing {field}")
                if value.strip().lower().startswith("to annotate"):
                    placeholder_count += 1

        primary = _primary_ref(shot)
        refs = _unique_strings([shot.frame_ref, primary, *shot.frame_refs])
        if not refs:
            shot_reasons.append("primary frame reference is required")
        elif project_root:
            for reference in refs:
                try:
                    evidence = read_frame_evidence(project_root, reference)
                    frame_evidence[(shot_id, reference)] = evidence
                    if reference == primary:
                        primary_refs.append(str(evidence["relative_path"]))
                except Exception:
                    shot_reasons.append(f"frame reference is missing or unsafe: {reference}")
        elif primary:
            primary_refs.append(primary)

        source = (shot.annotation_source or "").strip().lower()
        human_assertion = source == "human" and (shot.readiness_status or "").strip().lower() == "ready"
        if source == "human" and not human_assertion:
            shot_reasons.append("human annotation requires an explicit ready operator assertion")
        if source == "codex":
            shot_reasons.append("Codex-submitted analysis requires explicit human review; model execution identity is unverified")
        if source not in {"human", *PROVIDER_SOURCES}:
            shot_reasons.append("verified annotation provenance required")
        result = {
            "shot_id": shot_id,
            "annotation_state": "human_assertion" if human_assertion else "provider_claim" if source in PROVIDER_SOURCES else "unverified",
            "human_assertion": human_assertion,
            "provider_receipt_verified": False,
            "agent_submission_verified": False,
            "boundary_reviewed": boundary_reviewed,
            "confidence": confidence,
            "reasons": _unique(shot_reasons),
        }
        shot_results[result_key] = result

    duplicate_primary = len(primary_refs) != len(set(primary_refs))
    if duplicate_primary:
        reasons.append("duplicate primary frame refs")

    vision_result = _validate_vision_receipt(
        project_root,
        shots,
        frame_evidence,
        media_result,
    ) if project_root else {"complete": False, "reasons": [], "binding": None, "verified_shot_ids": set()}
    verified_provider_ids: set[str] = vision_result["verified_shot_ids"]
    for shot_id in verified_provider_ids:
        if shot_id in shot_results:
            is_codex = any(shot.shot_id == shot_id and shot.annotation_source == "codex" for shot in shots)
            shot_results[shot_id]["provider_receipt_verified"] = not is_codex
            shot_results[shot_id]["agent_submission_verified"] = is_codex
            shot_results[shot_id]["annotation_state"] = "agent_submission_bound" if is_codex else "provider_receipt_verified"
    provider_claims = any((shot.annotation_source or "").strip().lower() in PROVIDER_SOURCES for shot in shots)
    if provider_claims:
        reasons.extend(vision_result["reasons"])

    for index, shot in enumerate(shots):
        duplicate_id = sum(1 for item in shots[:index] if item.shot_id.strip() == shot.shot_id.strip()) > 0
        key = shot.shot_id.strip() if shot.shot_id.strip() and not duplicate_id else f"index:{index}"
        result = shot_results[key]
        source = (shot.annotation_source or "").strip().lower()
        provenance_ok = result["human_assertion"] or (source in PROVIDER_SOURCES and result["provider_receipt_verified"])
        if not provenance_ok:
            result["reasons"].append("verified annotation provenance required")
        if not visual_result["valid"]:
            result["reasons"].extend(visual_result["reasons"])
        result["reasons"] = _unique(result["reasons"])
        result["professional_ready"] = not result["reasons"] and result["confidence"] >= 0.65 and provenance_ok

    total = max(shot_count, 1)
    critical_total = total * len(critical_fields)
    empty_rate = empty_count / max(critical_total, 1)
    average_confidence = visual_confidence_total / total
    detected_low_boundary_rate = detected_low_boundary_count / total
    low_boundary_rate = unreviewed_low_boundary_count / total
    incomplete_shots = sum(not item["professional_ready"] for item in shot_results.values())
    aligned_results = list(shot_results.values())
    vision_complete = bool(shots) and len(aligned_results) == len(shots) and bool(vision_result["complete"]) and all(
        (shot.annotation_source or "").strip().lower() in PROVIDER_SOURCES
        and shot.shot_id in verified_provider_ids
        and result["professional_ready"]
        for shot, result in zip(shots, aligned_results, strict=True)
    )
    human_complete = bool(shots) and len(aligned_results) == len(shots) and all(
        result["human_assertion"] and result["professional_ready"] for result in aligned_results
    )
    if not vision_complete and not human_complete:
        reasons.append("complete provider annotation or all-shot human review required")
    if incomplete_shots:
        reasons.append(f"{incomplete_shots} shot(s) fail professional readiness")
    if not shots:
        reasons.append("at least one shot is required")
    if placeholder_count:
        reasons.append("placeholder strings in professional fields")
    if empty_rate > 0.2:
        reasons.append(f"critical field empty rate {empty_rate:.0%} > 20%")
    if average_confidence < 0.65:
        reasons.append(f"average visual confidence {average_confidence:.2f} < 0.65")
    if low_boundary_rate > 0.3:
        reasons.append(f"low boundary confidence rate {low_boundary_rate:.0%} > 30%")
    if project_root and not media_result["valid"]:
        reasons.append("current versioned media receipt is required")

    reasons = _unique(reasons)
    status = "ready" if not reasons else "blocked"
    score = max(0.0, min(1.0, average_confidence - empty_rate * 0.25 - low_boundary_rate * 0.15))
    return {
        "schema_version": READINESS_SCHEMA_VERSION,
        "receipt_type": "video_evidence_readiness",
        "binding_version": READINESS_BINDING_VERSION,
        "status": status,
        "professional_export_allowed": status == "ready",
        "vision_key_configured": key_configured,
        "vision_annotation_complete": vision_complete,
        "human_review_override": human_complete,
        "human_assertion_policy": "Local single-user operator assertion: annotation_source=human and readiness_status=ready on every current shot.",
        "analysis_profile": profile,
        "shot_count": shot_count,
        "shots_digest": shots_digest,
        "visual_generation_binding": visual_result["binding"],
        "boundary_review_binding": boundary_review_result["binding"],
        "boundary_review_complete": bool(shots) and all(
            (shot.boundary_confidence or "low").strip().lower() != "low"
            or shot.shot_id in reviewed_boundary_ids
            for shot in shots
        ),
        "media_binding": media_result["binding"],
        "vision_receipt_binding": vision_result["binding"],
        "audio_timeline_available": audio_review_result["available"],
        "audio_review_complete": audio_review_result["complete"],
        "audio_event_count": audio_review_result["event_count"],
        "audio_requires_review_count": audio_review_result["requires_review_count"],
        "audio_intelligence_binding": audio_review_result["binding"],
        "duplicate_primary_frames": duplicate_primary,
        "critical_empty_rate": round(empty_rate, 3),
        "average_visual_confidence": round(average_confidence, 3),
        "detected_low_boundary_confidence_rate": round(detected_low_boundary_rate, 3),
        "low_boundary_confidence_rate": round(low_boundary_rate, 3),
        "score": round(score, 3),
        "shot_results": list(shot_results.values()),
        "reasons": reasons,
    }


def _validate_audio_review(project: Path | None) -> dict[str, Any]:
    neutral = {
        "available": None,
        "complete": None,
        "event_count": 0,
        "requires_review_count": 0,
        "binding": None,
        "reasons": [],
    }
    if project is None:
        return neutral
    from .audio_synthesis import audio_timeline_source, event_requires_review

    try:
        timeline, binding = audio_timeline_source(
            ProjectPaths(project), _shots_lock_held=True
        )
    except (OSError, ValueError, TypeError, RecursionError, OverflowError) as exc:
        return {
            **neutral,
            "available": True,
            "complete": False,
            "reasons": [f"audio intelligence is missing, unsafe, stale, or invalid ({type(exc).__name__})"],
        }
    if timeline is None:
        # Legacy or intentionally audio-unavailable projects remain eligible;
        # absence is recorded as unknown and never re-labelled as silence.
        return {**neutral, "available": False}
    unresolved = [
        str(event["event_id"])
        for event in timeline["events"]
        if event_requires_review(event)
    ]
    return {
        "available": True,
        "complete": not unresolved,
        "event_count": len(timeline["events"]),
        "requires_review_count": len(unresolved),
        "binding": binding,
        "reasons": (
            [f"{len(unresolved)} audio event(s) require explicit operator review"]
            if unresolved
            else []
        ),
    }


def write_readiness(path: Path, shots: list[Shot], workspace_root: Path | None = None) -> dict[str, Any]:
    project = path.parent.parent
    with advisory_file_lock(project / "data" / ".shots.lock", root=project):
        media: CanonicalMediaPackage | None = None
        try:
            media = CanonicalMediaPackage.model_validate(load_json(project / "data" / "media_package.json"))
        except Exception:
            pass
        report = evaluate_readiness(
            shots,
            workspace_root=workspace_root or project.parent,
            project_root=project,
            media=media,
        )
        report["report_digest"] = canonical_json_digest(canonical_readiness_payload(report))
        report["stored_readiness_valid"] = True
        dump_json(path, report)
        return report


def _invalid_project_report(project: Path, workspace: Path, reason: str) -> dict[str, Any]:
    report = evaluate_readiness([], workspace_root=workspace, project_root=project, media=None)
    report["reasons"] = _unique([reason, *report["reasons"]])
    report["status"] = "blocked"
    report["professional_export_allowed"] = False
    report["stored_readiness_valid"] = False
    return report


def _persisted_readiness_problem(project: Path, current: dict[str, Any]) -> str | None:
    try:
        stored = load_json(artifact_path(project, "readiness_json"))
    except Exception:
        return "current readiness receipt is missing; regenerate the evidence package"
    if type(stored) is not dict:
        return "stored readiness receipt is invalid; regenerate the evidence package"
    try:
        stored_payload = canonical_readiness_payload(stored)
        current_payload = canonical_readiness_payload(current)
        stored_digest = canonical_json_digest(stored_payload)
        current_digest = canonical_json_digest(current_payload)
    except (TypeError, ValueError):
        return "stored readiness receipt contains unsupported values; regenerate the evidence package"
    if type(stored.get("report_digest")) is not str or stored["report_digest"] != stored_digest:
        return "stored readiness receipt digest is missing or invalid; regenerate the evidence package"
    if stored_digest != current_digest or stored_payload != current_payload:
        return "stored readiness receipt is stale or forged; regenerate the evidence package"
    return None


def _validate_visual_binding(project: Path | None, shots: list[Shot]) -> dict[str, Any]:
    if project is None:
        return {"valid": True, "binding": None, "reasons": []}
    try:
        binding = visual_generation_binding(
            ProjectPaths(project),
            shots,
            file_receipt_reader=_cached_regular_file_receipt,
        )
    except ValueError as exc:
        return {"valid": False, "binding": None, "reasons": [str(exc)]}
    return {"valid": True, "binding": binding, "reasons": []}


def _validate_media_binding(project: Path | None, media: CanonicalMediaPackage | None) -> dict[str, Any]:
    reasons: list[str] = []
    binding: dict[str, Any] = {
        "status": "invalid",
        "receipt_schema_version": None,
        "media_package_sha256": None,
        "master_sha256": None,
        "review_sha256": None,
    }
    if project is None or media is None:
        return {"valid": False, "duration_seconds": None, "binding": binding, "reasons": ["media package is missing or invalid"]}
    try:
        package_data = media.model_dump(mode="json")
        binding["media_package_sha256"] = canonical_json_digest(package_data)
    except (TypeError, ValueError):
        reasons.append("media package contains non-finite or unsupported values")
        return {"valid": False, "duration_seconds": None, "binding": binding, "reasons": reasons}
    if media.project_id != project.name:
        reasons.append("media package project_id does not match project")
    receipt = media.metadata.get("media_receipt") if isinstance(media.metadata, dict) else None
    if type(receipt) is not dict or receipt.get("schema_version") != MEDIA_RECEIPT_SCHEMA_VERSION:
        reasons.append("versioned media receipt schema 1.0 is required")
        return {"valid": False, "duration_seconds": None, "binding": binding, "reasons": reasons}
    binding["receipt_schema_version"] = MEDIA_RECEIPT_SCHEMA_VERSION
    current: dict[str, dict[str, Any]] = {}
    for label, value, expected_dir in (
        ("master", media.local_master_path, project / "ingest"),
        ("review", media.review_copy_path, project / "assets"),
    ):
        item = receipt.get(label)
        if type(item) is not dict:
            reasons.append(f"media {label} receipt is missing")
            continue
        try:
            _path, digest, size = _hash_bound_regular_file(project, value, expected_dir)
        except Exception:
            reasons.append(f"bound {label} media file is missing, unsafe, or unreadable")
            continue
        if item.get("sha256") != digest:
            reasons.append(f"bound {label} media sha256 does not match receipt")
        if isinstance(item.get("size_bytes"), bool) or item.get("size_bytes") != size:
            reasons.append(f"bound {label} media size does not match receipt")
        receipt_duration = _strict_number(item.get("duration_seconds"), minimum=0.0)
        receipt_fps = _strict_number(item.get("frame_rate"), minimum=0.0)
        if receipt_duration is None or receipt_duration <= 0 or receipt_fps is None or receipt_fps <= 0:
            reasons.append(f"bound {label} media duration/frame_rate receipt is invalid")
            continue
        # Ingest generated these metadata fields from ffprobe and bound them to
        # the exact file SHA. Re-probing unchanged bytes on every readiness or
        # Range request adds substantial latency without strengthening lineage.
        current[label] = {"sha256": digest, "duration_seconds": receipt_duration, "frame_rate": receipt_fps}
        binding[f"{label}_sha256"] = digest
    package_duration = _strict_number(media.duration_seconds, minimum=0.0)
    package_fps = _strict_number(media.frame_rate, minimum=0.0)
    review = current.get("review")
    if package_duration is None or package_duration <= 0 or package_fps is None or package_fps <= 0:
        reasons.append("media package duration/frame_rate is invalid")
    elif review:
        if abs(package_duration - review["duration_seconds"]) > MEDIA_DURATION_TOLERANCE_SECONDS:
            reasons.append("media package duration does not match current review media")
        if abs(package_fps - review["frame_rate"]) > MEDIA_FRAME_RATE_TOLERANCE:
            reasons.append("media package frame_rate does not match current review media")
    valid = not reasons and set(current) == {"master", "review"}
    binding["status"] = "bound" if valid else "invalid"
    return {"valid": valid, "duration_seconds": package_duration, "binding": binding, "reasons": reasons}


def _unbound_media_result(media: CanonicalMediaPackage | None) -> dict[str, Any]:
    duration = _strict_number(media.duration_seconds, minimum=0.0) if media else None
    return {"valid": True, "duration_seconds": duration, "binding": None, "reasons": []}


def _validate_vision_receipt(
    project: Path | None,
    shots: list[Shot],
    frame_evidence: dict[tuple[str, str], dict[str, Any]],
    media_result: dict[str, Any],
) -> dict[str, Any]:
    result = {"complete": False, "reasons": [], "binding": None, "verified_shot_ids": set()}
    if project is None:
        return result
    try:
        receipt = load_json(project / "data" / "vision_annotations.json")
    except Exception:
        result["reasons"] = ["versioned vision receipt is missing"]
        return result
    if type(receipt) is not dict:
        result["reasons"] = ["vision receipt must be a JSON object"]
        return result
    try:
        receipt_digest = canonical_json_digest(receipt)
    except (TypeError, ValueError):
        result["reasons"] = ["vision receipt contains non-finite or unsupported values"]
        return result
    provider = receipt.get("provider")
    binding = {
        "schema_version": receipt.get("schema_version"),
        "receipt_sha256": receipt_digest,
        "run_id": receipt.get("run_id"),
        "provider": provider,
        "model": receipt.get("model"),
    }
    result["binding"] = binding
    reasons: list[str] = []
    if receipt.get("schema_version") != VISION_RECEIPT_SCHEMA_VERSION:
        reasons.append("vision receipt schema 1.0 is required")
    if provider not in PROVIDER_SOURCES:
        reasons.append("vision receipt provider is unsupported")
    if receipt.get("provider_source") != PROVIDER_SOURCE_TYPES.get(str(provider)):
        reasons.append("vision receipt provider_source does not match provider")
    if provider == "bridgedeck":
        if receipt.get("provider_contract") != BRIDGE_PROVIDER_CONTRACT:
            reasons.append("BridgeDeck receipt transport contract is invalid")
        try:
            origin = receipt.get("endpoint_origin")
            if type(origin) is not str:
                raise ValueError("missing loopback origin")
            validate_bridgedeck_config(origin + "/accounts/receipt-bound/v1", receipt.get("model"))
        except ValueError:
            reasons.append("BridgeDeck receipt endpoint or model is invalid")
    if provider == "codex":
        from .codex_analysis import validate_codex_submission_receipt

        try:
            validate_codex_submission_receipt(ProjectPaths(project), receipt)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            reasons.append(f"Codex submission is invalid or stale: {type(exc).__name__}")
    for field in ("run_id", "started_at", "completed_at", "model", "endpoint_origin"):
        if type(receipt.get(field)) is not str or not receipt[field].strip():
            reasons.append(f"vision receipt {field} is missing")
    media_binding = receipt.get("media_binding")
    expected_media = media_result.get("binding") or {}
    if type(media_binding) is not dict or media_binding.get("status") != "bound":
        reasons.append("vision receipt is not bound to a media package")
    else:
        for key in ("media_package_sha256", "receipt_schema_version", "master_sha256", "review_sha256"):
            if media_binding.get(key) != expected_media.get(key):
                reasons.append(f"vision receipt media binding mismatch: {key}")

    shot_ids = [shot.shot_id for shot in shots]
    selected = _strict_string_list(receipt.get("selected_shot_ids"))
    annotated = _strict_string_list(receipt.get("annotated_shot_ids"))
    skipped = _strict_string_list(receipt.get("skipped_shot_ids"))
    if selected is None or annotated is None or skipped is None:
        reasons.append("vision receipt shot id lists are invalid")
        selected, annotated, skipped = [], [], []
    if len(selected) != len(set(selected)) or len(annotated) != len(set(annotated)) or len(skipped) != len(set(skipped)):
        reasons.append("vision receipt shot id lists contain duplicates")
    if selected != shot_ids or annotated != shot_ids or skipped:
        reasons.append("vision receipt must annotate every current shot exactly once")

    shot_receipts = _index_dicts(receipt.get("shot_receipts"), "shot_id")
    input_frames = _index_dicts(receipt.get("input_frames"), "shot_id")
    annotations = _index_dicts(receipt.get("annotations"), "shot_id")
    if shot_receipts is None or input_frames is None or annotations is None:
        reasons.append("vision receipt per-shot records are invalid or duplicated")
        shot_receipts, input_frames, annotations = {}, {}, {}
    if set(shot_receipts) != set(shot_ids) or set(input_frames) != set(shot_ids) or set(annotations) != set(shot_ids):
        reasons.append("vision receipt per-shot records do not match current shots")

    globally_valid = not reasons and media_result.get("valid") is True
    verified: set[str] = set()
    if globally_valid:
        for shot in shots:
            shot_id = shot.shot_id
            shot_receipt = shot_receipts[shot_id]
            frame_receipt = input_frames[shot_id]
            annotation = annotations[shot_id]
            try:
                current_digest = canonical_json_digest(shot.model_dump(mode="json"))
                annotation_digest = canonical_json_digest(annotation)
            except (TypeError, ValueError):
                reasons.append(f"vision receipt shot digest is invalid: {shot_id}")
                continue
            source = (shot.annotation_source or "").strip().lower()
            reference = str(frame_receipt.get("frame_ref") or "")
            current_frame = frame_evidence.get((shot_id, reference))
            frame_digest = current_frame.get("sha256") if current_frame else None
            if source != provider:
                reasons.append(f"vision receipt provider does not match current annotation_source: {shot_id}")
            elif shot_receipt.get("shot_sha256") != current_digest or annotation_digest != current_digest:
                reasons.append(f"vision receipt is stale for shot: {shot_id}")
            elif not frame_digest or shot_receipt.get("frame_sha256") != frame_digest or any(
                frame_receipt.get(field) != current_frame.get(field)
                for field in ("sha256", "size_bytes", "media_type", "width", "height")
            ):
                reasons.append(f"vision receipt frame digest mismatch: {shot_id}")
            elif reference != shot.frame_ref:
                reasons.append(f"vision receipt frame_ref mismatch: {shot_id}")
            else:
                verified.add(shot_id)
    result["verified_shot_ids"] = verified
    result["reasons"] = _unique(reasons)
    result["complete"] = not reasons and verified == set(shot_ids) and bool(shots)
    return result


def _hash_bound_regular_file(project: Path, value: str, expected_dir: Path) -> tuple[Path, str, int]:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = project / candidate
    lexical = Path(os.path.abspath(os.fspath(candidate)))
    project_lexical = Path(os.path.abspath(os.fspath(project)))
    expected_lexical = Path(os.path.abspath(os.fspath(expected_dir)))
    lexical.relative_to(project_lexical)
    lexical.relative_to(expected_lexical)
    current = _cached_regular_file_receipt(lexical, MAX_BOUND_MEDIA_BYTES)
    size = current["size_bytes"]
    if size <= 0:
        raise ValueError("bound media is empty")
    return lexical, current["sha256"], size


def read_frame_evidence(project: Path, reference: str) -> dict[str, Any]:
    raw = reference.strip()
    relative = Path(raw)
    if not raw or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("unsafe frame reference")
    keyframes = project / "assets" / "keyframes"
    if relative.is_absolute():
        candidate = relative
    elif relative.parts[:2] == ("assets", "keyframes"):
        candidate = project / relative
    else:
        candidate = keyframes / relative
    lexical = Path(os.path.abspath(os.fspath(candidate)))
    lexical.relative_to(Path(os.path.abspath(os.fspath(keyframes))))
    keyframes_root = Path(os.path.abspath(os.fspath(keyframes)))
    relative_path = lexical.relative_to(keyframes_root).as_posix()
    image_receipt = _cached_image_receipt(lexical)
    return {
        "path": str(lexical),
        "relative_path": relative_path,
        **image_receipt,
    }


def _cached_regular_file_receipt(path: Path, max_bytes: int) -> dict[str, Any]:
    from .media import _open_regular_no_symlinks

    with _open_regular_no_symlinks(path) as descriptor:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"bound artifact is not a regular file: {path}")
        if before.st_size > max_bytes:
            raise ValueError(f"bound artifact exceeds size limit: {path}")
        fingerprint = _stat_fingerprint(before)
        cached = _validation_cache_get(("sha256", *fingerprint))
        if cached is not None:
            return cached
        digest, size = _digest_descriptor(descriptor, max_bytes)
        after = os.fstat(descriptor)
        if _stat_fingerprint(after) != fingerprint:
            raise ValueError(f"bound artifact changed during validation: {path}")
    receipt = {
        "sha256": digest,
        "size_bytes": size,
    }
    _validation_cache_put(("sha256", *fingerprint), receipt)
    return dict(receipt)


def _cached_image_receipt(path: Path) -> dict[str, Any]:
    from .media import _open_regular_no_symlinks

    with _open_regular_no_symlinks(path) as descriptor:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size <= 0:
            raise ValueError(f"frame evidence is not a non-empty regular file: {path}")
        if before.st_size > MAX_IMAGE_BYTES:
            raise ValueError(f"frame evidence exceeds size limit: {path}")
        fingerprint = _stat_fingerprint(before)
        cached = _validation_cache_get(("image", *fingerprint))
        if cached is not None:
            return cached
        payload = _read_descriptor_bytes(descriptor, MAX_IMAGE_BYTES)
        after = os.fstat(descriptor)
        if _stat_fingerprint(after) != fingerprint:
            raise ValueError(f"frame evidence changed during validation: {path}")
    receipt = dict(inspect_image_bytes(payload).receipt_fields())
    _validation_cache_put(("image", *fingerprint), receipt)
    return dict(receipt)


def _read_descriptor_bytes(descriptor: int, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = os.read(descriptor, min(1024 * 1024, max_bytes + 1 - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
        if size > max_bytes:
            raise ValueError("bound artifact exceeds size limit")
    return b"".join(chunks)


def _digest_descriptor(descriptor: int, max_bytes: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            break
        size += len(chunk)
        if size > max_bytes:
            raise ValueError("bound artifact exceeds size limit")
        digest.update(chunk)
    return digest.hexdigest(), size


def _stat_fingerprint(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _validation_cache_get(key: tuple[Any, ...]) -> dict[str, Any] | None:
    with _VALIDATION_CACHE_LOCK:
        value = _VALIDATION_CACHE.get(key)
        if value is None:
            return None
        _VALIDATION_CACHE.move_to_end(key)
        return dict(value)


def _validation_cache_put(key: tuple[Any, ...], value: dict[str, Any]) -> None:
    with _VALIDATION_CACHE_LOCK:
        _VALIDATION_CACHE[key] = dict(value)
        _VALIDATION_CACHE.move_to_end(key)
        while len(_VALIDATION_CACHE) > VALIDATION_CACHE_MAX_ENTRIES:
            _VALIDATION_CACHE.popitem(last=False)


def _clear_validation_caches() -> None:
    with _VALIDATION_CACHE_LOCK:
        _VALIDATION_CACHE.clear()


# Compatibility for callers that previously treated this as a module-private
# helper.  New code should use the public, receipt-oriented name above.
_read_frame_evidence = read_frame_evidence


def _profile_value(media: CanonicalMediaPackage | None) -> str:
    if media is None:
        return "research"
    value = getattr(media.analysis_profile, "value", media.analysis_profile)
    normalized = str(value or "research").strip().lower()
    return normalized if normalized in {"ads", "research", "streaming", "shortform", "festival"} else "research"


def _strict_shot(value: Any) -> Shot:
    try:
        return Shot.model_validate(value, strict=True)
    except TypeError:  # lightweight fallback model used only without Pydantic
        return Shot.model_validate(value)


def _primary_ref(shot: Shot) -> str:
    return (shot.primary_frame_ref or shot.frame_ref or "").strip()


def _field_value(shot: Shot, field: str) -> str:
    if field == "primary_frame_ref":
        return _primary_ref(shot)
    if field == "content_summary":
        return shot.content_summary or shot.content_summary_zh
    if field == "subject":
        return shot.subject or shot.subject_zh
    if field == "action":
        return shot.action or shot.action_zh
    value = getattr(shot, field, "")
    return value if isinstance(value, str) else str(value)


def _is_placeholder(value: str) -> bool:
    normalized = value.strip().lower()
    return normalized in PLACEHOLDER_VALUES or normalized.startswith("to annotate") or normalized.startswith("heuristic_unverified:")


def _strict_number(value: Any, *, minimum: float | None = None, maximum: float | None = None) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    if not math.isfinite(numeric):
        return None
    if minimum is not None and numeric < minimum:
        return None
    if maximum is not None and numeric > maximum:
        return None
    return numeric


def _strict_string_list(value: Any) -> list[str] | None:
    if type(value) is not list or any(type(item) is not str or not item for item in value):
        return None
    return list(value)


def _index_dicts(value: Any, key: str) -> dict[str, dict[str, Any]] | None:
    if type(value) is not list:
        return None
    result: dict[str, dict[str, Any]] = {}
    for item in value:
        if type(item) is not dict or type(item.get(key)) is not str or not item[key] or item[key] in result:
            return None
        result[item[key]] = item
    return result


def _unique_strings(values: list[str]) -> list[str]:
    return [item for item in dict.fromkeys(value.strip() for value in values if type(value) is str and value.strip())]


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))
