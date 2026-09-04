from __future__ import annotations

from collections import OrderedDict
import hashlib
import html
import json
import os
import stat
import threading
import uuid
from pathlib import Path
from typing import Any

from .artifacts import (
    ADS_ONLY_REPORT_ARTIFACT_IDS,
    REPORT_ARTIFACT_RELATIVE_PATHS,
    artifact_path,
    load_artifact_registry,
    mark_artifacts_stale,
    record_committed_report_artifacts,
)
from .audio import audio_generation_binding
from .audio_synthesis import apply_audio_associations, audio_source_binding, build_project_audio_associations
from .paths import ProjectPaths
from .readiness import canonical_readiness_payload, evaluate_project_readiness
from .safe_io import advisory_file_lock, atomic_output_path, atomic_write_text
from .schemas import (
    AnalysisReport,
    BeatEvent,
    CanonicalMediaPackage,
    MusicProfile,
    Scene,
    Shot,
    TranscriptSegment,
    dump_json,
    load_json,
)
from .delivery import _delivery_lang, enforce_profile_output_boundary, enforce_project_profile_boundary, write_profile_delivery_package
from .utils import format_clock, run_command
from .visual import visual_generation_binding, write_shots_csv


HEURISTIC_BEAT_PREFIX = "heuristic_unverified:"
REPORT_GENERATION_SCHEMA_VERSION = 4
_UNSPECIFIED_AUDIO_BINDING = object()
REPORT_GENERATION_DIGEST_ALGORITHM = "sha256"
REPORT_GENERATION_TREE_DIGEST_MODE = "sha256-tree-v1"
REPORT_GENERATION_MANIFEST_DIGEST_MODE = "canonical-json-without-report-generation-v1"
REPORT_GENERATION_READINESS_DIGEST_MODE = "canonical-readiness-v1"
MAX_REPORT_ARTIFACT_BYTES = 512 * 1024 * 1024
MAX_AUDIO_PREVIEW_ROWS = 240
ARTIFACT_DIGEST_CACHE_MAX_ENTRIES = 256
_ARTIFACT_DIGEST_CACHE: OrderedDict[tuple[Any, ...], dict[str, Any]] = OrderedDict()
_ARTIFACT_DIGEST_CACHE_LOCK = threading.RLock()


def synthesize(paths: ProjectPaths) -> AnalysisReport:
    with advisory_file_lock(paths.data / ".shots.lock", root=paths.root):
        media = CanonicalMediaPackage.model_validate(load_json(paths.data / "media_package.json"))
        enforce_project_profile_boundary(paths, media)
        shots = [Shot.model_validate(item) for item in load_json(paths.data / "shots.json")]
        scenes = [Scene.model_validate(item) for item in load_json(paths.data / "scenes.json")]
        transcript = _load_list(paths.data / "transcript.json", TranscriptSegment)
        beats = _load_list(paths.data / "beats.json", BeatEvent)
        music = _load_list(paths.data / "music_profile.json", MusicProfile)
        _normalize_shots(media, shots)
        enforce_profile_output_boundary(media, shots)
        _attach_audio_to_shots(shots, transcript, beats, music)
        audio_associations = build_project_audio_associations(paths, media, shots, scenes)
        apply_audio_associations(shots, audio_associations, language=_delivery_lang(media))
        generation_id = str(uuid.uuid4())
        _begin_report_generation(paths, media, generation_id, _shots_lock_held=True)
        dump_json(paths.data / "shots.json", shots)
        write_shots_csv(paths.reports / "shot_breakdown.csv", shots, media.analysis_profile)
        report = build_report(media, shots, scenes, transcript, beats, music, paths, audio_associations=audio_associations)
        delivery_artifacts = write_profile_delivery_package(
            report,
            media,
            shots,
            scenes,
            transcript,
            beats,
            music,
            paths,
            _shots_lock_held=True,
            audio_associations=audio_associations,
        )
        report.artifacts.update(delivery_artifacts)
        render_html_report(report, media, shots, scenes, transcript, beats, music, paths.reports / "report.html", audio_associations=audio_associations)
        if not render_pdf_report(paths.reports / "report.html", paths.reports / "overview.pdf"):
            report.artifacts.pop("overview_pdf", None)
        dump_json(paths.data / "analysis_report.json", report)
        _commit_report_generation(paths, media, generation_id, report.artifacts, _shots_lock_held=True,
                                  expected_audio_intelligence=audio_associations["source_binding"])
        return report


def _begin_report_generation(
    paths: ProjectPaths,
    media: CanonicalMediaPackage,
    generation_id: str,
    *,
    _shots_lock_held: bool = False,
) -> None:
    """Invalidate the previous publication before changing any report bytes.

    Cross-file replacement cannot be atomic on a normal filesystem.  This
    marker is therefore the publication transaction boundary: readers must
    accept only a ``reported`` manifest whose committed generation receipts
    validate.  An interrupted run remains ``publishing`` and cannot make an
    older manifest describe a mixture of old and new files.
    """
    if _shots_lock_held:
        _begin_report_generation_locked(paths, media, generation_id)
        return
    with advisory_file_lock(paths.data / ".shots.lock", root=paths.root):
        _begin_report_generation_locked(paths, media, generation_id)


def _begin_report_generation_locked(
    paths: ProjectPaths,
    media: CanonicalMediaPackage,
    generation_id: str,
) -> None:
    _require_current_media_snapshot(paths, media)
    source_receipts = _source_generation_bindings(paths)
    # Validate the optional registry before invalidating the authoritative
    # manifest. Missing legacy registries are accepted; malformed registries
    # fail closed without changing the previous publication.
    load_artifact_registry(paths)
    payload = _base_manifest_payload(paths, media, "publishing", {})
    payload["report_generation"] = {
        "schema_version": REPORT_GENERATION_SCHEMA_VERSION,
        "generation_id": generation_id,
        "run_id": generation_id,
        "state": "publishing",
        "digest_algorithm": REPORT_GENERATION_DIGEST_ALGORITHM,
        "source_receipts": source_receipts,
        "artifact_digests": {},
    }
    dump_json(paths.manifest, payload)
    # Cross-file publication cannot be atomic. If this secondary write fails,
    # the manifest remains publishing and readers still fail closed.
    mark_artifacts_stale(
        paths,
        scopes={"report", "client_export"},
        reason="report_generation_started",
    )


def _commit_report_generation(
    paths: ProjectPaths,
    media: CanonicalMediaPackage,
    generation_id: str,
    artifacts: dict[str, str],
    *,
    _shots_lock_held: bool = False,
    expected_audio_intelligence: Any = _UNSPECIFIED_AUDIO_BINDING,
) -> dict[str, Any]:
    """Publish one digest-bound report generation with the manifest last."""
    if _shots_lock_held:
        return _commit_report_generation_locked(paths, media, generation_id, artifacts, expected_audio_intelligence=expected_audio_intelligence)
    with advisory_file_lock(paths.data / ".shots.lock", root=paths.root):
        return _commit_report_generation_locked(paths, media, generation_id, artifacts, expected_audio_intelligence=expected_audio_intelligence)


def _commit_report_generation_locked(
    paths: ProjectPaths,
    media: CanonicalMediaPackage,
    generation_id: str,
    artifacts: dict[str, str],
    *,
    expected_audio_intelligence: Any = _UNSPECIFIED_AUDIO_BINDING,
) -> dict[str, Any]:
    _require_current_media_snapshot(paths, media)
    source_receipts = _source_generation_bindings(paths)
    if expected_audio_intelligence is not _UNSPECIFIED_AUDIO_BINDING and source_receipts["audio_intelligence"] != expected_audio_intelligence:
        raise ValueError("audio intelligence changed during report generation")
    base = _base_manifest_payload(paths, media, "reported", artifacts)
    contract_reasons = _report_artifact_contract_reasons(
        paths,
        artifacts,
        profile=media.analysis_profile.value,
    )
    if contract_reasons:
        raise ValueError("Invalid report artifact contract: " + "; ".join(contract_reasons))
    receipts = _artifact_receipts(paths, base, artifacts)
    payload = dict(base)
    payload["report_generation"] = {
        "schema_version": REPORT_GENERATION_SCHEMA_VERSION,
        "generation_id": generation_id,
        "run_id": generation_id,
        "state": "committed",
        "digest_algorithm": REPORT_GENERATION_DIGEST_ALGORITHM,
        "source_receipts": source_receipts,
        "artifact_digests": receipts,
    }
    # The manifest is the commit marker and is therefore written last. A
    # registry-only intermediate cannot authorize report downloads.
    record_committed_report_artifacts(paths, payload)
    dump_json(paths.manifest, payload)
    return payload


def verify_report_generation_manifest(
    paths: ProjectPaths,
    *,
    _shots_lock_held: bool = False,
) -> tuple[bool, list[str]]:
    """Verify that the current manifest and every published artifact agree.

    The project manifest is the commit record, so its self-receipt hashes the
    canonical manifest payload with ``report_generation`` omitted.  File
    artifacts hash raw bytes.  Directory artifacts use a deterministic tree
    digest over relative filenames, sizes, and file SHA-256 values.
    """
    if _shots_lock_held:
        return _verify_report_generation_manifest_locked(paths)
    with advisory_file_lock(paths.data / ".shots.lock", root=paths.root):
        return _verify_report_generation_manifest_locked(paths)


def _verify_report_generation_manifest_locked(paths: ProjectPaths) -> tuple[bool, list[str]]:
    try:
        payload = load_json(paths.manifest)
    except Exception as exc:
        return False, [f"project manifest is unreadable: {exc}"]
    if type(payload) is not dict:
        return False, ["project manifest must be an object"]
    generation = payload.get("report_generation")
    if type(generation) is not dict:
        return False, ["report generation receipt is missing"]
    reasons: list[str] = []
    if payload.get("status") != "reported" or generation.get("state") != "committed":
        reasons.append("report generation is not committed")
    version = generation.get("schema_version")
    if type(version) is not int or version not in {3, REPORT_GENERATION_SCHEMA_VERSION}:
        reasons.append("report generation schema version is unsupported")
    generation_id = generation.get("generation_id")
    if type(generation_id) is not str or generation.get("run_id") != generation_id:
        reasons.append("generation_id and run_id are missing or inconsistent")
    else:
        try:
            uuid.UUID(generation_id)
        except ValueError:
            reasons.append("generation_id is not a UUID")
    if generation.get("digest_algorithm") != REPORT_GENERATION_DIGEST_ALGORITHM:
        reasons.append("report generation digest algorithm is unsupported")
    stored_source_receipts = generation.get("source_receipts")
    source_keys = {
        "audio_generation",
        "readiness",
        "visual_generation",
    }
    if version == 4:
        source_keys.add("audio_intelligence")
    if type(stored_source_receipts) is not dict or set(stored_source_receipts) != source_keys:
        reasons.append("report source generation receipts are missing or invalid")
    else:
        try:
            current_source_receipts = _source_generation_bindings(paths, version=version)
        except ValueError as exc:
            reasons.append(f"report source generation verification failed: {exc}")
        else:
            if stored_source_receipts != current_source_receipts:
                reasons.append("report source generation receipts are stale or forged")

    artifacts = payload.get("artifacts")
    receipts = generation.get("artifact_digests")
    if type(artifacts) is not dict or not all(type(key) is str and type(value) is str for key, value in artifacts.items()):
        reasons.append("manifest artifacts must be a string map")
        return False, reasons
    contract_reasons = _report_artifact_contract_reasons(
        paths,
        artifacts,
        profile=str(payload.get("profile") or ""),
    )
    reasons.extend(contract_reasons)
    if contract_reasons:
        return False, reasons
    manifest_artifact = artifacts.get("project_manifest")
    try:
        manifest_bound = (
            type(manifest_artifact) is str
            and _absolute_artifact_path(paths, manifest_artifact)
            == _absolute_artifact_path(paths, str(paths.manifest))
        )
    except (OSError, ValueError):
        manifest_bound = False
    if not manifest_bound:
        reasons.append("project manifest self-receipt is missing or non-canonical")
    if type(receipts) is not dict:
        reasons.append("artifact digest receipts are missing")
        return False, reasons
    if set(receipts) != set(artifacts):
        reasons.append("artifact digest receipt ids do not exactly match manifest artifacts")
        return False, reasons

    base = dict(payload)
    base.pop("report_generation", None)
    try:
        expected = _artifact_receipts(paths, base, artifacts)
    except Exception as exc:
        reasons.append(f"artifact receipt verification failed: {exc}")
        return False, reasons
    for artifact_id in sorted(artifacts):
        if receipts.get(artifact_id) != expected.get(artifact_id):
            reasons.append(f"artifact digest mismatch: {artifact_id}")
    return not reasons, reasons


def _report_artifact_contract_reasons(
    paths: ProjectPaths,
    artifacts: dict[str, str],
    *,
    profile: str,
) -> list[str]:
    """Reject aliases and unbounded paths before reading artifact content."""
    reasons: list[str] = []
    normalized_profile = profile.strip().lower()
    for artifact_id, value in sorted(artifacts.items()):
        relative = REPORT_ARTIFACT_RELATIVE_PATHS.get(artifact_id)
        if relative is None:
            reasons.append(f"unsupported report artifact id: {artifact_id}")
            continue
        if artifact_id in ADS_ONLY_REPORT_ARTIFACT_IDS and normalized_profile != "ads":
            reasons.append(f"ads-only artifact is not allowed for profile {normalized_profile or 'unknown'}: {artifact_id}")
            continue
        try:
            actual = _absolute_artifact_path(paths, value)
            expected = _absolute_artifact_path(paths, str(paths.root / relative))
        except (OSError, ValueError):
            reasons.append(f"report artifact path is unsafe: {artifact_id}")
            continue
        if actual != expected:
            reasons.append(f"report artifact path is non-canonical: {artifact_id}")
    return reasons


def _base_manifest_payload(
    paths: ProjectPaths,
    media: CanonicalMediaPackage,
    status: str,
    artifacts: dict[str, str],
) -> dict[str, Any]:
    return {
        "project_id": media.project_id,
        "profile": media.analysis_profile.value,
        "root_path": str(paths.root),
        "source": media.source,
        "status": status,
        "artifacts": dict(artifacts),
    }


def _require_current_media_snapshot(paths: ProjectPaths, media: CanonicalMediaPackage) -> None:
    try:
        stored = CanonicalMediaPackage.model_validate(load_json(paths.data / "media_package.json"))
    except Exception as exc:
        raise ValueError(f"Current media package is missing or invalid: {type(exc).__name__}") from None
    if stored.model_dump(mode="json") != media.model_dump(mode="json"):
        raise RuntimeError("media_package.json changed before report publication; reload the project and retry")


def _readiness_generation_binding(paths: ProjectPaths) -> dict[str, Any]:
    current = evaluate_project_readiness(
        paths.root,
        workspace_root=paths.root.parent,
        require_persisted_receipt=False,
        _shots_lock_held=True,
    )
    payload = canonical_readiness_payload(current)
    canonical = _canonical_json_bytes(payload)
    shots_digest = current.get("shots_digest")
    media_binding = current.get("media_binding")
    vision_binding = current.get("vision_receipt_binding")
    if type(shots_digest) is not str or type(media_binding) is not dict:
        raise ValueError("current readiness source binding is incomplete")
    if vision_binding is not None and type(vision_binding) is not dict:
        raise ValueError("current vision source binding is invalid")
    return {
        "schema_version": current.get("schema_version"),
        "binding_version": current.get("binding_version"),
        "digest_mode": REPORT_GENERATION_READINESS_DIGEST_MODE,
        "sha256": hashlib.sha256(canonical).hexdigest(),
        "size_bytes": len(canonical),
        "shots_digest": shots_digest,
        "media_binding": media_binding,
        # Absence is deliberately explicit: adding a provider receipt after
        # publication changes this binding and invalidates stale reports.
        "vision_receipt_binding": vision_binding,
    }


def _source_generation_bindings(paths: ProjectPaths, *, version: int = REPORT_GENERATION_SCHEMA_VERSION) -> dict[str, Any]:
    try:
        visual = visual_generation_binding(
            paths,
            file_receipt_reader=_cached_artifact_file_receipt,
        )
    except ValueError as exc:
        raise ValueError(f"visual generation is invalid: {exc}") from None
    try:
        audio = audio_generation_binding(
            paths,
            file_receipt_reader=_cached_artifact_file_receipt,
        )
    except ValueError as exc:
        raise ValueError(f"audio generation is invalid: {exc}") from None
    result = {
        "visual_generation": visual,
        "audio_generation": audio,
        "readiness": _readiness_generation_binding(paths),
    }
    audio_intelligence = audio_source_binding(paths, _shots_lock_held=True)
    if version == 3:
        if audio_intelligence is not None:
            raise ValueError("legacy report must be finalized again to bind audio intelligence")
    else:
        result["audio_intelligence"] = audio_intelligence
    return result


def _artifact_receipts(
    paths: ProjectPaths,
    base_manifest: dict[str, Any],
    artifacts: dict[str, str],
) -> dict[str, dict[str, Any]]:
    receipts: dict[str, dict[str, Any]] = {}
    manifest_path = _absolute_artifact_path(paths, str(paths.manifest))
    manifest_digest = hashlib.sha256(_canonical_json_bytes(base_manifest)).hexdigest()
    manifest_size = len(_canonical_json_bytes(base_manifest))
    for artifact_id, value in sorted(artifacts.items()):
        path = _absolute_artifact_path(paths, value)
        if path == manifest_path:
            receipts[artifact_id] = {
                "path": value,
                "kind": "manifest",
                "digest_mode": REPORT_GENERATION_MANIFEST_DIGEST_MODE,
                "sha256": manifest_digest,
                "size_bytes": manifest_size,
            }
            continue
        try:
            info = path.lstat()
        except FileNotFoundError:
            raise ValueError(f"Published artifact does not exist: {artifact_id}") from None
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f"Published artifact must not be a symlink: {artifact_id}")
        if stat.S_ISREG(info.st_mode):
            current = _cached_artifact_file_receipt(path, MAX_REPORT_ARTIFACT_BYTES)
            receipts[artifact_id] = {
                "path": value,
                "kind": "file",
                "digest_mode": "sha256-file-v1",
                "sha256": current["sha256"],
                "size_bytes": current["size_bytes"],
            }
            continue
        if stat.S_ISDIR(info.st_mode):
            digest, size, count = _directory_tree_digest(paths, path)
            receipts[artifact_id] = {
                "path": value,
                "kind": "directory",
                "digest_mode": REPORT_GENERATION_TREE_DIGEST_MODE,
                "sha256": digest,
                "size_bytes": size,
                "file_count": count,
            }
            continue
        raise ValueError(f"Published artifact must be a regular file or directory: {artifact_id}")
    return receipts


def _absolute_artifact_path(paths: ProjectPaths, value: str) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = paths.root / candidate
    absolute = _normalize_system_prefix(candidate)
    root = _normalize_system_prefix(paths.root)
    try:
        relative = absolute.relative_to(root)
    except ValueError:
        raise ValueError(f"Published artifact escapes the project root: {value}") from None
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            break
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"Published artifact has an unsafe parent: {value}")
    return absolute


def _normalize_system_prefix(path: Path) -> Path:
    """Resolve only an OS alias such as macOS ``/var`` -> ``/private/var``.

    Resolving the whole artifact path here would hide a symlink below the
    project boundary.  Normalizing only the first component lets lexical paths
    written before/after ``Path.resolve()`` interoperate without weakening the
    later no-symlink parent checks.
    """
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


def _directory_tree_digest(paths: ProjectPaths, directory: Path) -> tuple[str, int, int]:
    entries: list[tuple[str, str, int]] = []
    for candidate in sorted(directory.rglob("*"), key=lambda item: item.relative_to(directory).as_posix()):
        info = candidate.lstat()
        relative = candidate.relative_to(directory).as_posix()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f"Published artifact directory contains a symlink: {relative}")
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"Published artifact directory contains a special file: {relative}")
        current = _cached_artifact_file_receipt(candidate, MAX_REPORT_ARTIFACT_BYTES)
        entries.append((relative, current["sha256"], current["size_bytes"]))
    digest = hashlib.sha256()
    for relative, file_digest, size in entries:
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_digest.encode("ascii"))
        digest.update(b"\0")
        digest.update(str(size).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest(), sum(size for _relative, _digest, size in entries), len(entries)


def _cached_artifact_file_receipt(path: Path, max_bytes: int) -> dict[str, Any]:
    from .media import _open_regular_no_symlinks

    with _open_regular_no_symlinks(path) as descriptor:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"Published artifact is not a regular file: {path}")
        if before.st_size > max_bytes:
            raise ValueError(f"Published artifact exceeds the {max_bytes}-byte limit: {path}")
        fingerprint = _artifact_stat_fingerprint(before)
        key = ("sha256", *fingerprint)
        cached = _artifact_digest_cache_get(key)
        if cached is not None:
            return cached
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise ValueError(f"Published artifact exceeds the {max_bytes}-byte limit: {path}")
            digest.update(chunk)
        after = os.fstat(descriptor)
        if _artifact_stat_fingerprint(after) != fingerprint:
            raise ValueError(f"Published artifact changed during digest verification: {path}")
    receipt = {"sha256": digest.hexdigest(), "size_bytes": size}
    _artifact_digest_cache_put(key, receipt)
    return dict(receipt)


def _artifact_stat_fingerprint(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _artifact_digest_cache_get(key: tuple[Any, ...]) -> dict[str, Any] | None:
    with _ARTIFACT_DIGEST_CACHE_LOCK:
        value = _ARTIFACT_DIGEST_CACHE.get(key)
        if value is None:
            return None
        _ARTIFACT_DIGEST_CACHE.move_to_end(key)
        return dict(value)


def _artifact_digest_cache_put(key: tuple[Any, ...], value: dict[str, Any]) -> None:
    with _ARTIFACT_DIGEST_CACHE_LOCK:
        _ARTIFACT_DIGEST_CACHE[key] = dict(value)
        _ARTIFACT_DIGEST_CACHE.move_to_end(key)
        while len(_ARTIFACT_DIGEST_CACHE) > ARTIFACT_DIGEST_CACHE_MAX_ENTRIES:
            _ARTIFACT_DIGEST_CACHE.popitem(last=False)


def _clear_artifact_digest_cache() -> None:
    with _ARTIFACT_DIGEST_CACHE_LOCK:
        _ARTIFACT_DIGEST_CACHE.clear()


def _canonical_json_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _load_list(path: Path, cls):
    if not path.exists():
        return []
    return [cls.model_validate(item) for item in load_json(path)]


def _attach_audio_to_shots(
    shots: list[Shot],
    transcript: list[TranscriptSegment],
    beats: list[BeatEvent],
    music: list[MusicProfile],
) -> None:
    for shot in shots:
        speech = [seg.text for seg in transcript if _overlaps(shot.start_time, shot.end_time, seg.start_time, seg.end_time)]
        joined_speech = " ".join(speech)[:220]
        # ``dialogue`` is an editable review field. Once a provider or human
        # owns the shot annotation, even an empty value is an explicit review
        # decision and must survive report finalization. The transcript-derived
        # text remains available separately as ``speech_summary``.
        if (shot.annotation_source or "machine").strip().lower() == "machine":
            shot.dialogue = joined_speech
        shot.speech_summary = joined_speech
        beat_count = sum(1 for beat in beats if shot.start_time <= beat.time < shot.end_time)
        shot.beat_density = round(beat_count / max(shot.duration, 0.1), 3)
        if (shot.annotation_source or "machine").strip().lower() == "machine":
            shot.rhythm_notes = _rhythm_note(shot.beat_density)
            active_music = next((item for item in music if item.confidence > 0 and _overlaps(shot.start_time, shot.end_time, item.start_time, item.end_time)), None)
            shot.music_state = active_music.energy_level if active_music else "unknown"
            shot.sound_design = "music-led (unverified)" if active_music else "review required"
            shot.sound_rhythm = f"{shot.music_state}; {shot.rhythm_notes}; rhythm candidates {beat_count} (not verified musical beats)"
        if not shot.audio_notes:
            shot.audio_notes = "review dialogue, music, and SFX relationship"


def _normalize_shots(media: CanonicalMediaPackage, shots: list[Shot]) -> None:
    frame_count = max(1, len(list((Path(media.review_copy_path).parent / "keyframes").glob("frame-*.jpg"))))
    profile = media.analysis_profile.value
    for index, shot in enumerate(shots, start=1):
        if not shot.scene_no:
            shot.scene_no = f"{((index - 1) // 4) + 1:03d}"
        if not shot.shot_no:
            shot.shot_no = index
        if not shot.setup_id:
            shot.setup_id = chr(65 + ((index - 1) % 26))
        if not shot.timecode:
            shot.timecode = f"{format_clock(shot.start_time)}-{format_clock(shot.end_time)}"
        if not shot.frame_ref:
            shot.frame_ref = f"frame-{min(index, frame_count):04d}.jpg"
        if not shot.primary_frame_ref:
            shot.primary_frame_ref = shot.frame_ref
        if not shot.frame_refs:
            shot.frame_refs = [shot.primary_frame_ref]
        if not shot.boundary_confidence:
            shot.boundary_confidence = "low"
        existing_story_beat = shot.story_beat
        if shot.annotation_source == "machine":
            story_beat = existing_story_beat or _story_beat(index, len(shots), profile)
            if profile != "ads" and _strip_heuristic_marker(story_beat) in {
                "hook",
                "problem",
                "demo",
                "proof",
                "payoff",
                "cta",
            }:
                story_beat = _story_beat(index, len(shots), profile)
            if not story_beat.startswith(HEURISTIC_BEAT_PREFIX):
                story_beat = f"{HEURISTIC_BEAT_PREFIX}{story_beat}"
            shot.story_beat = story_beat
            if not shot.scene_type or shot.scene_type == existing_story_beat:
                shot.scene_type = story_beat
        else:
            if not shot.story_beat:
                shot.story_beat = f"{HEURISTIC_BEAT_PREFIX}{_story_beat(index, len(shots), profile)}"
            if not shot.scene_type:
                shot.scene_type = shot.story_beat
        if shot.equipment == "TBD":
            shot.equipment = "not inferable from final video"
        if shot.lens == "TBD":
            shot.lens = "not inferable from final video"
        if shot.subject == "unknown":
            shot.subject = ""
        if shot.action == "unknown" or shot.action == "review required":
            shot.action = ""
        if not shot.review_notes:
            shot.review_notes = "automatic shot boundary; vision enrichment required for professional export"
        if shot.annotation_source == "machine" and "heuristic interpretations are unverified" not in shot.review_notes:
            shot.review_notes = f"{shot.review_notes}; story beat and scene type heuristic interpretations are unverified"
        if not shot.visual_confidence:
            shot.visual_confidence = (
                shot.confidence
                if shot.annotation_source in {"vision", "openai", "minimax", "minimax_mcp"}
                else 0.0
            )
        if not shot.readiness_status:
            shot.readiness_status = "draft"


def _story_beat(index: int, count: int, profile: str) -> str:
    if profile != "ads":
        if index == 1:
            return "opening_sequence"
        ratio = (index - 1) / max(count - 1, 1)
        if ratio < 0.25:
            return "early_sequence"
        if ratio < 0.65:
            return "middle_sequence"
        if index == count:
            return "closing_sequence"
        return "late_sequence"
    if index == 1:
        return "hook"
    ratio = (index - 1) / max(count - 1, 1)
    if ratio < 0.25:
        return "problem"
    if ratio < 0.55:
        return "demo"
    if ratio < 0.78:
        return "proof"
    if index == count:
        return "cta"
    return "payoff"


def _strip_heuristic_marker(value: str) -> str:
    return value[len(HEURISTIC_BEAT_PREFIX) :] if value.startswith(HEURISTIC_BEAT_PREFIX) else value


def _rhythm_note(beat_density: float) -> str:
    if beat_density >= 0.8:
        return "dense rhythm peaks; check edit/music alignment"
    if beat_density >= 0.25:
        return "moderate rhythm activity"
    return "sparse rhythm activity"


def _overlaps(a_start: float, a_end: float, b_start: float, b_end: float) -> bool:
    return max(a_start, b_start) < min(a_end, b_end)


def build_report(
    media: CanonicalMediaPackage,
    shots: list[Shot],
    scenes: list[Scene],
    transcript: list[TranscriptSegment],
    beats: list[BeatEvent],
    music: list[MusicProfile],
    paths: ProjectPaths,
    *,
    audio_associations: dict[str, Any] | None = None,
) -> AnalysisReport:
    avg_shot = sum(shot.duration for shot in shots) / max(len(shots), 1)
    beat_rate = len(beats) / max(media.duration_seconds, 1.0) * 60
    profile = media.analysis_profile.value
    visual = [
        f"{len(shots)} shots estimated across {len(scenes)} scenes.",
        f"Average estimated shot duration is {avg_shot:.1f}s. Heuristic pacing interpretation (unverified): {_pace_label(avg_shot)}.",
        "Shot labels and narrative functions are interpretations, not source evidence; machine estimates remain unverified.",
    ]
    audio = [
        f"{len(transcript)} transcript segments available; consult ASR capability status before concluding speech is absent.",
        (f"PCM summary: overall audio energy {music[0].energy_level}; pulse tempo bucket {music[0].tempo_bucket}; music/SFX/VO identity unknown."
         if music and music[0].confidence == 0 else
         f"Machine interpretation (unverified): music reads as {music[0].energy_level if music else 'unknown'} energy with {music[0].tempo_bucket if music else 'unknown'} tempo."),
        f"{len(beats)} rhythm peaks detected across the runtime.",
    ]
    if audio_associations is not None:
        audio = _audio_report_observations(audio_associations, zh=False)
    speech_count = (sum(bool(event["effective_proposal"] and event["effective_proposal"]["text"]) for event in audio_associations["events"])
                    if audio_associations and audio_associations["available"] else len(transcript))
    rhythm = [
        f"Estimated rhythm density is {beat_rate:.1f} peaks per minute.",
        _profile_specific_rhythm(profile, avg_shot, beat_rate),
    ]
    takeaways = _takeaways(profile, avg_shot, beat_rate, transcript, has_transcript=speech_count > 0)
    artifacts = {
        artifact_id: str(artifact_path(paths.root, artifact_id))
        for artifact_id in (
            "overview_pdf",
            "report_html",
            "storyboard_html",
            "shot_list_csv",
            "profile_analysis_html",
            "shot_breakdown_csv",
            "shot_table_csv",
            "lineage_json",
            "readiness_json",
            "transcript_srt",
            "music_rhythm_summary",
            "contact_sheet",
            "keyframes",
            "project_manifest",
        )
    }
    boundary_review_path = artifact_path(paths.root, "boundary_review_json")
    if os.path.lexists(boundary_review_path):
        artifacts["boundary_review_json"] = str(boundary_review_path)
    if profile == "ads":
        artifacts.update(
            {
                artifact_id: str(artifact_path(paths.root, artifact_id))
                for artifact_id in ADS_ONLY_REPORT_ARTIFACT_IDS
            }
        )
    return AnalysisReport(
        project_id=media.project_id,
        profile=media.analysis_profile,
        summary=f"{media.project_id} analyzed as a {profile} video with {len(shots)} estimated shots, {speech_count} effective transcript events, and {len(beats)} rhythm peaks.",
        technical={
            "duration_seconds": media.duration_seconds,
            "duration": format_clock(media.duration_seconds),
            "frame_rate": media.frame_rate,
            "resolution": media.resolution,
            "aspect_ratio": media.aspect_ratio,
        },
        visual_observations=visual,
        audio_observations=audio,
        rhythm_observations=rhythm,
        client_takeaways=takeaways,
        artifacts=artifacts,
    )


def _pace_label(avg_shot: float) -> str:
    if avg_shot < 3:
        return "fast"
    if avg_shot < 7:
        return "controlled"
    return "slow"


def _profile_specific_rhythm(profile: str, avg_shot: float, beat_rate: float) -> str:
    if profile == "ads":
        return "Heuristic interpretation (unverified): review opening-hook, beat alignment, and CTA pacing."
    if profile == "shortform":
        return "Heuristic interpretation (unverified): review opening clarity, beat alignment, and closing cadence."
    if profile == "festival":
        return "Heuristic interpretation (unverified): review concept clarity, mood continuity, and audiovisual intent."
    return "Heuristic interpretation (unverified): review scene flow, continuity, and emotional energy."


def _takeaways(profile: str, avg_shot: float, beat_rate: float, transcript: list[TranscriptSegment], *, has_transcript: bool | None = None) -> list[str]:
    takeaways = []
    if profile == "ads":
        takeaways.append("Review the first 3-5 seconds against the strongest visual and audio peaks.")
        takeaways.append("Check whether brand, product, or topic recognition appears before viewer attention drops.")
    elif profile == "shortform":
        takeaways.append("Review the opening 3-5 seconds against the strongest visual and audio peaks.")
        takeaways.append("Check whether the topic is legible before the first major edit transition.")
    else:
        takeaways.append("Review scene grouping against actual narrative or emotional turns.")
        takeaways.append("Check whether recurring motifs are intentional enough to name in a client deck.")
    if not (bool(transcript) if has_transcript is None else has_transcript):
        takeaways.append("No usable transcript was produced; run ASR again or import subtitles before final client delivery.")
    if beat_rate < 20:
        takeaways.append("Rhythm peak density is low; verify whether that is intentional restraint or a pacing issue.")
    elif beat_rate > 120:
        takeaways.append("Rhythm peak density is high; verify that edits and sound hits do not flatten emphasis.")
    return [f"Interpretation/review note (unverified): {item}" for item in takeaways]


def render_html_report(
    report: AnalysisReport,
    media: CanonicalMediaPackage,
    shots: list[Shot],
    scenes: list[Scene],
    transcript: list[TranscriptSegment],
    beats: list[BeatEvent],
    music: list[MusicProfile],
    path: Path,
    *,
    audio_associations: dict[str, Any] | None = None,
) -> None:
    zh = _localized_report(report, media, shots, scenes, transcript, beats, music, audio_associations=audio_associations)
    css = """
    :root { color-scheme: dark; --ink:#f3f3f0; --text:#c9c9c3; --muted:#777771; --line:#252525; --line2:#3b3b3b; --paper:#050505; --panel:#111; --panel2:#171717; --accent:#f2f2ed; --soft:#d7d7d0; }
    * { box-sizing: border-box; }
    html { scroll-behavior:smooth; }
    body { margin:0; font-family:"Helvetica Neue","PingFang SC","Hiragino Sans GB","Noto Sans CJK SC","Microsoft YaHei",Arial,sans-serif; background:linear-gradient(135deg,#050505,#0b0b0b 56%,#151515); color:var(--ink); -webkit-font-smoothing:antialiased; }
    main { max-width:1480px; margin:0 auto; padding:24px 22px 64px; }
    header { display:grid; grid-template-columns: 1fr auto; gap:24px; align-items:end; border:1px solid var(--line); border-radius:4px; padding:22px; background:linear-gradient(180deg,rgba(24,24,24,.88),rgba(10,10,10,.96)); box-shadow:0 28px 80px rgba(0,0,0,.36); }
    h1 { font-size:clamp(42px,7vw,92px); line-height:.88; margin:0; letter-spacing:-.075em; font-weight:850; text-transform:lowercase; }
    h2 { font-size:16px; margin:0 0 14px; letter-spacing:-.01em; }
    p { color:var(--text); line-height:1.55; }
    .meta { color:var(--muted); font-size:13px; display:flex; gap:8px; flex-wrap:wrap; margin-top:14px; }
    .meta span, .badge, .lang button, .nav a { border:1px solid var(--line); background:#080808; padding:7px 9px; border-radius:999px; }
    .badge { color:var(--accent); text-transform:uppercase; letter-spacing:.1em; font-size:11px; }
    .topbar { position:sticky; top:0; z-index:10; display:flex; align-items:center; justify-content:space-between; gap:16px; padding:10px 0 14px; background:linear-gradient(180deg,#050505 0%,rgba(5,5,5,.86) 78%,transparent); }
    .nav { display:flex; gap:8px; flex-wrap:wrap; }
    .nav a { color:var(--muted); text-decoration:none; font:700 11px/1 "Helvetica Neue",Arial,sans-serif; text-transform:uppercase; letter-spacing:.09em; }
    .nav a:hover { color:var(--ink); border-color:var(--line2); }
    .lang { display:flex; gap:8px; justify-content:flex-end; }
    .lang button { cursor:pointer; color:var(--muted); font:700 11px/1 "Helvetica Neue",Arial,sans-serif; text-transform:uppercase; letter-spacing:.08em; }
    .lang button.active { background:var(--soft); color:#050505; border-color:var(--soft); }
    .grid { display:grid; grid-template-columns: repeat(3, 1fr); gap:12px; margin:16px 0; }
    .panel { background:linear-gradient(180deg,rgba(24,24,24,.92),rgba(12,12,12,.96)); border:1px solid var(--line); border-radius:4px; padding:17px; box-shadow:0 20px 60px rgba(0,0,0,.26); }
    .wide { grid-column: span 2; }
    ul { padding-left:18px; margin:0; }
    li { margin:8px 0; color:var(--text); }
    .atlas { margin:16px 0; border:1px solid var(--line); border-radius:4px; overflow:hidden; background:#080808; }
    .atlasTop { min-height:260px; display:grid; grid-template-columns:1.1fr .9fr; gap:18px; padding:22px; align-items:end; background:linear-gradient(135deg,#050505,#111 62%,#1a1a1a); position:relative; }
    .atlasTop:after { content:""; position:absolute; inset:0; background:repeating-linear-gradient(90deg,rgba(255,255,255,.035) 0 1px,transparent 1px 110px); opacity:.5; pointer-events:none; }
    .atlasTop > * { position:relative; z-index:1; }
    .atlasLabel { color:var(--soft); font-size:11px; letter-spacing:.16em; text-transform:uppercase; }
    .atlasTitle { margin:8px 0 0; font-size:clamp(58px,11vw,148px); line-height:.82; letter-spacing:-.09em; text-transform:uppercase; }
    .atlasDeck { color:var(--text); font-size:16px; line-height:1.45; max-width:520px; justify-self:end; }
    .shotIndex { display:grid; }
    .shotItem { display:grid; grid-template-columns:92px 170px minmax(240px,1fr) 150px 150px; gap:14px; align-items:center; min-height:104px; padding:12px 16px; border-top:1px solid var(--line); text-decoration:none; color:var(--ink); transition:background .28s ease, transform .28s ease; animation:rise .6s ease both; }
    .shotItem:hover { background:#181818; transform:translateX(5px); }
    .shotNo { font-size:22px; letter-spacing:-.04em; }
    .shotThumb { width:168px; aspect-ratio:2.39/1; object-fit:cover; border:1px solid var(--line2); filter:saturate(.82) contrast(1.06); transition:transform .35s ease, filter .35s ease; }
    .shotItem:hover .shotThumb { transform:scale(1.035); filter:saturate(1) contrast(1.12); }
    .shotName { font-size:18px; letter-spacing:-.03em; }
    .shotMeta { color:var(--muted); font-size:12px; line-height:1.45; }
    @keyframes rise { from { opacity:0; transform:translateY(12px); } to { opacity:1; transform:translateY(0); } }
    .tablepanel { padding:0; overflow:visible; }
    .tablehead { display:flex; justify-content:space-between; gap:16px; align-items:end; padding:16px 17px; border-bottom:1px solid var(--line); background:#101010; }
    .tablehead p { margin:5px 0 0; color:var(--muted); font-size:12px; }
    .tablewrap { overflow-x:auto; overflow-y:visible; }
    table { min-width:2100px; width:100%; border-collapse:separate; border-spacing:0; font-size:15px; background:#0d0d0d; }
    th, td { border-bottom:1px solid var(--line); border-right:1px solid rgba(37,37,37,.82); text-align:left; padding:14px 12px; vertical-align:top; }
    th { position:sticky; top:58px; z-index:3; color:var(--soft); background:#151515; font-weight:700; text-transform:uppercase; letter-spacing:.06em; font-size:12px; }
    td { color:var(--text); line-height:1.55; }
    tr:hover td { background:#171717; }
    td:first-child, th:first-child { position:sticky; left:0; z-index:2; background:#111; color:var(--ink); }
    th:first-child { z-index:4; background:#151515; }
    .contact { width:100%; border:1px solid var(--line); border-radius:6px; display:block; background:#090805; }
    .thumb { width:240px; aspect-ratio:2.39/1; object-fit:cover; border:1px solid var(--line2); border-radius:3px; display:block; background:#050504; }
    .small { color:var(--muted); font-size:13px; }
    .note { color:var(--muted); font-size:12px; padding:12px 17px; border-top:1px solid var(--line); margin:0; background:#101010; }
    .zh { display:none; }
    body[data-lang="zh"] .en { display:none; }
    body[data-lang="zh"] .zh { display:revert; }
    body[data-lang="en"] .en { display:revert; }
    body[data-lang="en"] .zh { display:none; }
    @media (max-width: 860px) { header, .grid, .atlasTop { grid-template-columns:1fr; } .wide { grid-column:auto; } main { padding:22px 16px 48px; } .atlasDeck{justify-self:start}.shotItem{grid-template-columns:1fr;gap:8px}.shotThumb{width:100%} }
    """
    contact = Path(report.artifacts["contact_sheet"])
    contact_src = f"../assets/{contact.name}"
    include_generation = report.profile.value == "ads"
    rows = "\n".join(
        _storyboard_row(shot, zh=False, include_generation=include_generation)
        for shot in shots[:30]
    )
    rows_zh = "\n".join(
        _storyboard_row(shot, zh=True, include_generation=include_generation)
        for shot in shots[:30]
    )
    generation_header = "<th>Creative Generation Interpretation</th>" if include_generation else ""
    generation_header_zh = "<th>创意生成解释</th>" if include_generation else ""
    interpretation_scope = "Descriptions and scene functions are interpretations whose verification state must be checked."
    interpretation_scope_zh = "内容描述与叙事功能均为解释，必须结合复核状态阅读。"
    if include_generation:
        interpretation_scope = "Descriptions, scene functions, and creative prompts are interpretations whose verification state must be checked."
        interpretation_scope_zh = "内容描述、叙事功能和创意提示词均为解释，必须结合复核状态阅读。"
    atlas_rows = "\n".join(_shot_atlas_item(shot, zh=False, index=index) for index, shot in enumerate(shots[:24], start=1))
    atlas_rows_zh = "\n".join(_shot_atlas_item(shot, zh=True, index=index) for index, shot in enumerate(shots[:24], start=1))
    body = f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(report.project_id)} Report</title><style>{css}</style></head>
<body data-lang="en"><main>
<div class="topbar"><nav class="nav"><a href="/"><span class="en">Index</span><span class="zh">引导页</span></a><a href="/projects/{html.escape(report.project_id)}"><span class="en">Project</span><span class="zh">项目页</span></a><a href="#shot-board"><span class="en">Shot Board</span><span class="zh">分镜表</span></a></nav><div class="lang"><button type="button" data-set-lang="en" class="active">EN</button><button type="button" data-set-lang="zh">中文</button></div></div>
<header><div><h1>{html.escape(report.project_id)}</h1><p class="en">{html.escape(report.summary)}</p><p class="zh">{html.escape(zh["summary"])}</p><div class="meta"><span>{html.escape(report.profile.value)}</span><span>{html.escape(report.technical["duration"])}</span><span>{html.escape(str(report.technical["resolution"]))}</span></div></div><div class="badge"><span class="en">Local Analysis MVP</span><span class="zh">本地分析 MVP</span></div></header>
<section class="grid">
<article class="panel"><h2><span class="en">Visual</span><span class="zh">画面</span></h2><ul class="en">{''.join(f'<li>{html.escape(item)}</li>' for item in report.visual_observations)}</ul><ul class="zh">{''.join(f'<li>{html.escape(item)}</li>' for item in zh["visual"])}</ul></article>
<article class="panel"><h2><span class="en">Audio</span><span class="zh">声音</span></h2><ul class="en">{''.join(f'<li>{html.escape(item)}</li>' for item in report.audio_observations)}</ul><ul class="zh">{''.join(f'<li>{html.escape(item)}</li>' for item in zh["audio"])}</ul></article>
<article class="panel"><h2><span class="en">Rhythm</span><span class="zh">节奏</span></h2><ul class="en">{''.join(f'<li>{html.escape(item)}</li>' for item in report.rhythm_observations)}</ul><ul class="zh">{''.join(f'<li>{html.escape(item)}</li>' for item in zh["rhythm"])}</ul></article>
<article class="panel wide"><h2><span class="en">Interpretations / Review Notes (Unverified)</span><span class="zh">解释与复核备注（未验证）</span></h2><ul class="en">{''.join(f'<li>{html.escape(item)}</li>' for item in report.client_takeaways)}</ul><ul class="zh">{''.join(f'<li>{html.escape(item)}</li>' for item in zh["takeaways"])}</ul></article>
<article class="panel"><h2><span class="en">Contact Sheet</span><span class="zh">画面联系表</span></h2><img class="contact" src="{html.escape(contact_src)}" alt="Contact sheet"></article>
</section>
{_render_audio_associations(audio_associations)}
<section class="atlas en"><div class="atlasTop"><div><div class="atlasLabel">Index ({len(shots)})</div><div class="atlasTitle">Shot<br>Atlas</div></div><p class="atlasDeck">A cinematic visual index generated from the analysis output. Every analyzed shot becomes a navigable frame record with timecode, image, content, scale, movement, and review state.</p></div><div class="shotIndex">{atlas_rows}</div></section>
<section class="atlas zh"><div class="atlasTop"><div><div class="atlasLabel">索引 ({len(shots)})</div><div class="atlasTitle">Shot<br>Atlas</div></div><p class="atlasDeck">由分析结果自动生成的影像索引。每个被解析的镜头都会成为一个画面记录，包含时码、截图、内容、景别、运镜和复核状态。</p></div><div class="shotIndex">{atlas_rows_zh}</div></section>
<section id="shot-board" class="panel tablepanel en"><div class="tablehead"><div><h2>Shot Evidence Board</h2><p>Timecodes and media files are evidence. {interpretation_scope}</p></div><span class="badge">Evidence workbench</span></div><div class="tablewrap"><table><thead><tr><th>Shot</th><th>Panel</th><th>TC / Dur.</th><th>Annotated Content</th><th>Narrative Interpretation</th><th>Shot Size</th><th>Angle</th><th>Movement</th><th>Composition</th><th>Dialogue / Sound</th><th>Music / Rhythm</th>{generation_header}<th>Review</th></tr></thead><tbody>{rows}</tbody></table></div><p class="note">Optional vision annotation can enrich descriptive fields. Run `analyze-video vision PROJECT_ID` with a configured provider, then verify every annotation against the source before marking it reviewed.</p></section>
<section id="shot-board-zh" class="panel tablepanel zh"><div class="tablehead"><div><h2>逐镜头证据表</h2><p>时码与媒体文件是证据；{interpretation_scope_zh}</p></div><span class="badge">视频证据工作台</span></div><div class="tablewrap"><table><thead><tr><th>镜头</th><th>画面</th><th>时码/时长</th><th>标注内容</th><th>叙事解释</th><th>景别</th><th>角度</th><th>运镜</th><th>构图</th><th>对白/声音</th><th>音乐/节奏</th>{generation_header_zh}<th>复核</th></tr></thead><tbody>{rows_zh}</tbody></table></div><p class="note">可选视觉标注可以补充描述字段。配置 provider 后执行 `analyze-video vision PROJECT_ID`，并在标记已复核前逐项对照原片。</p></section>
</main><script>
const buttons = document.querySelectorAll('[data-set-lang]');
function setLang(lang) {{
  document.body.dataset.lang = lang;
  document.documentElement.lang = lang === 'zh' ? 'zh-CN' : 'en';
  buttons.forEach(btn => btn.classList.toggle('active', btn.dataset.setLang === lang));
  localStorage.setItem('video-analysis-report-lang', lang);
}}
buttons.forEach(btn => btn.addEventListener('click', () => setLang(btn.dataset.setLang)));
setLang(localStorage.getItem('video-analysis-report-lang') || 'en');
</script></body></html>"""
    atomic_write_text(path, body)


def _localized_report(
    report: AnalysisReport,
    media: CanonicalMediaPackage,
    shots: list[Shot],
    scenes: list[Scene],
    transcript: list[TranscriptSegment],
    beats: list[BeatEvent],
    music: list[MusicProfile],
    *,
    audio_associations: dict[str, Any] | None = None,
) -> dict[str, list[str] | str]:
    avg_shot = sum(shot.duration for shot in shots) / max(len(shots), 1)
    beat_rate = len(beats) / max(media.duration_seconds, 1.0) * 60
    music_energy = _zh_value(music[0].energy_level if music else "unknown")
    music_tempo = _zh_value(music[0].tempo_bucket if music else "unknown")
    result = {
        "summary": f"{report.project_id} 已按 {report.profile.value} 类型完成分析：估算 {len(shots)} 个镜头、{len(transcript)} 段字幕/对白、{len(beats)} 个节奏峰值。",
        "visual": [
            f"估算 {len(shots)} 个镜头，归并为 {len(scenes)} 个场景段落。",
            f"平均镜头时长 {avg_shot:.1f} 秒；启发式节奏解释（未验证）：{_zh_value(_pace_label(avg_shot))}。",
            "镜头标签与叙事功能属于解释，不是源证据；机器估算均为未验证。",
        ],
        "audio": [
            f"生成 {len(transcript)} 段字幕/对白。",
            f"机器解释（未验证）：音乐轮廓为{music_energy}能量、{music_tempo}速度。",
            f"全片检测到 {len(beats)} 个节奏峰值。",
        ],
        "rhythm": [
            f"估算节奏密度为每分钟 {beat_rate:.1f} 个峰值。",
            _zh_value(_profile_specific_rhythm(report.profile.value, avg_shot, beat_rate)),
        ],
        "takeaways": [_zh_value(item) for item in report.client_takeaways],
    }
    if audio_associations is not None:
        result["audio"] = _audio_report_observations(audio_associations, zh=True)
        if audio_associations["available"]:
            count = sum(bool(event["effective_proposal"] and event["effective_proposal"]["text"]) for event in audio_associations["events"])
            result["summary"] = f"{report.project_id}：估算 {len(shots)} 个镜头，{count} 条有效文本事件，{len(beats)} 个节奏候选。音频语义须按来源与复核状态阅读。"
    return result


def _audio_report_observations(view: dict[str, Any], *, zh: bool) -> list[str]:
    if not view["available"]:
        return (["未生成结构化音频时间线；旧音频字段只供参考，不能据此认定静音。"] if zh else
                ["Structured audio timeline unavailable; legacy fields are reference only, not evidence of silence."])
    states = ", ".join(f"{key}: {value['status']}" for key, value in sorted(view["capabilities"].items()))
    if zh:
        return [f"结构化音频事件 {len(view['events'])} 条，已按原始时间关联镜头与叙事段。",
                "能力状态：" + states,
                "跨镜头文本保留完整事件，不伪造词级时间；估计、人工复核和被拒绝提案分开显示。"]
    return [f"{len(view['events'])} structured audio events linked to shots and narrative ranges.",
            "Capabilities: " + states,
            "Cross-shot text retains full events, not word timings; estimates, human review and rejected proposals stay distinct."]


def _render_audio_associations(view: dict[str, Any] | None) -> str:
    if view is None:
        return ""
    if not view["available"]:
        return '<section id="audio-evidence" class="panel"><h2>音频证据 / Audio evidence</h2><p>结构化时间线不可用 / Structured timeline unavailable. Unknown is not silence.</p></section>'
    event_map = {event["event_id"]: event for event in view["events"]}
    rows = []
    total_links = sum(len(shot["event_links"]) for shot in view["shots"])
    for shot in view["shots"]:
        if len(rows) >= MAX_AUDIO_PREVIEW_ROWS:
            break
        for link in shot["event_links"]:
            if len(rows) >= MAX_AUDIO_PREVIEW_ROWS:
                break
            event = event_map[link["event_id"]]
            effective = event["effective_proposal"]
            review = event["review"]
            state = review["status"] if review else event["proposal"]["verification"]
            content = ((effective["text"] or effective["label"] or "—") if effective is not None
                       else "不使用此提案 / Excluded proposal; original retained in JSON")
            if len(content) > 1200:
                content = content[:1200] + "… [预览；全文见音频事件 JSON / full text in audio event JSON]"
            if effective is not None:
                if effective["energy"] is not None:
                    content += f" · RMS {effective['energy']:.3f}"
                if effective["estimated_bpm"] is not None:
                    content += f" · 脉冲估计 / pulse estimate {effective['estimated_bpm']:.1f} BPM"
            scope = "完整事件文本，非词级对齐 / Full event text, not word-aligned" if effective and effective["text"] else ""
            cells = [str(shot["shot_no"] or shot["shot_id"]),
                     f"{link['overlap_start']:.3f}–{link['overlap_end']:.3f}s",
                     f"{event['kind']} · identity {event['identity_status']}", content,
                     f"{state}; source {event['source_id']}; event {event['event_id']}; original {event['start_time']:.3f}–{event['end_time']:.3f}s. {scope}"]
            rows.append("<tr>" + "".join("<td>" + html.escape(cell).replace("\n", "<br>") + "</td>" for cell in cells) + "</tr>")
    if not rows:
        rows.append('<tr><td colspan="5">无已记录事件 / No recorded events. This does not establish silence.</td></tr>')
    note = f'按时间顺序预览前 {min(total_links, MAX_AUDIO_PREVIEW_ROWS)} / {total_links} 条关联；完整数据见 <code>data/visualization_dataset.json#audio_associations</code>。Chronological preview only; complete links remain in JSON.'
    return '<section id="audio-evidence" class="panel tablepanel"><h2>镜头音频证据 / Shot audio evidence</h2><p>类别与文字须结合来源/复核状态；同一原始事件可跨多个镜头。Unknown is not silence.</p><div class="tablewrap"><table><thead><tr><th>镜头 / Shot</th><th>重叠范围 / Overlap</th><th>类别 / Kind</th><th>原事件内容 / Event content</th><th>来源与复核 / Provenance</th></tr></thead><tbody>' + "".join(rows) + '</tbody></table></div><p class="note">' + note + '</p></section>'


def _zh_value(value: str) -> str:
    if value.startswith(HEURISTIC_BEAT_PREFIX):
        return f"启发式解释（未验证）：{_zh_value(_strip_heuristic_marker(value))}"
    for prefix, translated in (
        ("Heuristic interpretation (unverified): ", "启发式解释（未验证）："),
        ("Interpretation/review note (unverified): ", "解释/复核备注（未验证）："),
    ):
        if value.startswith(prefix):
            return f"{translated}{_zh_value(value[len(prefix):])}"
    mapping = {
        "unknown": "待复核",
        "TBD": "待定",
        "to annotate": "待标注",
        "to annotate from frame": "待根据画面标注",
        "not inferable from final video": "无法仅凭成片可靠反推",
        "fast": "快",
        "controlled": "可控",
        "slow": "慢",
        "medium": "中等",
        "high": "高",
        "low": "低",
        "opening_sequence": "开场序列",
        "early_sequence": "前段序列",
        "middle_sequence": "中段序列",
        "late_sequence": "后段序列",
        "closing_sequence": "收尾序列",
        "wide": "远景/全景",
        "close-up": "近景/特写",
        "detail": "细节",
        "static": "固定镜头",
        "eye-level": "平视",
        "low angle": "低机位",
        "high angle": "高机位",
        "overhead/graphic": "俯拍/图形化",
        "slow movement": "缓慢运动",
        "reframe": "重新构图",
        "push-in": "推进",
        "handheld/kinetic": "手持/动态",
        "cutaway": "插入/切出",
        "center-weighted": "中心构图",
        "subject-led": "主体主导",
        "graphic/insert": "图形/插入",
        "environment-led": "环境主导",
        "music-led": "音乐主导",
        "sync": "同期/同步",
        "tripod": "三脚架",
        "dolly/gimbal": "轨道/稳定器",
        "locked-off": "锁定机位",
        "handheld": "手持",
        "gimbal": "稳定器",
        "macro/insert rig": "微距/插入镜头设备",
        "review required": "需要复核",
        "machine segmented; visual fields require human/model annotation": "机器分段；画面字段需要人工或视觉模型标注",
        "to annotate blocking, screen direction, and action beat": "待标注调度、画面方向与动作节拍",
        "to annotate lighting, VFX, and AI artifacts": "待标注灯光、VFX 与 AI 痕迹",
        "audio/rhythm detected; dialogue requires ASR or subtitle import": "已检测音频/节奏；对白需 ASR 或字幕导入",
        "vision annotated; verify against source before final evidence use": "已视觉标注；最终使用证据前请对照原片复核",
        "MiniMax MCP vision annotated; verify against source before final evidence use": "MiniMax MCP 已完成视觉标注；最终使用证据前请对照原片复核",
        "review blocking and screen direction": "复核调度与画面方向",
        "review practical light, VFX, and AI artifacts": "复核实际光源、VFX 与 AI 痕迹",
        "review dialogue, music, and SFX relationship": "复核对白、音乐与音效关系",
        "pending audio sync": "等待音频同步",
        "first-pass generated row; review required": "首轮生成行，需要人工复核",
        "dense rhythm peaks; check edit/music alignment": "节奏峰值密集，检查剪辑与音乐卡点",
        "moderate rhythm activity": "中等节奏活动",
        "sparse rhythm activity": "节奏活动较稀疏",
        "Review the frame for hook, product/topic visibility, and edit emphasis.": "复核该画面的开场抓力、产品/主题可见度与剪辑强调。",
        "Review the frame for concept, motif, mood, and visual continuity.": "复核该画面的概念、母题、情绪与视觉连续性。",
        "Review the frame for scene function, subject action, and continuity.": "复核该画面的场景功能、主体动作与连续性。",
        "The analysis prioritizes opening hook, beat alignment, and CTA-ready pacing.": "本分析优先关注开场抓力、音乐卡点与 CTA 前后的节奏组织。",
        "The analysis prioritizes concept clarity, mood continuity, and audiovisual intent.": "本分析优先关注概念清晰度、情绪连续性与音画意图。",
        "The analysis prioritizes scene flow, continuity, and emotional energy.": "本分析优先关注场景流动、连续性与情绪能量。",
        "Review scene grouping against actual narrative or emotional turns.": "对照实际叙事或情绪转折复核场景分组。",
        "Check whether recurring motifs are intentional enough to name in a client deck.": "检查重复母题是否足够明确，能否写入客户提案或复盘文档。",
        "No usable transcript was produced; run ASR again or import subtitles before final client delivery.": "当前未生成可用字幕；正式交付前应重新运行 ASR 或导入字幕。",
        "Rhythm peak density is low; verify whether that is intentional restraint or a pacing issue.": "节奏峰值密度偏低；需判断这是有意克制还是节奏问题。",
        "Rhythm peak density is high; verify that edits and sound hits do not flatten emphasis.": "节奏峰值密度偏高；需检查剪辑和声音重音是否削弱重点。",
        "Review the first 3-5 seconds against the strongest visual and audio peaks.": "用最强视觉点和声音峰值复核前 3-5 秒的开场抓力。",
        "Check whether brand, product, or topic recognition appears before viewer attention drops.": "检查品牌、产品或主题识别是否在注意力下降前出现。",
    }
    return mapping.get(value, value)


def _storyboard_row(shot: Shot, zh: bool, include_generation: bool = True) -> str:
    def v(value: str) -> str:
        return _zh_value(value) if zh else value

    def escaped(value: object) -> str:
        return html.escape(str(value))

    thumb = f"../assets/keyframes/{shot.frame_ref}" if shot.frame_ref else "../assets/contact_sheet.jpg"
    tc = f"{escaped(shot.timecode)}<br><span class='small'>{escaped(f'{shot.duration:.1f}s')}</span>"
    shot_label = (
        f"{escaped(shot.scene_no)}-{escaped(shot.shot_no)}"
        f"<br><span class='small'>{escaped(shot.setup_id)}</span>"
    )
    content = escaped(v(shot.content_summary or shot.visual_description))
    displayed_dialogue = shot.dialogue
    if not displayed_dialogue and (shot.annotation_source or "machine").strip().lower() == "machine":
        displayed_dialogue = shot.speech_summary
    sound = (
        f"{escaped(displayed_dialogue)}"
        f"<br><span class='small'>{escaped(v(shot.sound_sync))} / {escaped(v(shot.audio_notes))}</span>"
    )
    rhythm = (
        f"{escaped(v(shot.music_state))}"
        f"<br><span class='small'>{escaped(f'{shot.beat_density:.2f}')} / {escaped(v(shot.rhythm_notes))}</span>"
    )
    notes = (
        f"{escaped(v(shot.review_notes))}"
        f"<br><span class='small'>{escaped(v(shot.style_notes or shot.continuity_notes))}</span>"
    )
    cells = [
        shot_label,
        f"<img class='thumb' src='{escaped(thumb)}' alt='frame {escaped(shot.shot_no)}'>",
        tc,
        content,
        escaped(v(shot.scene_type or "to annotate")),
        escaped(v(shot.shot_scale)),
        escaped(v(shot.camera_angle)),
        escaped(v(shot.camera_motion)),
        escaped(v(shot.composition)),
        sound,
        rhythm,
    ]
    if include_generation:
        prompt = shot.prompt_zh if zh and shot.prompt_zh else shot.prompt_en
        if not prompt:
            prompt = "creative generation interpretation not supplied" if not zh else "未提供创意生成解释"
        cells.append(escaped(prompt))
    cells.append(notes)
    return f'<tr id="shot-{escaped(shot.shot_no)}">' + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>"


def _shot_atlas_item(shot: Shot, zh: bool, index: int) -> str:
    def v(value: str) -> str:
        return _zh_value(value) if zh else value

    thumb = f"../assets/keyframes/{shot.frame_ref}" if shot.frame_ref else "../assets/contact_sheet.jpg"
    title = v(shot.content_summary or shot.visual_description or "to annotate from frame")
    scene_type = v(shot.scene_type or "to annotate")
    shot_scale = v(shot.shot_scale)
    camera_motion = v(shot.camera_motion)
    review = v(shot.review_notes)
    style = f"animation-delay:{min(index * 0.025, 0.6):.3f}s"
    return (
        f'<a class="shotItem" style="{style}" href="#shot-{html.escape(str(shot.shot_no))}">'
        f'<div><div class="shotNo">{html.escape(str(shot.shot_no).zfill(2))}</div><div class="shotMeta">{html.escape(shot.timecode)} / {shot.duration:.1f}s</div></div>'
        f'<img class="shotThumb" src="{html.escape(thumb)}" alt="shot {html.escape(str(shot.shot_no))}">'
        f'<div><div class="shotName">{html.escape(title)}</div><div class="shotMeta">{html.escape(scene_type)}</div></div>'
        f'<div class="shotMeta">{html.escape(shot_scale)}<br>{html.escape(camera_motion)}</div>'
        f'<div class="shotMeta">{html.escape(review)}</div>'
        "</a>"
    )


def render_pdf_report(html_path: Path, pdf_path: Path) -> bool:
    if __import__("shutil").which("wkhtmltopdf"):
        with atomic_output_path(pdf_path) as temporary:
            run_command(["wkhtmltopdf", str(html_path), str(temporary)], timeout=120)
        return True
    pdf_path.unlink(missing_ok=True)
    return False
