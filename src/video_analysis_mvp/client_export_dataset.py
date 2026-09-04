"""Build the single generation-bound dataset consumed by all client renderers."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import unicodedata
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import urlsplit

from ._audio_intelligence_metadata import bounded_text
from .artifacts import artifact_path
from .audio_synthesis import event_requires_review
from .paths import ProjectPaths
from .readiness import canonical_json_digest, canonical_shots_digest
from .safe_io import advisory_file_lock, atomic_write_bytes, read_regular_bytes
from .schemas import CanonicalMediaPackage, Scene, Shot
from .synthesis import verify_report_generation_manifest

SCHEMA_ID = "client-export-dataset/v1"
DIGEST_ALGORITHM = "sha256"
MAX_TEXT_BYTES = 256 * 1024
MAX_DATASET_BYTES = 64 * 1024 * 1024
MAX_SHOTS = 20_000
MAX_AUDIO_EVENTS = 100_000
TEXT_CELL_KEYS = frozenset({"text", "spreadsheet_text", "is_blank", "formula_neutralized"})
PRIVATE_PATH_PATTERN = re.compile(
    r"(?i)(?:file:(?://)?/\S+"
    r"|(?<![\w])~[/\\]\S+"
    r"|(?<![\w/])/(?!/)[^/\s,;)]+(?:/[^/\s,;)]+)+"
    r"|(?<![\w])[a-z]:[/\\]\S+|(?<![\w])\\\\[^\\\s]+\\\S*)"
)
IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
EVENT_KINDS = ("voice", "music", "sfx", "silence", "mixed")
SHOT_TEXT_KEYS = frozenset(
    {
        "story_beat", "visual_description", "content_summary", "content_summary_zh",
        "subject", "subject_zh", "action", "action_zh", "onscreen_text", "dialogue",
        "speech_summary", "sound_design", "music_state", "rhythm_notes", "sound_rhythm",
        "transition_in", "transition_out", "review_notes",
    }
)
TOP_LEVEL_KEYS = frozenset(
    {
        "schema_id",
        "digest_algorithm",
        "dataset_id",
        "dataset_digest",
        "source_bindings",
        "project",
        "delivery_status",
        "field_semantics",
        "scenes",
        "shots",
        "audio",
        "limitations",
        "unresolved_items",
    }
)


class ClientExportDatasetError(ValueError):
    """The current project cannot produce one honest renderer input."""


def build_client_export_dataset(
    paths: ProjectPaths,
    *,
    _shots_lock_held: bool = False,
) -> dict[str, Any]:
    """Return a deterministic projection; never write or render an artifact."""
    if _shots_lock_held:
        return _build_locked(paths)
    with advisory_file_lock(paths.data / ".shots.lock", root=paths.root):
        return _build_locked(paths)


def write_client_export_dataset(paths: ProjectPaths) -> dict[str, Any]:
    """Explicitly replace the stable dataset slot, without producing client files."""
    with advisory_file_lock(paths.data / ".shots.lock", root=paths.root):
        dataset = _build_locked(paths)
        payload = _pretty_bytes(dataset)
        if len(payload) > MAX_DATASET_BYTES:
            raise ClientExportDatasetError("client export dataset exceeds 64 MiB")
        target = artifact_path(paths.root, "client_export_dataset")
        atomic_write_bytes(target, payload, root=paths.root)
        try:
            readback = json.loads(read_regular_bytes(target, root=paths.root, max_bytes=MAX_DATASET_BYTES))
            validate_client_export_dataset(readback)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ClientExportDatasetError("client export dataset write could not be verified") from exc
        return dataset


def _build_locked(paths: ProjectPaths) -> dict[str, Any]:
    valid, reasons = verify_report_generation_manifest(paths, _shots_lock_held=True)
    if not valid:
        detail = "; ".join(reasons[:4]) or "unknown report-generation failure"
        raise ClientExportDatasetError(f"a current committed report is required: {detail}")
    try:
        manifest_bytes = read_regular_bytes(paths.manifest, root=paths.root, max_bytes=MAX_DATASET_BYTES)
        manifest = _json_bytes(manifest_bytes, "manifest")
        media_bytes, media_value = _read_json_file(paths, paths.data / "media_package.json", "media package")
        _shots_bytes, shots_value = _read_json_file(paths, paths.data / "shots.json", "shots")
        scenes_bytes, scenes_value = _read_json_file(paths, paths.data / "scenes.json", "scenes")
        visualization_bytes, visualization = _read_json_file(
            paths, artifact_path(paths.root, "visualization_dataset"), "visualization dataset"
        )
        visual_receipt_bytes, visual_receipt = _read_json_file(
            paths, paths.data / "visual_generation.json", "visual generation receipt"
        )
        media = CanonicalMediaPackage.model_validate(media_value)
        if type(shots_value) is not list or type(scenes_value) is not list:
            raise ClientExportDatasetError("shots/scenes must be JSON arrays")
        shots = [Shot.model_validate(item) for item in shots_value]
        scenes = [Scene.model_validate(item) for item in scenes_value]
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ClientExportDatasetError(f"current report inputs are unreadable or invalid: {type(exc).__name__}") from exc
    if type(manifest) is not dict or type(visualization) is not dict:
        raise ClientExportDatasetError("current report inputs must be JSON objects")
    generation = manifest.get("report_generation")
    if type(generation) is not dict:
        raise ClientExportDatasetError("current committed report generation is missing")
    _verify_input_snapshot(
        generation,
        manifest,
        media_bytes=media_bytes,
        shots=shots,
        scenes_bytes=scenes_bytes,
        visualization_bytes=visualization_bytes,
        visual_receipt_bytes=visual_receipt_bytes,
        visual_receipt=visual_receipt,
    )
    if media.project_id != paths.root.name or visualization.get("project", {}).get("project_id") != media.project_id:
        raise ClientExportDatasetError("client dataset project bindings do not match")
    if len(shots) > MAX_SHOTS:
        raise ClientExportDatasetError(f"client dataset exceeds {MAX_SHOTS} shots")

    visual_shots = visualization.get("shots")
    audio = visualization.get("audio_associations")
    if type(visual_shots) is not list or type(audio) is not dict:
        raise ClientExportDatasetError("visualization dataset lacks shot/audio evidence")
    visual_by_id = _unique_by_id(visual_shots, "shot_id", "visualization shots")
    audio_shots = audio.get("shots")
    audio_events = audio.get("events")
    if type(audio_shots) is not list or type(audio_events) is not list:
        raise ClientExportDatasetError("audio associations lack shot/event records")
    audio_by_shot = _unique_by_id(audio_shots, "shot_id", "audio shot associations")
    ordered_ids = [shot.shot_id for shot in sorted(shots, key=lambda item: (item.shot_no, item.start_time, item.shot_id))]
    if set(ordered_ids) != set(visual_by_id) or set(ordered_ids) != set(audio_by_shot):
        raise ClientExportDatasetError("shot coverage differs across current report evidence")

    event_records = [_audio_event(item) for item in audio_events]
    event_ids = [item["event_id"] for item in event_records]
    if len(event_ids) > MAX_AUDIO_EVENTS or len(event_ids) != len(set(event_ids)):
        raise ClientExportDatasetError("audio event coverage is excessive or duplicated")
    event_by_id = {item["event_id"]: item for item in event_records}
    scene_membership: dict[str, list[str]] = {shot_id: [] for shot_id in ordered_ids}
    seen_scene_ids: set[str] = set()
    for scene in scenes:
        if (
            scene.scene_id in seen_scene_ids
            or len(scene.shot_ids) != len(set(scene.shot_ids))
            or any(shot_id not in scene_membership for shot_id in scene.shot_ids)
        ):
            raise ClientExportDatasetError("scene coverage contains duplicate or unknown ids")
        seen_scene_ids.add(scene.scene_id)
        for shot_id in scene.shot_ids:
            scene_membership[shot_id].append(scene.scene_id)
    shot_records = [
        _shot_record(
            shot,
            visual_by_id[shot.shot_id],
            audio_by_shot[shot.shot_id],
            event_by_id,
            scene_membership[shot.shot_id],
        )
        for shot in sorted(shots, key=lambda item: (item.shot_no, item.start_time, item.shot_id))
    ]
    scene_records = [_scene_record(scene) for scene in sorted(scenes, key=lambda item: (item.start_time, item.end_time, item.scene_id))]
    readiness = visualization.get("readiness")
    if type(readiness) is not dict:
        raise ClientExportDatasetError("visualization readiness is missing")
    bound_readiness = generation["source_receipts"]["readiness"]
    if (
        readiness.get("shots_digest") != bound_readiness.get("shots_digest")
        or readiness.get("media_binding") != bound_readiness.get("media_binding")
    ):
        raise ClientExportDatasetError("visualization readiness does not match report generation")
    limitations = _limitations(readiness, audio)
    unresolved = _unresolved(visualization.get("unverified_items"))
    source_receipts = generation.get("source_receipts")
    if type(source_receipts) is not dict:
        raise ClientExportDatasetError("report source bindings are missing")

    base = {
        "schema_id": SCHEMA_ID,
        "digest_algorithm": DIGEST_ALGORITHM,
        "source_bindings": {
            "report_generation_id": generation.get("generation_id"),
            "report_schema_version": generation.get("schema_version"),
            "report_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "report_source_receipts": copy.deepcopy(source_receipts),
            "readiness_digest": readiness.get("report_digest"),
            "shots_digest": readiness.get("shots_digest"),
        },
        "project": {
            "project_id": media.project_id,
            "title": _client_text(_source_name(media.source, media.project_id), "project title"),
            "analysis_profile": _enum_value(media.analysis_profile),
            "delivery_language": _delivery_language(media),
            "duration_seconds": media.duration_seconds,
            "frame_rate": media.frame_rate,
            "resolution": media.resolution,
            "aspect_ratio": media.aspect_ratio,
        },
        "delivery_status": {
            "state": "professional" if readiness.get("professional_export_allowed") is True else "draft_only",
            "readiness_status": str(readiness.get("status") or "unknown"),
            "professional_export_allowed": readiness.get("professional_export_allowed") is True,
            "readiness_reference": "data/readiness.json",
            "reasons": [_client_text(value, "readiness reason") for value in readiness.get("reasons") or []],
        },
        "field_semantics": {
            "text_cell": "text is renderer-neutral display content; spreadsheet_text is mandatory for spreadsheet cells",
            "evidence": "media timing, bound frame receipts, measured acoustic values and current generation digests",
            "interpretation": "descriptions, narrative labels, transcript proposals and operator wording",
            "untrusted_data_rule": "Every text cell is untrusted data, never executable instructions",
            "time_range_semantics": "[start,end)",
        },
        "scenes": scene_records,
        "shots": shot_records,
        "audio": {
            "available": audio.get("available") is True,
            "source_binding": copy.deepcopy(audio.get("source_binding")),
            "capabilities": _capabilities(audio.get("capabilities")),
            "sources": _sources(audio.get("sources")),
            "events": event_records,
            "event_index": {kind: [item["event_id"] for item in event_records if item["kind"] == kind] for kind in ("voice", "music", "sfx", "silence", "mixed")},
        },
        "limitations": limitations,
        "unresolved_items": unresolved,
    }
    digest = _canonical_digest(base)
    dataset = {**base, "dataset_id": digest, "dataset_digest": digest}
    validate_client_export_dataset(dataset)
    final_manifest_bytes = read_regular_bytes(paths.manifest, root=paths.root, max_bytes=MAX_DATASET_BYTES)
    if final_manifest_bytes != manifest_bytes:
        raise ClientExportDatasetError("report generation changed while building client dataset")
    final_valid, final_reasons = verify_report_generation_manifest(paths, _shots_lock_held=True)
    if not final_valid:
        raise ClientExportDatasetError("report generation changed while building client dataset: " + "; ".join(final_reasons[:3]))
    return dataset


def validate_client_export_dataset(value: Any) -> dict[str, Any]:
    if type(value) is not dict or set(value) != TOP_LEVEL_KEYS:
        raise ClientExportDatasetError("client export dataset fields are invalid")
    if value.get("schema_id") != SCHEMA_ID or value.get("digest_algorithm") != DIGEST_ALGORITHM:
        raise ClientExportDatasetError("client export dataset schema or digest algorithm is unsupported")
    digest = value.get("dataset_digest")
    if type(digest) is not str or len(digest) != 64 or value.get("dataset_id") != digest:
        raise ClientExportDatasetError("client export dataset digest is invalid")
    base = {key: item for key, item in value.items() if key not in {"dataset_id", "dataset_digest"}}
    if _canonical_digest(base) != digest:
        raise ClientExportDatasetError("client export dataset digest does not match its content")
    shots = value.get("shots")
    events = value.get("audio", {}).get("events") if type(value.get("audio")) is dict else None
    if type(shots) is not list or len(shots) > MAX_SHOTS or type(events) is not list or len(events) > MAX_AUDIO_EVENTS:
        raise ClientExportDatasetError("client export dataset collections are invalid")
    if len({item.get("shot_id") for item in shots if type(item) is dict}) != len(shots):
        raise ClientExportDatasetError("client export dataset shot ids are duplicated")
    if len({item.get("event_id") for item in events if type(item) is dict}) != len(events):
        raise ClientExportDatasetError("client export dataset event ids are duplicated")
    _validate_structure(value)
    _validate_tree(value, "dataset", depth=0, count=[0])
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ClientExportDatasetError("client export dataset is not finite JSON") from exc
    if len(encoded) > MAX_DATASET_BYTES:
        raise ClientExportDatasetError("client export dataset exceeds 64 MiB")
    return value


def _shot_record(
    shot: Shot,
    visual: dict[str, Any],
    audio: dict[str, Any],
    event_by_id: dict[str, dict[str, Any]],
    scene_ids: list[str],
) -> dict[str, Any]:
    if type(visual) is not dict or type(audio) is not dict:
        raise ClientExportDatasetError(f"shot evidence is invalid: {shot.shot_id}")
    evidence = visual.get("evidence_refs")
    frame = evidence.get("primary_frame") if type(evidence) is dict else None
    links = audio.get("event_links")
    if type(links) is not list:
        raise ClientExportDatasetError(f"shot audio links are invalid: {shot.shot_id}")
    normalized_links = []
    for link in links:
        if type(link) is not dict or link.get("event_id") not in event_by_id:
            raise ClientExportDatasetError(f"shot audio link references an unknown event: {shot.shot_id}")
        event_id = link["event_id"]
        normalized_links.append({
            "event_id": event_id,
            "kind": event_by_id[event_id]["kind"],
            **{key: copy.deepcopy(link.get(key)) for key in (
                "overlap_start", "overlap_end", "overlap_seconds", "event_fraction",
                "range_fraction", "continues_from_previous",
            )},
            "continues_to_next": link.get("continues_into_next"),
        })
    text = {
        key: _client_text(getattr(shot, key), f"shot {shot.shot_id} {key}")
        for key in (
            "story_beat", "visual_description", "content_summary", "content_summary_zh", "subject", "subject_zh",
            "action", "action_zh", "onscreen_text", "dialogue", "speech_summary", "sound_design",
            "music_state", "rhythm_notes", "sound_rhythm", "transition_in", "transition_out", "review_notes",
        )
    }
    return {
        "shot_id": shot.shot_id,
        "shot_no": shot.shot_no,
        "scene_ids": [str(item) for item in scene_ids],
        "start_seconds": shot.start_time,
        "end_seconds": shot.end_time,
        "duration_seconds": shot.duration,
        "timecode": _client_text(shot.timecode, f"shot {shot.shot_id} timecode"),
        "frame": _frame(frame),
        "text": text,
        "camera": {
            "shot_scale": _client_text(shot.shot_scale, "shot scale"),
            "angle": _client_text(shot.camera_angle, "camera angle"),
            "motion": _client_text(shot.camera_motion, "camera motion"),
            "composition": _client_text(shot.composition, "composition"),
        },
        "audio": {
            "event_links": normalized_links,
            "event_coverage_seconds": copy.deepcopy(audio.get("event_coverage_seconds") or {}),
            "summary": _client_text(str(audio.get("summary") or ""), "shot audio summary"),
            "source_reference": "data/client_export_dataset.json#audio.events",
        },
        "verification": {
            "annotation_source": str(shot.annotation_source or "unknown"),
            "annotation_verification": str(visual.get("annotation", {}).get("verification_status") or "unverified"),
            "visual_confidence": shot.visual_confidence,
            "readiness_status": shot.readiness_status,
            "readiness_reasons": [_client_text(item, "shot readiness reason") for item in shot.readiness_reasons],
        },
        "evidence_reference": f"data/shots.json#shot_id={shot.shot_id}",
    }


def _audio_event(value: Any) -> dict[str, Any]:
    if type(value) is not dict:
        raise ClientExportDatasetError("audio event is invalid")
    original = value.get("proposal")
    effective = value.get("effective_proposal")
    review = value.get("review")
    result = {
        "event_id": value.get("event_id"),
        "kind": value.get("kind"),
        "source_id": value.get("source_id"),
        "start_seconds": value.get("start_time"),
        "end_seconds": value.get("end_time"),
        "identity_status": value.get("identity_status"),
        "requires_review": event_requires_review(value),
        "proposal_sha256": value.get("proposal_sha256"),
        "original_proposal": _proposal(original, "original audio proposal"),
        "effective_proposal": None if effective is None else _proposal(effective, "effective audio proposal"),
        "review": None,
        "evidence_reference": str(value.get("evidence_ref") or ""),
    }
    if review is not None:
        if type(review) is not dict:
            raise ClientExportDatasetError("audio review is invalid")
        result["review"] = {
            "status": review.get("status"),
            "verification": review.get("verification"),
            "review_notes": _client_text(str(review.get("review_notes") or ""), "audio review notes"),
        }
    return result


def _proposal(value: Any, label: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise ClientExportDatasetError(f"{label} is invalid")
    return {
        "label": _client_text(str(value.get("label") or ""), f"{label} label"),
        "text": _client_text(str(value.get("text") or ""), f"{label} text"),
        "language": _client_text(str(value.get("language") or "unknown"), f"{label} language"),
        "speaker_id": value.get("speaker_id"),
        "voice_role": str(value.get("voice_role") or "unknown"),
        "energy": value.get("energy"),
        "onset_density": value.get("onset_density"),
        "estimated_bpm": value.get("estimated_bpm"),
        "confidence": value.get("confidence"),
        "verification": str(value.get("verification") or "unknown"),
    }


def _scene_record(scene: Scene) -> dict[str, Any]:
    return {
        "scene_id": scene.scene_id,
        "start_seconds": scene.start_time,
        "end_seconds": scene.end_time,
        "shot_ids": list(scene.shot_ids),
        "function": _client_text(scene.scene_function, f"scene {scene.scene_id} function"),
        "pace": _client_text(scene.pace_label, f"scene {scene.scene_id} pace"),
        "confidence": scene.confidence,
    }


def _frame(value: Any) -> dict[str, Any]:
    if value is None:
        return {"path": None, "present": False, "sha256": None, "size_bytes": None, "media_type": None, "width": None, "height": None, "failure": _client_text("Primary frame unavailable", "frame failure")}
    if type(value) is not dict:
        raise ClientExportDatasetError("primary frame evidence is invalid")
    result = {
        "path": value.get("path"),
        "present": value.get("present") is True,
        "sha256": value.get("sha256"),
        "size_bytes": value.get("size_bytes"),
        "media_type": value.get("media_type"),
        "width": value.get("width"),
        "height": value.get("height"),
        "failure": _client_text(str(value.get("failure") or ""), "frame failure"),
    }
    _validate_frame(result)
    return result


def _validate_frame(frame: dict[str, Any]) -> None:
    expected = {"path", "present", "sha256", "size_bytes", "media_type", "width", "height", "failure"}
    if set(frame) != expected or type(frame.get("present")) is not bool:
        raise ClientExportDatasetError("client export frame fields are invalid")
    path = frame.get("path")
    if path is not None and (type(path) is not str or not _safe_relative(path)):
        raise ClientExportDatasetError("client export frame path must be project-relative")
    _text_cell(frame.get("failure"), "frame failure")
    if frame["present"]:
        if not path:
            raise ClientExportDatasetError("present client export frame lacks a path")
        _sha(frame.get("sha256"), "frame digest")
        _finite(frame.get("size_bytes"), "frame size", integer=True, minimum=1)
        if frame.get("media_type") not in {"image/jpeg", "image/png"}:
            raise ClientExportDatasetError("frame media type is unsupported")
        for key in ("width", "height"):
            _finite(frame.get(key), f"frame {key}", integer=True, minimum=1)
        if not frame["failure"]["is_blank"]:
            raise ClientExportDatasetError("present client export frame cannot carry a failure")
    elif any(frame.get(key) is not None for key in ("sha256", "size_bytes", "media_type", "width", "height")):
        raise ClientExportDatasetError("missing client export frame must not carry file metadata")
    elif frame["failure"]["is_blank"]:
        raise ClientExportDatasetError("missing client export frame requires a failure explanation")


def _validate_structure(value: dict[str, Any]) -> None:
    bindings = _exact(value.get("source_bindings"), {
        "report_generation_id", "report_schema_version", "report_manifest_sha256",
        "report_source_receipts", "readiness_digest", "shots_digest",
    }, "source bindings")
    _identifier(bindings["report_generation_id"], "report generation id")
    _finite(bindings["report_schema_version"], "report schema version", integer=True)
    _sha(bindings["report_manifest_sha256"], "report manifest digest")
    if type(bindings["report_source_receipts"]) is not dict:
        raise ClientExportDatasetError("report source receipts must be an object")
    for key in ("readiness_digest", "shots_digest"):
        if bindings[key] is not None:
            _sha(bindings[key], key)

    project = _exact(value.get("project"), {
        "project_id", "title", "analysis_profile", "delivery_language", "duration_seconds",
        "frame_rate", "resolution", "aspect_ratio",
    }, "project")
    if type(project["project_id"]) is not str or re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", project["project_id"]) is None:
        raise ClientExportDatasetError("project id is invalid")
    _text_cell(project["title"], "project title")
    if project["analysis_profile"] not in {"research", "ads", "streaming", "shortform", "festival"}:
        raise ClientExportDatasetError("project analysis profile is invalid")
    if project["delivery_language"] not in {"zh", "en"}:
        raise ClientExportDatasetError("project delivery language is invalid")
    for key in ("duration_seconds", "frame_rate", "aspect_ratio"):
        _finite(project[key], f"project {key}", minimum=0.0)
    if type(project["resolution"]) is not str or re.fullmatch(r"[1-9][0-9]{0,5}x[1-9][0-9]{0,5}", project["resolution"]) is None:
        raise ClientExportDatasetError("project resolution is invalid")

    delivery = _exact(value.get("delivery_status"), {
        "state", "readiness_status", "professional_export_allowed", "readiness_reference", "reasons",
    }, "delivery status")
    if delivery["state"] not in {"professional", "draft_only"} or type(delivery["professional_export_allowed"]) is not bool:
        raise ClientExportDatasetError("delivery state is invalid")
    if (delivery["state"] == "professional") != delivery["professional_export_allowed"]:
        raise ClientExportDatasetError("delivery state conflicts with professional-export permission")
    if type(delivery["readiness_status"]) is not str or delivery["readiness_reference"] != "data/readiness.json":
        raise ClientExportDatasetError("delivery readiness binding is invalid")
    _text_cell_list(delivery["reasons"], "delivery reasons")

    semantics = _exact(value.get("field_semantics"), {
        "text_cell", "evidence", "interpretation", "untrusted_data_rule", "time_range_semantics",
    }, "field semantics")
    if semantics["time_range_semantics"] != "[start,end)" or not all(type(item) is str for item in semantics.values()):
        raise ClientExportDatasetError("field semantics are invalid")

    shots = value["shots"]
    shot_ids = [shot.get("shot_id") for shot in shots]
    if any(not _is_identifier(item) for item in shot_ids):
        raise ClientExportDatasetError("shot id is invalid")
    scenes = value.get("scenes")
    if type(scenes) is not list:
        raise ClientExportDatasetError("scenes must be a list")
    scene_ids: set[str] = set()
    scene_membership: dict[str, list[str]] = {shot_id: [] for shot_id in shot_ids}
    for scene in scenes:
        record = _exact(scene, {
            "scene_id", "start_seconds", "end_seconds", "shot_ids", "function", "pace", "confidence",
        }, "scene")
        _identifier(record["scene_id"], "scene id")
        if (
            record["scene_id"] in scene_ids
            or type(record["shot_ids"]) is not list
            or len(record["shot_ids"]) != len(set(record["shot_ids"]))
            or any(item not in shot_ids for item in record["shot_ids"])
        ):
            raise ClientExportDatasetError("scene membership is invalid")
        scene_ids.add(record["scene_id"])
        for shot_id in record["shot_ids"]:
            scene_membership[shot_id].append(record["scene_id"])
        _range(record["start_seconds"], record["end_seconds"], "scene")
        _text_cell(record["function"], "scene function")
        _text_cell(record["pace"], "scene pace")
        _finite(record["confidence"], "scene confidence", minimum=0.0, maximum=1.0)

    audio = _exact(value.get("audio"), {
        "available", "source_binding", "capabilities", "sources", "events", "event_index",
    }, "audio")
    if type(audio["available"]) is not bool:
        raise ClientExportDatasetError("audio availability must be boolean")
    if audio["source_binding"] is not None:
        binding = _exact(audio["source_binding"], {
            "schema_version", "generation_id", "receipt_sha256", "dataset_sha256",
        }, "audio source binding")
        _finite(binding["schema_version"], "audio schema version", integer=True)
        for key in ("generation_id", "receipt_sha256", "dataset_sha256"):
            _sha(binding[key], f"audio {key}")
    capabilities = audio["capabilities"]
    if type(capabilities) is not dict or set(capabilities) != {"baseline_features", "asr", "diarization", "separation", "classification"}:
        raise ClientExportDatasetError("audio capabilities are incomplete")
    for name, capability in capabilities.items():
        item = _exact(capability, {"status", "source_id", "reason"}, f"audio capability {name}")
        if item["status"] not in {"produced", "unknown", "failed", "skipped"}:
            raise ClientExportDatasetError("audio capability status is invalid")
        if item["source_id"] is not None:
            _identifier(item["source_id"], "audio capability source id")
        _text_cell(item["reason"], "audio capability reason")
    sources = audio["sources"]
    if type(sources) is not list:
        raise ClientExportDatasetError("audio sources must be a list")
    source_ids: set[str] = set()
    for source in sources:
        item = _exact(source, {"source_id", "capability", "source_type", "status", "adapter", "engine", "model"}, "audio source")
        _identifier(item["source_id"], "audio source id")
        if item["source_id"] in source_ids:
            raise ClientExportDatasetError("audio source ids are duplicated")
        source_ids.add(item["source_id"])
        if item["capability"] not in capabilities or type(item["source_type"]) is not str or type(item["status"]) is not str:
            raise ClientExportDatasetError("audio source metadata is invalid")
        for key in ("adapter", "engine", "model"):
            _text_cell(item[key], f"audio source {key}")
    events = audio["events"]
    event_by_id: dict[str, dict[str, Any]] = {}
    previous_event: tuple[float, float, str] | None = None
    for event in events:
        item = _exact(event, {
            "event_id", "kind", "source_id", "start_seconds", "end_seconds", "identity_status",
            "requires_review", "proposal_sha256", "original_proposal", "effective_proposal", "review",
            "evidence_reference",
        }, "audio event")
        _identifier(item["event_id"], "audio event id")
        if item["event_id"] in event_by_id or item["kind"] not in EVENT_KINDS or item["source_id"] not in source_ids:
            raise ClientExportDatasetError("audio event identity/source is invalid")
        _range(item["start_seconds"], item["end_seconds"], "audio event")
        order = (item["start_seconds"], item["end_seconds"], item["event_id"])
        if previous_event is not None and order < previous_event:
            raise ClientExportDatasetError("audio events are not deterministically ordered")
        previous_event = order
        if type(item["identity_status"]) is not str or type(item["requires_review"]) is not bool:
            raise ClientExportDatasetError("audio event status is invalid")
        _sha(item["proposal_sha256"], "audio proposal digest")
        _validate_proposal_record(item["original_proposal"], "original proposal")
        if item["effective_proposal"] is not None:
            _validate_proposal_record(item["effective_proposal"], "effective proposal")
        if item["review"] is not None:
            review = _exact(item["review"], {"status", "verification", "review_notes"}, "audio review")
            if review["status"] not in {"reviewed", "rejected", "needs_work"} or review["verification"] not in {"human_draft", "human_reviewed"}:
                raise ClientExportDatasetError("audio review state is invalid")
            _text_cell(review["review_notes"], "audio review notes")
        if not _safe_reference(item["evidence_reference"]):
            raise ClientExportDatasetError("audio evidence reference must be project-relative")
        event_by_id[item["event_id"]] = item
    index = audio["event_index"]
    if type(index) is not dict or set(index) != set(EVENT_KINDS):
        raise ClientExportDatasetError("audio event index is invalid")
    for kind in EVENT_KINDS:
        expected = [event_id for event_id, event in event_by_id.items() if event["kind"] == kind]
        if index[kind] != expected:
            raise ClientExportDatasetError(f"audio event index does not match {kind} events")

    seen_shot_order: tuple[int, float, str] | None = None
    for shot in shots:
        item = _exact(shot, {
            "shot_id", "shot_no", "scene_ids", "start_seconds", "end_seconds", "duration_seconds",
            "timecode", "frame", "text", "camera", "audio", "verification", "evidence_reference",
        }, "shot")
        order = (item["shot_no"], item["start_seconds"], item["shot_id"])
        if seen_shot_order is not None and order < seen_shot_order:
            raise ClientExportDatasetError("shots are not deterministically ordered")
        seen_shot_order = order
        _finite(item["shot_no"], "shot number", integer=True, minimum=1)
        _range(item["start_seconds"], item["end_seconds"], "shot")
        _finite(item["duration_seconds"], "shot duration", minimum=0.0)
        if abs(item["duration_seconds"] - (item["end_seconds"] - item["start_seconds"])) > 0.001:
            raise ClientExportDatasetError("shot duration does not match its range")
        if (
            type(item["scene_ids"]) is not list
            or item["scene_ids"] != scene_membership[item["shot_id"]]
        ):
            raise ClientExportDatasetError("shot scene references are invalid")
        _text_cell(item["timecode"], "shot timecode")
        _validate_frame(_exact(item["frame"], {"path", "present", "sha256", "size_bytes", "media_type", "width", "height", "failure"}, "frame"))
        text = _exact(item["text"], SHOT_TEXT_KEYS, "shot text")
        for name, cell in text.items():
            _text_cell(cell, f"shot text {name}")
        camera = _exact(item["camera"], {"shot_scale", "angle", "motion", "composition"}, "camera")
        for name, cell in camera.items():
            _text_cell(cell, f"camera {name}")
        shot_audio = _exact(item["audio"], {"event_links", "event_coverage_seconds", "summary", "source_reference"}, "shot audio")
        if type(shot_audio["event_links"]) is not list or type(shot_audio["event_coverage_seconds"]) is not dict:
            raise ClientExportDatasetError("shot audio collections are invalid")
        for link in shot_audio["event_links"]:
            record = _exact(link, {
                "event_id", "kind", "overlap_start", "overlap_end", "overlap_seconds", "event_fraction",
                "range_fraction", "continues_from_previous", "continues_to_next",
            }, "shot audio link")
            if record["event_id"] not in event_by_id or record["kind"] != event_by_id[record["event_id"]]["kind"]:
                raise ClientExportDatasetError("shot audio link event/kind is invalid")
            _range(record["overlap_start"], record["overlap_end"], "shot audio overlap")
            for key in ("overlap_seconds", "event_fraction", "range_fraction"):
                _finite(record[key], f"shot audio {key}", minimum=0.0)
            if type(record["continues_from_previous"]) is not bool or type(record["continues_to_next"]) is not bool:
                raise ClientExportDatasetError("shot audio continuation flags are invalid")
        if set(shot_audio["event_coverage_seconds"]) != set(EVENT_KINDS):
            raise ClientExportDatasetError("shot audio coverage kinds are incomplete")
        for amount in shot_audio["event_coverage_seconds"].values():
            _finite(amount, "shot audio coverage", minimum=0.0)
        _text_cell(shot_audio["summary"], "shot audio summary")
        if shot_audio["source_reference"] != "data/client_export_dataset.json#audio.events":
            raise ClientExportDatasetError("shot audio source reference is invalid")
        verification = _exact(item["verification"], {
            "annotation_source", "annotation_verification", "visual_confidence", "readiness_status", "readiness_reasons",
        }, "shot verification")
        for key in ("annotation_source", "annotation_verification", "readiness_status"):
            if type(verification[key]) is not str:
                raise ClientExportDatasetError("shot verification status is invalid")
        _finite(verification["visual_confidence"], "visual confidence", minimum=0.0, maximum=1.0)
        _text_cell_list(verification["readiness_reasons"], "shot readiness reasons")
        if item["evidence_reference"] != f"data/shots.json#shot_id={item['shot_id']}":
            raise ClientExportDatasetError("shot evidence reference is invalid")

    _text_cell_list(value.get("limitations"), "limitations")
    unresolved = value.get("unresolved_items")
    if type(unresolved) is not list:
        raise ClientExportDatasetError("unresolved items must be a list")
    for entry in unresolved:
        item = _exact(entry, {"scope", "shot_id", "reason", "evidence_reference"}, "unresolved item")
        if not _is_identifier(item["scope"]) or (item["shot_id"] is not None and item["shot_id"] not in shot_ids):
            raise ClientExportDatasetError("unresolved item scope is invalid")
        _text_cell(item["reason"], "unresolved reason")
        if item["evidence_reference"] is not None and not _safe_reference(item["evidence_reference"]):
            raise ClientExportDatasetError("unresolved evidence reference must be project-relative")


def _validate_proposal_record(value: Any, label: str) -> None:
    item = _exact(value, {
        "label", "text", "language", "speaker_id", "voice_role", "energy", "onset_density",
        "estimated_bpm", "confidence", "verification",
    }, label)
    _text_cell(item["label"], f"{label} label")
    _text_cell(item["text"], f"{label} text")
    _text_cell(item["language"], f"{label} language")
    if item["speaker_id"] is not None and not _is_identifier(item["speaker_id"]):
        raise ClientExportDatasetError(f"{label} language/speaker is invalid")
    if item["voice_role"] not in {"voice_over", "dialogue", "singing", "unknown"}:
        raise ClientExportDatasetError(f"{label} voice role is invalid")
    for key in ("energy", "onset_density", "estimated_bpm"):
        if item[key] is not None:
            _finite(item[key], f"{label} {key}", minimum=0.0)
    _finite(item["confidence"], f"{label} confidence", minimum=0.0, maximum=1.0)
    if item["verification"] not in {"measured", "machine_estimated", "model_interpreted", "human_draft", "human_reviewed"}:
        raise ClientExportDatasetError(f"{label} verification is invalid")


def _exact(value: Any, keys: set[str] | frozenset[str], label: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != set(keys):
        raise ClientExportDatasetError(f"{label} fields are invalid")
    return value


def _text_cell(value: Any, label: str) -> None:
    if type(value) is not dict or set(value) != TEXT_CELL_KEYS or _client_text(value.get("text"), label) != value:
        raise ClientExportDatasetError(f"{label} text cell is invalid")


def _text_cell_list(value: Any, label: str) -> None:
    if type(value) is not list:
        raise ClientExportDatasetError(f"{label} must be a list")
    for index, item in enumerate(value):
        _text_cell(item, f"{label}[{index}]")


def _identifier(value: Any, label: str) -> None:
    if not _is_identifier(value):
        raise ClientExportDatasetError(f"{label} is invalid")


def _is_identifier(value: Any) -> bool:
    return type(value) is str and IDENTIFIER_PATTERN.fullmatch(value) is not None


def _sha(value: Any, label: str) -> None:
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ClientExportDatasetError(f"{label} must be a SHA-256 digest")


def _finite(value: Any, label: str, *, minimum: float | None = None, maximum: float | None = None, integer: bool = False) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ClientExportDatasetError(f"{label} must be finite")
    if integer and type(value) is not int:
        raise ClientExportDatasetError(f"{label} must be an integer")
    if minimum is not None and value < minimum or maximum is not None and value > maximum:
        raise ClientExportDatasetError(f"{label} is outside its allowed range")


def _range(start: Any, end: Any, label: str) -> None:
    _finite(start, f"{label} start", minimum=0.0)
    _finite(end, f"{label} end", minimum=0.0)
    if start >= end:
        raise ClientExportDatasetError(f"{label} range is invalid")


def _capabilities(value: Any) -> dict[str, Any]:
    if type(value) is not dict:
        return {}
    result = {}
    for name, item in sorted(value.items()):
        if type(item) is not dict:
            raise ClientExportDatasetError("audio capability is invalid")
        result[str(name)] = {
            "status": str(item.get("status") or "unknown"),
            "source_id": item.get("source_id"),
            "reason": _client_text(str(item.get("reason") or ""), f"audio capability {name} reason"),
        }
    return result


def _sources(value: Any) -> list[dict[str, Any]]:
    if type(value) is not list:
        raise ClientExportDatasetError("audio sources must be a list")
    result = []
    for item in value:
        if type(item) is not dict:
            raise ClientExportDatasetError("audio source is invalid")
        result.append({
            "source_id": item.get("source_id"),
            "capability": item.get("capability"),
            "source_type": item.get("source_type"),
            "status": item.get("status"),
            "adapter": _client_text(str(item.get("adapter") or ""), "audio adapter"),
            "engine": _client_text(str(item.get("engine") or ""), "audio engine"),
            "model": _client_text(str(item.get("model") or ""), "audio model"),
        })
    return result


def _limitations(readiness: dict[str, Any], audio: dict[str, Any]) -> list[dict[str, Any]]:
    messages = ["Audio observations come from the final mix; unavailable source stems are never inferred as fact."]
    messages.extend(str(item) for item in readiness.get("reasons") or [])
    for name, item in sorted((audio.get("capabilities") or {}).items()):
        if type(item) is dict and item.get("status") != "produced":
            messages.append(f"{name}: {item.get('status') or 'unknown'}" + (f" — {item.get('reason')}" if item.get("reason") else ""))
    return [_client_text(message, "client limitation") for message in dict.fromkeys(messages)]


def _unresolved(value: Any) -> list[dict[str, Any]]:
    if type(value) is not list:
        raise ClientExportDatasetError("unresolved items must be a list")
    result = []
    for item in value:
        if type(item) is not dict:
            raise ClientExportDatasetError("unresolved item must be an object")
        reference = str(item.get("evidence_ref") or "")
        if reference and not _safe_reference(reference):
            raise ClientExportDatasetError("unresolved evidence reference must be project-relative")
        result.append({
            "scope": str(item.get("scope") or "project"),
            "shot_id": item.get("shot_id"),
            "reason": _client_text(str(item.get("reason") or "Needs review"), "unresolved reason"),
            "evidence_reference": reference or None,
        })
    return result


def _client_text(value: Any, label: str) -> dict[str, Any]:
    try:
        text = bounded_text(
            value,
            label,
            maximum=MAX_TEXT_BYTES,
            forbid_sensitive_value=True,
        )
    except ValueError as exc:
        raise ClientExportDatasetError(str(exc)) from exc
    if PRIVATE_PATH_PATTERN.search(unicodedata.normalize("NFKC", text)):
        raise ClientExportDatasetError(f"{label} must not contain a private absolute path")
    stripped = text.lstrip()
    neutralized = stripped.startswith(("=", "+", "-", "@", "\t", "\r"))
    if neutralized and len(text.encode("utf-8")) >= MAX_TEXT_BYTES:
        raise ClientExportDatasetError(f"{label} leaves no room for formula neutralization")
    return {
        "text": text,
        "spreadsheet_text": f"'{text}" if neutralized else text,
        "is_blank": text == "",
        "formula_neutralized": neutralized,
    }


def _validate_tree(value: Any, path: str, *, depth: int, count: list[int]) -> None:
    count[0] += 1
    if depth > 16 or count[0] > 1_000_000:
        raise ClientExportDatasetError("client export dataset exceeds structural limits")
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str or len(key) > 128:
                raise ClientExportDatasetError(f"client export key is invalid: {path}")
            _validate_tree(item, f"{path}.{key}", depth=depth + 1, count=count)
    elif type(value) is list:
        for index, item in enumerate(value):
            _validate_tree(item, f"{path}[{index}]", depth=depth + 1, count=count)
    elif type(value) is str:
        try:
            bounded_text(value, path, maximum=MAX_TEXT_BYTES, forbid_sensitive_value=True)
        except ValueError as exc:
            raise ClientExportDatasetError(str(exc)) from exc
        if PRIVATE_PATH_PATTERN.search(unicodedata.normalize("NFKC", value)):
            raise ClientExportDatasetError(f"{path} must not contain a private absolute path")
    elif value is None or type(value) in {bool, int}:
        return
    elif type(value) is float:
        if not math.isfinite(value):
            raise ClientExportDatasetError(f"client export number is non-finite: {path}")
    else:
        raise ClientExportDatasetError(f"client export value type is invalid: {path}")


def _unique_by_id(values: list[Any], key: str, label: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in values:
        if type(item) is not dict or type(item.get(key)) is not str or item[key] in result:
            raise ClientExportDatasetError(f"{label} contain invalid or duplicate ids")
        result[item[key]] = item
    return result


def _read_json_file(paths: ProjectPaths, path: Path, label: str) -> tuple[bytes, Any]:
    raw = read_regular_bytes(path, root=paths.root, max_bytes=MAX_DATASET_BYTES)
    return raw, _json_bytes(raw, label)


def _json_bytes(raw: bytes, label: str) -> Any:
    def reject_constant(value: str) -> None:
        raise ClientExportDatasetError(f"{label} contains non-finite JSON: {value}")

    try:
        return json.loads(raw, parse_constant=reject_constant)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ClientExportDatasetError(f"{label} is not strict UTF-8 JSON") from exc


def _verify_input_snapshot(
    generation: dict[str, Any],
    manifest: dict[str, Any],
    *,
    media_bytes: bytes,
    shots: list[Shot],
    scenes_bytes: bytes,
    visualization_bytes: bytes,
    visual_receipt_bytes: bytes,
    visual_receipt: Any,
) -> None:
    sources = generation.get("source_receipts")
    receipts = generation.get("artifact_digests")
    artifacts = manifest.get("artifacts")
    if type(sources) is not dict or type(receipts) is not dict or type(artifacts) is not dict:
        raise ClientExportDatasetError("report generation input receipts are incomplete")
    readiness = sources.get("readiness")
    visual_source = sources.get("visual_generation")
    if type(readiness) is not dict or type(visual_source) is not dict or type(visual_receipt) is not dict:
        raise ClientExportDatasetError("report readiness/visual source receipts are incomplete")
    media_binding = readiness.get("media_binding")
    media_value = _json_bytes(media_bytes, "captured media package")
    if type(media_binding) is not dict or media_binding.get("media_package_sha256") != canonical_json_digest(media_value):
        raise ClientExportDatasetError("captured media package does not match report generation")
    if readiness.get("shots_digest") != canonical_shots_digest(shots):
        raise ClientExportDatasetError("captured shots do not match report generation")
    if visual_source.get("receipt_sha256") != canonical_json_digest(visual_receipt):
        raise ClientExportDatasetError("captured visual receipt does not match report generation")
    if visual_source.get("generation_id") != visual_receipt.get("generation_id"):
        raise ClientExportDatasetError("captured visual generation id is inconsistent")
    scene_receipt = (visual_receipt.get("artifacts") or {}).get("scenes")
    if (
        type(scene_receipt) is not dict
        or scene_receipt.get("kind") != "file"
        or scene_receipt.get("sha256") != _sha_bytes(scenes_bytes)
        or scene_receipt.get("size_bytes") != len(scenes_bytes)
    ):
        raise ClientExportDatasetError("captured scenes do not match visual generation")
    visualization_receipt = receipts.get("visualization_dataset")
    if (
        type(visualization_receipt) is not dict
        or visualization_receipt.get("kind") != "file"
        or visualization_receipt.get("sha256") != _sha_bytes(visualization_bytes)
        or visualization_receipt.get("size_bytes") != len(visualization_bytes)
        or "visualization_dataset" not in artifacts
    ):
        raise ClientExportDatasetError("captured visualization dataset does not match report generation")


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _source_name(source: str, fallback: str) -> str:
    parsed = urlsplit(source)
    if parsed.scheme and parsed.netloc:
        candidate = PurePosixPath(parsed.path).name
    elif re.match(r"^[A-Za-z]:[/\\]", source):
        candidate = PureWindowsPath(source).name
    else:
        candidate = Path(source).name
    return candidate or fallback


def _delivery_language(media: CanonicalMediaPackage) -> str:
    value = str(media.metadata.get("delivery_language") or "zh").strip().lower()
    return "en" if value.startswith("en") else "zh"


def _safe_relative(value: str) -> bool:
    path = PurePosixPath(value)
    return bool(value) and not path.is_absolute() and ".." not in path.parts and "\\" not in value


def _safe_reference(value: str) -> bool:
    path = value.split("#", 1)[0]
    return _safe_relative(path)


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _canonical_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _pretty_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


__all__ = [
    "SCHEMA_ID",
    "ClientExportDatasetError",
    "build_client_export_dataset",
    "validate_client_export_dataset",
    "write_client_export_dataset",
]
