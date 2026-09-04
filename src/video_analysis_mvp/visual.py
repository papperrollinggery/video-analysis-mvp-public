from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any, Callable

from .paths import ProjectPaths
from .safe_io import (
    advisory_file_lock,
    atomic_output_path,
    atomic_write_text,
    ensure_output_directory,
    read_regular_bytes,
)
from .schemas import AnalysisProfile, CanonicalMediaPackage, Scene, Shot, dump_json, load_json
from .utils import format_clock, run_command


VISUAL_GENERATION_SCHEMA_VERSION = 2
VISUAL_GENERATION_DIGEST_ALGORITHM = "sha256"
VISUAL_FILE_DIGEST_MODE = "sha256-file-v1"
VISUAL_KEYFRAME_SET_DIGEST_MODE = "sha256-file-set-v1"
VISUAL_SHOT_STRUCTURE_DIGEST_MODE = "canonical-shot-structure-v1"
MAX_VISUAL_ARTIFACT_BYTES = 64 * 1024 * 1024
VISUAL_SHOT_STRUCTURE_FIELDS = (
    "shot_id",
    "scene_no",
    "shot_no",
    "setup_id",
    "start_time",
    "end_time",
    "duration",
    "timecode",
    "frame_ref",
    "primary_frame_ref",
    "frame_refs",
    "boundary_confidence",
)
FileReceiptReader = Callable[[Path, int], dict[str, Any]]


def analyze_visual(media: CanonicalMediaPackage, paths: ProjectPaths, interval_seconds: float = 8.0) -> tuple[list[Shot], list[Scene]]:
    video_path = Path(media.review_copy_path)
    segments, method = _detect_shot_segments(video_path, media)
    shots = _build_shots(media, segments, method)
    scenes = _build_scenes(shots, media.analysis_profile)
    _stage_and_commit_visual_generation(
        video_path,
        paths,
        shots,
        scenes,
        media.analysis_profile,
        interval_seconds,
    )
    return shots, scenes


def _stage_and_commit_visual_generation(
    video_path: Path,
    paths: ProjectPaths,
    shots: list[Shot],
    scenes: list[Scene],
    profile: AnalysisProfile | str,
    interval_seconds: float,
) -> None:
    ensure_output_directory(paths.assets, root=paths.root)
    with tempfile.TemporaryDirectory(prefix=".visual-stage-", dir=paths.assets) as directory:
        staging_root = Path(directory)
        staged_paths = ProjectPaths(staging_root)
        staged_paths.ensure()
        staging_keyframes = staged_paths.keyframes
        staged_contact_sheet = staged_paths.assets / "contact_sheet.jpg"
        _extract_shot_frames(video_path, staging_keyframes, shots)
        # FFmpeg 8 no longer flushes an incomplete tile at EOF.  Request a
        # complete 3x4 set of evenly distributed samples for every clip.
        duration_seconds = max((shot.end_time for shot in shots), default=interval_seconds)
        contact_sheet_interval = min(max(interval_seconds, 0.01), max(duration_seconds / 12, 0.01))
        _build_contact_sheet(video_path, staged_contact_sheet, contact_sheet_interval)
        expected_names = [name for shot in shots for name in shot.frame_refs]
        staged_shots = staged_paths.data / "shots.json"
        staged_scenes = staged_paths.data / "scenes.json"
        staged_csv = staged_paths.reports / "shot_breakdown.csv"
        staged_receipt = staged_paths.data / "visual_generation.json"
        dump_json(staged_shots, shots)
        dump_json(staged_scenes, scenes)
        write_shots_csv(staged_csv, shots, profile)
        _validate_staged_visual_generation(
            staging_keyframes,
            staged_contact_sheet,
            (staged_shots, staged_scenes, staged_csv),
            expected_names,
        )
        dump_json(staged_receipt, _build_visual_generation_receipt(staged_paths, shots, scenes))
        visual_generation_binding(staged_paths, shots, scenes)
        _commit_visual_generation(paths, staged_paths, expected_names)


def _validate_staged_visual_generation(
    keyframes_dir: Path,
    contact_sheet: Path,
    tabular_outputs: tuple[Path, ...],
    expected_names: list[str],
) -> None:
    if len(expected_names) != len(set(expected_names)):
        raise ValueError("Generated keyframe names must be unique")
    if any(
        Path(name).name != name
        or re.fullmatch(r"shot_[0-9]{4}_(?:start|mid|end)\.jpg", name) is None
        for name in expected_names
    ):
        raise ValueError("Generated keyframe names must use the managed shot filename format")
    expected = set(expected_names)
    actual = {candidate.name for candidate in keyframes_dir.iterdir()}
    if actual != expected:
        raise ValueError("Visual extraction did not produce the complete expected keyframe set")
    for candidate in [*(keyframes_dir / name for name in expected_names), contact_sheet, *tabular_outputs]:
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            raise ValueError(f"Visual extraction output is missing: {candidate.name}") from None
        if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
            raise ValueError(f"Visual extraction output is not a non-empty regular file: {candidate.name}")


def _commit_visual_generation(
    paths: ProjectPaths,
    staged_paths: ProjectPaths,
    expected_names: list[str],
) -> None:
    ensure_output_directory(paths.keyframes, root=paths.root)
    ensure_output_directory(paths.data, root=paths.root)
    ensure_output_directory(paths.reports, root=paths.root)
    staged_keyframes = staged_paths.keyframes
    staged_targets = [
        *((staged_keyframes / name, paths.keyframes / name) for name in expected_names),
        (staged_paths.assets / "contact_sheet.jpg", paths.assets / "contact_sheet.jpg"),
        (staged_paths.data / "shots.json", paths.data / "shots.json"),
        (staged_paths.data / "scenes.json", paths.data / "scenes.json"),
        (staged_paths.reports / "shot_breakdown.csv", paths.reports / "shot_breakdown.csv"),
        # This commit marker is deliberately last.  Its digests describe every
        # artifact above, so absence or mismatch means the generation is not a
        # complete publishable snapshot.
        (staged_paths.data / "visual_generation.json", paths.data / "visual_generation.json"),
    ]
    with advisory_file_lock(paths.data / ".shots.lock", root=paths.root):
        managed_existing = sorted(paths.keyframes.glob("shot_*.jpg"))
        fixed_targets = [destination for _staged, destination in staged_targets if destination.parent != paths.keyframes]
        existing_targets = [
            *managed_existing,
            *(candidate for candidate in fixed_targets if os.path.lexists(candidate)),
        ]
        for candidate in existing_targets:
            if not stat.S_ISREG(candidate.lstat().st_mode):
                raise ValueError(f"Managed visual generation target is unsafe: {candidate}")

        rollback_root = staged_paths.root / "rollback"
        rollback_root.mkdir(mode=0o700)
        moved_old: list[tuple[Path, Path]] = []
        committed: list[Path] = []
        try:
            for current in existing_targets:
                backup = rollback_root / current.relative_to(paths.root)
                backup.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.replace(current, backup)
                moved_old.append((current, backup))
            for staged, destination in staged_targets:
                os.replace(staged, destination)
                committed.append(destination)
            _fsync_visual_generation_directories(paths)
        except Exception:
            for destination in reversed(committed):
                try:
                    destination.unlink()
                except FileNotFoundError:
                    pass
            for original, backup in reversed(moved_old):
                if os.path.lexists(backup):
                    os.replace(backup, original)
            _fsync_visual_generation_directories(paths)
            raise


def _build_visual_generation_receipt(
    paths: ProjectPaths,
    shots: list[Shot],
    scenes: list[Scene],
    *,
    file_receipt_reader: FileReceiptReader | None = None,
) -> dict[str, Any]:
    """Build the strict receipt committed after every visual artifact.

    The shot receipt intentionally hashes only measured structure. Synthesis and
    provider review may add dialogue, rhythm, and annotation fields later while
    the measured boundaries and exact frame lineage remain immutable.
    """
    reader = file_receipt_reader or _default_file_receipt
    frame_names = _visual_frame_names(shots)
    keyframe_files: list[dict[str, Any]] = []
    for name in frame_names:
        current = reader(paths.keyframes / name, MAX_VISUAL_ARTIFACT_BYTES)
        keyframe_files.append(
            {
                "path": f"assets/keyframes/{name}",
                "sha256": current["sha256"],
                "size_bytes": current["size_bytes"],
            }
        )
    contact = reader(paths.assets / "contact_sheet.jpg", MAX_VISUAL_ARTIFACT_BYTES)
    scenes_file = reader(paths.data / "scenes.json", MAX_VISUAL_ARTIFACT_BYTES)
    shot_structure = _visual_shot_structure(shots)
    artifacts = {
        "contact_sheet": {
            "path": "assets/contact_sheet.jpg",
            "kind": "file",
            "digest_mode": VISUAL_FILE_DIGEST_MODE,
            "sha256": contact["sha256"],
            "size_bytes": contact["size_bytes"],
        },
        "keyframes": {
            "path": "assets/keyframes",
            "kind": "file_set",
            "digest_mode": VISUAL_KEYFRAME_SET_DIGEST_MODE,
            "sha256": _keyframe_set_digest(keyframe_files),
            "size_bytes": sum(item["size_bytes"] for item in keyframe_files),
            "file_count": len(keyframe_files),
            "files": keyframe_files,
        },
        "scenes": {
            "path": "data/scenes.json",
            "kind": "file",
            "digest_mode": VISUAL_FILE_DIGEST_MODE,
            "sha256": scenes_file["sha256"],
            "size_bytes": scenes_file["size_bytes"],
        },
        "shot_structure": {
            "path": "data/shots.json",
            "kind": "canonical_json_projection",
            "digest_mode": VISUAL_SHOT_STRUCTURE_DIGEST_MODE,
            "sha256": hashlib.sha256(_canonical_json_bytes(shot_structure)).hexdigest(),
            "shot_count": len(shots),
        },
    }
    core = {
        "digest_algorithm": VISUAL_GENERATION_DIGEST_ALGORITHM,
        "shot_ids": [shot.shot_id for shot in shots],
        "scene_ids": [scene.scene_id for scene in scenes],
        "artifacts": artifacts,
    }
    return {
        "schema_version": VISUAL_GENERATION_SCHEMA_VERSION,
        "generation_id": hashlib.sha256(_canonical_json_bytes(core)).hexdigest(),
        **core,
    }


def verify_visual_generation(paths: ProjectPaths) -> tuple[bool, list[str]]:
    try:
        visual_generation_binding(paths)
    except ValueError as exc:
        return False, [str(exc)]
    return True, []


def visual_generation_binding(
    paths: ProjectPaths,
    shots: list[Shot] | None = None,
    scenes: list[Scene] | None = None,
    *,
    file_receipt_reader: FileReceiptReader | None = None,
) -> dict[str, Any]:
    """Validate visual lineage against current immutable assets and structure."""
    try:
        receipt = load_json(paths.data / "visual_generation.json")
    except FileNotFoundError:
        raise ValueError("visual generation receipt is missing") from None
    except Exception as exc:
        raise ValueError(f"visual generation receipt is unreadable: {exc}") from None
    if type(receipt) is not dict:
        raise ValueError("visual generation receipt must be an object")
    if set(receipt) != {
        "schema_version",
        "generation_id",
        "digest_algorithm",
        "shot_ids",
        "scene_ids",
        "artifacts",
    }:
        raise ValueError("visual generation receipt fields are invalid")
    if receipt.get("schema_version") != VISUAL_GENERATION_SCHEMA_VERSION:
        raise ValueError("visual generation receipt schema version is unsupported")
    if receipt.get("digest_algorithm") != VISUAL_GENERATION_DIGEST_ALGORITHM:
        raise ValueError("visual generation digest algorithm is unsupported")
    try:
        raw_shots = load_json(paths.data / "shots.json")
        if type(raw_shots) is not list or any(
            type(item) is not dict or any(field not in item for field in VISUAL_SHOT_STRUCTURE_FIELDS)
            for item in raw_shots
        ):
            raise ValueError("shots.json lacks required measured structure fields")
        raw_structure = [
            {field: item[field] for field in VISUAL_SHOT_STRUCTURE_FIELDS}
            for item in raw_shots
        ]
        disk_shots = [_strict_model_validate(Shot, item) for item in raw_shots]
        current_shots = shots if shots is not None else disk_shots
        if _visual_shot_structure(current_shots) != _visual_shot_structure(disk_shots):
            raise ValueError("provided shots do not match current measured structure")
        if scenes is not None:
            current_scenes = scenes
        else:
            raw_scenes = load_json(paths.data / "scenes.json")
            if type(raw_scenes) is not list:
                raise ValueError("scenes.json must be an array")
            current_scenes = [_strict_model_validate(Scene, item) for item in raw_scenes]
    except Exception as exc:
        raise ValueError(f"visual generation structure is missing or invalid: {exc}") from None
    shot_ids = [shot.shot_id for shot in current_shots]
    scene_ids = [scene.scene_id for scene in current_scenes]
    if receipt.get("shot_ids") != shot_ids or len(shot_ids) != len(set(shot_ids)):
        raise ValueError("visual generation shot ids do not exactly match current shots")
    if receipt.get("scene_ids") != scene_ids or len(scene_ids) != len(set(scene_ids)):
        raise ValueError("visual generation scene ids do not exactly match current scenes")
    artifacts = receipt.get("artifacts")
    if type(artifacts) is not dict or set(artifacts) != {
        "contact_sheet",
        "keyframes",
        "scenes",
        "shot_structure",
    }:
        raise ValueError("visual generation artifact ids are invalid")

    reader = file_receipt_reader or _default_file_receipt
    _validate_visual_file_artifact(
        artifacts.get("contact_sheet"),
        "assets/contact_sheet.jpg",
        paths.assets / "contact_sheet.jpg",
        reader,
    )
    _validate_visual_file_artifact(
        artifacts.get("scenes"),
        "data/scenes.json",
        paths.data / "scenes.json",
        reader,
    )

    structure = artifacts.get("shot_structure")
    if type(structure) is not dict or set(structure) != {
        "path",
        "kind",
        "digest_mode",
        "sha256",
        "shot_count",
    }:
        raise ValueError("visual shot structure receipt fields are invalid")
    if (
        type(structure.get("sha256")) is not str
        or re.fullmatch(r"[0-9a-f]{64}", structure["sha256"]) is None
        or isinstance(structure.get("shot_count"), bool)
        or not isinstance(structure.get("shot_count"), int)
        or structure["shot_count"] < 0
    ):
        raise ValueError("visual shot structure receipt is invalid")
    expected_structure_digest = hashlib.sha256(
        _canonical_json_bytes(raw_structure)
    ).hexdigest()
    if (
        structure.get("path") != "data/shots.json"
        or structure.get("kind") != "canonical_json_projection"
        or structure.get("digest_mode") != VISUAL_SHOT_STRUCTURE_DIGEST_MODE
        or structure.get("shot_count") != len(current_shots)
        or structure.get("sha256") != expected_structure_digest
    ):
        raise ValueError("visual shot structure digest mismatch")

    keyframes = artifacts.get("keyframes")
    if type(keyframes) is not dict or set(keyframes) != {
        "path",
        "kind",
        "digest_mode",
        "sha256",
        "size_bytes",
        "file_count",
        "files",
    }:
        raise ValueError("visual keyframe set receipt fields are invalid")
    stored_files = keyframes.get("files")
    if type(stored_files) is not list:
        raise ValueError("visual keyframe file receipts are invalid")
    frame_names = _visual_frame_names(current_shots)
    expected_paths = [f"assets/keyframes/{name}" for name in frame_names]
    if keyframes.get("path") != "assets/keyframes" or keyframes.get("kind") != "file_set":
        raise ValueError("visual keyframe set path or kind is invalid")
    if keyframes.get("digest_mode") != VISUAL_KEYFRAME_SET_DIGEST_MODE:
        raise ValueError("visual keyframe set digest mode is invalid")
    if (
        type(keyframes.get("sha256")) is not str
        or re.fullmatch(r"[0-9a-f]{64}", keyframes["sha256"]) is None
        or isinstance(keyframes.get("size_bytes"), bool)
        or not isinstance(keyframes.get("size_bytes"), int)
        or keyframes["size_bytes"] < 0
        or isinstance(keyframes.get("file_count"), bool)
        or not isinstance(keyframes.get("file_count"), int)
        or keyframes["file_count"] < 0
    ):
        raise ValueError("visual keyframe set receipt is invalid")
    if [item.get("path") if type(item) is dict else None for item in stored_files] != expected_paths:
        raise ValueError("visual keyframe file ids do not exactly match current shots")

    current_files: list[dict[str, Any]] = []
    for name, stored in zip(frame_names, stored_files, strict=True):
        if type(stored) is not dict or set(stored) != {"path", "sha256", "size_bytes"}:
            raise ValueError(f"visual keyframe receipt fields are invalid: {name}")
        if (
            type(stored.get("sha256")) is not str
            or re.fullmatch(r"[0-9a-f]{64}", stored["sha256"]) is None
            or isinstance(stored.get("size_bytes"), bool)
            or not isinstance(stored.get("size_bytes"), int)
            or stored["size_bytes"] <= 0
        ):
            raise ValueError(f"visual keyframe receipt is invalid: {name}")
        try:
            current = reader(paths.keyframes / name, MAX_VISUAL_ARTIFACT_BYTES)
        except Exception as exc:
            raise ValueError(f"visual keyframe is missing, unsafe, or unreadable: {name} ({type(exc).__name__})") from None
        current_entry = {
            "path": f"assets/keyframes/{name}",
            "sha256": current["sha256"],
            "size_bytes": current["size_bytes"],
        }
        current_files.append(current_entry)
        if stored != current_entry:
            raise ValueError(f"visual keyframe digest mismatch: {name}")
    managed_pattern = re.compile(r"shot_[0-9]{4}_(?:start|mid|end)\.jpg")
    actual_managed = {
        candidate.name
        for candidate in paths.keyframes.iterdir()
        if managed_pattern.fullmatch(candidate.name)
    }
    expected_managed = {name for name in frame_names if managed_pattern.fullmatch(name)}
    if actual_managed != expected_managed:
        raise ValueError("visual managed keyframe set does not exactly match current shots")
    if (
        keyframes.get("file_count") != len(current_files)
        or keyframes.get("size_bytes") != sum(item["size_bytes"] for item in current_files)
        or keyframes.get("sha256") != _keyframe_set_digest(current_files)
    ):
        raise ValueError("visual keyframe set digest mismatch")

    core = {
        "digest_algorithm": receipt["digest_algorithm"],
        "shot_ids": receipt["shot_ids"],
        "scene_ids": receipt["scene_ids"],
        "artifacts": artifacts,
    }
    if receipt.get("generation_id") != hashlib.sha256(_canonical_json_bytes(core)).hexdigest():
        raise ValueError("visual generation id does not match its artifact receipts")
    return {
        "schema_version": VISUAL_GENERATION_SCHEMA_VERSION,
        "generation_id": receipt["generation_id"],
        "receipt_sha256": hashlib.sha256(_canonical_json_bytes(receipt)).hexdigest(),
    }


def _validate_visual_file_artifact(
    stored: Any,
    expected_path: str,
    current_path: Path,
    reader: FileReceiptReader,
) -> None:
    if type(stored) is not dict or set(stored) != {
        "path",
        "kind",
        "digest_mode",
        "sha256",
        "size_bytes",
    }:
        raise ValueError(f"visual file receipt fields are invalid: {expected_path}")
    if (
        stored.get("path") != expected_path
        or stored.get("kind") != "file"
        or stored.get("digest_mode") != VISUAL_FILE_DIGEST_MODE
        or type(stored.get("sha256")) is not str
        or re.fullmatch(r"[0-9a-f]{64}", stored["sha256"]) is None
        or isinstance(stored.get("size_bytes"), bool)
        or not isinstance(stored.get("size_bytes"), int)
        or stored["size_bytes"] <= 0
    ):
        raise ValueError(f"visual file receipt is invalid: {expected_path}")
    try:
        current = reader(current_path, MAX_VISUAL_ARTIFACT_BYTES)
    except Exception as exc:
        raise ValueError(f"visual artifact is missing, unsafe, or unreadable: {expected_path} ({type(exc).__name__})") from None
    if stored["sha256"] != current.get("sha256") or stored["size_bytes"] != current.get("size_bytes"):
        raise ValueError(f"visual artifact digest mismatch: {expected_path}")


def _visual_frame_names(shots: list[Shot]) -> list[str]:
    if any(not shot.frame_refs for shot in shots):
        raise ValueError("visual frame references are required for every shot")
    names = [name for shot in shots for name in shot.frame_refs]
    if len(names) != len(set(names)):
        raise ValueError("visual frame references must be unique")
    if any(
        type(name) is not str
        or not name
        or Path(name).name != name
        or any(part in {"", ".", ".."} for part in Path(name).parts)
        for name in names
    ):
        raise ValueError("visual frame references must be safe basenames")
    return names


def _visual_shot_structure(shots: list[Shot]) -> list[dict[str, Any]]:
    structures: list[dict[str, Any]] = []
    for shot in shots:
        raw = shot.model_dump(mode="json")
        structures.append({field: raw.get(field) for field in VISUAL_SHOT_STRUCTURE_FIELDS})
    return structures


def _strict_model_validate(model: Any, value: Any) -> Any:
    try:
        return model.model_validate(value, strict=True)
    except TypeError:  # pragma: no cover - lightweight fallback without Pydantic
        return model.model_validate(value)


def _keyframe_set_digest(files: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for item in files:
        digest.update(str(item["path"]).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(item["sha256"]).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(item["size_bytes"]).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _default_file_receipt(path: Path, max_bytes: int) -> dict[str, Any]:
    payload = read_regular_bytes(path, max_bytes=max_bytes)
    return {"sha256": hashlib.sha256(payload).hexdigest(), "size_bytes": len(payload)}


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _fsync_visual_generation_directories(paths: ProjectPaths) -> None:
    for directory in (paths.keyframes, paths.assets, paths.data, paths.reports):
        _fsync_directory_path(directory)


def _fsync_directory_path(path: Path) -> None:
    if os.name != "posix":  # pragma: no cover - exercised by Windows CI
        return
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _build_contact_sheet(
    video_path: Path,
    output_path: Path,
    interval_seconds: float,
) -> None:
    sample_rate = 1 / max(interval_seconds, 0.01)
    with atomic_output_path(output_path) as temporary:
        run_command(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(video_path),
                "-vf",
                f"fps={sample_rate:.8f},scale=320:-1,tile=3x4",
                "-frames:v",
                "1",
                "-pix_fmt",
                "yuvj420p",
                str(temporary),
            ],
            timeout=300,
        )


def _detect_shot_segments(video_path: Path, media: CanonicalMediaPackage) -> tuple[list[tuple[float, float, str]], str]:
    duration = max(media.duration_seconds, 0.1)
    scene_times = _scene_change_times(video_path, duration)
    if len(scene_times) < 2 and duration > 6:
        return _fallback_segments(media), "fixed_cadence"
    boundaries = [0.0, *scene_times, duration]
    segments = [(boundaries[index], boundaries[index + 1], "high") for index in range(len(boundaries) - 1)]
    segments = _merge_short_segments(segments, min_duration=0.75)
    segments = _split_long_segments(segments, media, max_duration=_max_shot_length(media.analysis_profile.value))
    return segments, "scene_detection"


def _scene_change_times(video_path: Path, duration: float, threshold: float = 0.24) -> list[float]:
    try:
        result = run_command(
            [
                "ffmpeg",
                "-hide_banner",
                "-i",
                str(video_path),
                "-vf",
                f"select=gt(scene\\,{threshold}),showinfo",
                "-an",
                "-f",
                "null",
                "-",
            ],
            timeout=300,
        )
    except Exception:
        return []
    times: list[float] = []
    for match in re.finditer(r"pts_time:([0-9]+(?:\.[0-9]+)?)", result.stderr):
        value = float(match.group(1))
        if 0.25 < value < duration - 0.25:
            times.append(round(value, 3))
    deduped: list[float] = []
    for value in sorted(times):
        if not deduped or value - deduped[-1] > 0.25:
            deduped.append(value)
    return deduped


def _fallback_segments(media: CanonicalMediaPackage) -> list[tuple[float, float, str]]:
    duration = max(media.duration_seconds, 0.1)
    target = _target_shot_length(media.analysis_profile.value, duration)
    count = max(1, math.ceil(duration / target))
    return [
        (
            round(index * duration / count, 3),
            round((index + 1) * duration / count, 3),
            "low",
        )
        for index in range(count)
    ]


def _merge_short_segments(segments: list[tuple[float, float, str]], min_duration: float) -> list[tuple[float, float, str]]:
    merged: list[tuple[float, float, str]] = []
    for start, end, confidence in segments:
        if merged and end - start < min_duration:
            prev_start, _prev_end, prev_confidence = merged[-1]
            merged[-1] = (prev_start, end, _lower_confidence(prev_confidence, confidence))
        else:
            merged.append((start, end, confidence))
    if len(merged) > 1 and merged[0][1] - merged[0][0] < min_duration:
        first_start, _first_end, first_confidence = merged.pop(0)
        next_start, next_end, next_confidence = merged.pop(0)
        merged.insert(0, (first_start, next_end, _lower_confidence(first_confidence, next_confidence)))
    return merged


def _split_long_segments(
    segments: list[tuple[float, float, str]],
    media: CanonicalMediaPackage,
    max_duration: float,
) -> list[tuple[float, float, str]]:
    split: list[tuple[float, float, str]] = []
    fallback_target = _target_shot_length(media.analysis_profile.value, media.duration_seconds)
    for start, end, confidence in segments:
        length = end - start
        if length <= max_duration:
            split.append((round(start, 3), round(end, 3), confidence))
            continue
        count = max(2, math.ceil(length / fallback_target))
        for index in range(count):
            chunk_start = start + index * length / count
            chunk_end = start + (index + 1) * length / count
            split.append((round(chunk_start, 3), round(chunk_end, 3), _lower_confidence(confidence, "medium")))
    return split


def _lower_confidence(left: str, right: str) -> str:
    order = {"low": 0, "medium": 1, "high": 2}
    return left if order.get(left, 0) <= order.get(right, 0) else right


def _build_shots(media: CanonicalMediaPackage, segments: list[tuple[float, float, str]], method: str) -> list[Shot]:
    shots: list[Shot] = []
    for index, (start, end, confidence) in enumerate(segments):
        length = round(end - start, 3)
        shot_id = f"shot_{index + 1:04d}"
        primary_frame = f"{shot_id}_mid.jpg"
        frame_refs = [f"{shot_id}_start.jpg", primary_frame, f"{shot_id}_end.jpg"]
        shots.append(
            Shot(
                shot_id=shot_id,
                scene_no=f"{(index // 4) + 1:03d}",
                shot_no=index + 1,
                setup_id=chr(65 + (index % 26)),
                take_no="",
                start_time=start,
                end_time=end,
                duration=length,
                timecode=f"{format_clock(start)}-{format_clock(end)}",
                frame_ref=primary_frame,
                primary_frame_ref=primary_frame,
                frame_refs=frame_refs,
                boundary_confidence=confidence,
                lens="not inferable from final video",
                equipment="not inferable from final video",
                int_ext="unknown",
                story_beat=_story_beat(index, len(segments), media.analysis_profile.value),
                scene_type=_story_beat(index, len(segments), media.analysis_profile.value),
                sound_sync="sync",
                audio_notes="audio/rhythm detected; dialogue requires ASR or subtitle import",
                beat_density=0.0,
                rhythm_notes="pending audio sync",
                sound_rhythm="pending audio sync",
                preferred_take="",
                estimated_production_time="",
                review_notes=f"{method}; machine boundary only; provider annotation or human review required",
                annotation_source="machine",
                readiness_status="blocked",
                confidence=0.34 if confidence == "high" else 0.24,
                visual_confidence=0.0,
            )
        )
    return shots


def _target_shot_length(profile: str, duration: float) -> float:
    if profile == "shortform":
        return 2.5
    if profile == "ads":
        return 3.0 if duration <= 90 else 5.0
    if profile == "festival":
        return 7.0
    return 6.0


def _max_shot_length(profile: str) -> float:
    if profile in {"ads", "shortform"}:
        return 5.5
    if profile == "festival":
        return 12.0
    return 8.0


def _story_beat(index: int, count: int, profile: str) -> str:
    if profile != "ads":
        if count <= 1 or index == 0:
            return "heuristic_unverified:opening_sequence"
        ratio = index / max(count - 1, 1)
        if ratio < 0.25:
            return "heuristic_unverified:early_sequence"
        if ratio < 0.65:
            return "heuristic_unverified:middle_sequence"
        if index == count - 1:
            return "heuristic_unverified:closing_sequence"
        return "heuristic_unverified:late_sequence"
    if count <= 1:
        return "heuristic_unverified:hook"
    ratio = index / max(count - 1, 1)
    if index == 0:
        return "heuristic_unverified:hook"
    if ratio < 0.25:
        return "heuristic_unverified:problem"
    if ratio < 0.55:
        return "heuristic_unverified:demo"
    if ratio < 0.78:
        return "heuristic_unverified:proof"
    if index == count - 1:
        return "heuristic_unverified:cta"
    return "heuristic_unverified:payoff"


def _extract_shot_frames(video_path: Path, keyframes_dir: Path, shots: list[Shot]) -> None:
    ensure_output_directory(keyframes_dir)
    for shot in shots:
        start = min(shot.end_time, shot.start_time + 0.1)
        mid = shot.start_time + max(shot.duration, 0.1) / 2
        end = max(shot.start_time, shot.end_time - 0.1)
        frame_times = [
            (shot.frame_refs[0], start),
            (shot.primary_frame_ref, mid),
            (shot.frame_refs[-1], end),
        ]
        for filename, seconds in frame_times:
            _extract_frame_at(video_path, keyframes_dir / filename, seconds)


def _extract_frame_at(video_path: Path, output_path: Path, seconds: float) -> None:
    with atomic_output_path(output_path) as temporary:
        run_command(
            [
                "ffmpeg",
                "-y",
                "-ss",
                f"{max(0.0, seconds):.3f}",
                "-i",
                str(video_path),
                "-frames:v",
                "1",
                "-vf",
                "scale=720:-2",
                "-q:v",
                "3",
                "-pix_fmt",
                "yuvj420p",
                str(temporary),
            ],
            timeout=120,
        )


def _build_scenes(
    shots: list[Shot],
    profile: AnalysisProfile | str = AnalysisProfile.research,
) -> list[Scene]:
    if not shots:
        return []
    profile_value = profile.value if isinstance(profile, AnalysisProfile) else profile
    group_size = 4 if len(shots) > 6 else max(1, len(shots))
    scenes: list[Scene] = []
    for idx in range(0, len(shots), group_size):
        chunk = shots[idx : idx + group_size]
        avg_duration = sum(shot.duration for shot in chunk) / len(chunk)
        pace = "fast" if avg_duration < 3 else "medium" if avg_duration < 7 else "slow"
        scenes.append(
            Scene(
                scene_id=f"scene_{len(scenes) + 1:03d}",
                start_time=chunk[0].start_time,
                end_time=chunk[-1].end_time,
                shot_ids=[shot.shot_id for shot in chunk],
                scene_function=_scene_function(len(scenes), profile_value),
                pace_label=pace,
                confidence=0.3,
            )
        )
    return scenes


def _scene_function(index: int, profile: AnalysisProfile | str = AnalysisProfile.research) -> str:
    profile_value = profile.value if isinstance(profile, AnalysisProfile) else profile
    if profile_value == AnalysisProfile.ads.value:
        return ["opening hook", "problem / demo", "proof / payoff", "CTA"][min(index, 3)]
    return ["opening sequence", "development", "turning point", "resolution"][min(index, 3)]


def write_shots_csv(
    path: Path,
    shots: list[Shot],
    profile: AnalysisProfile | str = AnalysisProfile.research,
) -> None:
    profile_value = profile.value if isinstance(profile, AnalysisProfile) else str(profile)
    fields = [
        "scene_no",
        "shot_no",
        "shot_id",
        "setup_id",
        "take_no",
        "timecode",
        "duration",
        "frame_ref",
        "primary_frame_ref",
        "frame_refs",
        "boundary_confidence",
        "story_beat",
        "shot_scale",
        "camera_angle",
        "camera_motion",
        "lens",
        "equipment",
        "composition",
        "subject",
        "subject_zh",
        "location",
        "int_ext",
        "props",
        "visual_description",
        "content_summary",
        "content_summary_zh",
        "scene_type",
        "action",
        "action_zh",
        "style_notes",
        "style_notes_zh",
    ]
    if profile_value == AnalysisProfile.ads.value:
        fields.extend(
            [
                "prompt_en",
                "prompt_zh",
                "remake_notes",
                "remake_notes_zh",
            ]
        )
    fields.extend(
        [
            "direction_notes",
            "direction_notes_zh",
            "lighting_vfx",
            "onscreen_text",
            "dialogue",
            "sound_design",
            "sound_sync",
            "audio_notes",
            "sound_rhythm",
            "music_state",
            "beat_density",
            "rhythm_notes",
            "motifs",
            "continuity_notes",
            "preferred_take",
            "estimated_production_time",
            "shoot_day",
            "review_notes",
            "annotation_source",
            "visual_confidence",
            "readiness_status",
            "readiness_reasons",
            "professional_ready",
            "confidence",
        ]
    )
    handle = io.StringIO(newline="")
    writer = csv.DictWriter(handle, fieldnames=fields)
    writer.writeheader()
    for shot in shots:
        raw = shot.model_dump(mode="json")
        row = {field: raw.get(field, "") for field in fields}
        if "motifs" in row:
            row["motifs"] = ";".join(row.get("motifs") or [])
        if "frame_refs" in row:
            row["frame_refs"] = ";".join(row.get("frame_refs") or [])
        if "readiness_reasons" in row:
            row["readiness_reasons"] = ";".join(row.get("readiness_reasons") or [])
        writer.writerow({field: _spreadsheet_safe(value) for field, value in row.items()})
    atomic_write_text(path, handle.getvalue())


def _spreadsheet_safe(value: object) -> object:
    """Neutralize text that spreadsheet apps may otherwise interpret as a formula."""
    if not isinstance(value, str):
        return value
    candidate = value.lstrip()
    if candidate.startswith(("=", "+", "-", "@", "\t", "\r")):
        return f"'{value}"
    return value
