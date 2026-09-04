from __future__ import annotations

import json
import os
from typing import Any

from ._audio_intelligence_metadata import validate_parameters
from ._audio_intelligence_receipt import (
    AUDIO_INTELLIGENCE_DIGEST_ALGORITHM,
    AUDIO_INTELLIGENCE_RECEIPT_SCHEMA_VERSION,
    FileReceiptReader,
    build_audio_intelligence_receipt,
    validate_receipt_structure,
)
from ._audio_intelligence_receipt import (
    audio_intelligence_binding_locked as _receipt_binding_locked,
)
from ._audio_intelligence_schema import (
    AUDIO_TIME_RANGE_SEMANTICS,
    AUDIO_TIMELINE_SCHEMA_ID,
    CAPABILITIES,
    CAPABILITY_STATUSES,
    EVENT_KINDS,
    PROPOSAL_VERIFICATIONS,
    REVIEW_STATUSES,
    SOURCE_TYPES,
    VOICE_ROLES,
    proposal_sha256,
    resolve_effective_proposal,
    validate_audio_timeline,
)
from ._audio_intelligence_storage import (
    AUDIO_INTELLIGENCE_FILE_DIGEST_MODE,
    MAX_AUDIO_INPUT_BYTES,
    MAX_AUDIO_INTELLIGENCE_BYTES,
    cleanup_recovery_directories,
    commit_audio_intelligence,
    file_receipt,
    recovery_state,
    staging_area,
    write_staged_file,
)
from .artifacts import artifact_path
from .paths import ProjectPaths
from .safe_io import advisory_file_lock

# Private compatibility hooks retained across the internal module split.
_file_receipt = file_receipt
_audio_intelligence_binding_locked = _receipt_binding_locked


def audio_intelligence_status(
    paths: ProjectPaths, *, _shots_lock_held: bool = False
) -> dict[str, Any]:
    dataset_path = artifact_path(paths.root, "audio_intelligence")
    receipt_path = artifact_path(paths.root, "audio_intelligence_generation")
    dataset_present = os.path.lexists(dataset_path)
    receipt_present = os.path.lexists(receipt_path)
    try:
        cleanup = recovery_state(paths)
    except ValueError as exc:
        return {
            "available": dataset_present or receipt_present,
            "valid": False,
            "binding": None,
            "reasons": [str(exc)],
        }
    if not dataset_present and not receipt_present:
        if cleanup["cleanup_required"]:
            return {
                "available": False,
                "valid": False,
                "binding": None,
                "reasons": ["audio intelligence recovery requires operator cleanup"],
            }
        return {"available": False, "valid": True, "binding": None, "reasons": []}
    try:
        binding = audio_intelligence_binding(
            paths, _shots_lock_held=_shots_lock_held
        )
    except ValueError as exc:
        return {
            "available": True,
            "valid": False,
            "binding": None,
            "reasons": [str(exc)],
        }
    return {"available": True, "valid": True, "binding": binding, "reasons": []}


def stage_and_commit_audio_intelligence(
    paths: ProjectPaths,
    dataset: Any,
    *,
    parameters: Any | None = None,
    expected_audio_wav: dict[str, Any] | None = None,
    expected_generation_id: str | None = None,
    expected_inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validated_dataset = validate_audio_timeline(dataset)
    validated_parameters = validate_parameters({} if parameters is None else parameters)
    dataset_bytes = _pretty_json_bytes(validated_dataset)
    paths.ensure()
    with advisory_file_lock(paths.data / ".shots.lock", root=paths.root):
        if expected_generation_id is not None:
            current = _audio_intelligence_binding_locked(paths, file_receipt_reader=None)
            if current["generation_id"] != expected_generation_id:
                raise ValueError("Audio generation changed before commit")
        with staging_area(paths.root) as area:
            write_staged_file(area, "audio_intelligence.json", dataset_bytes)
            receipt = build_audio_intelligence_receipt(
                paths,
                validated_dataset,
                validated_parameters,
                dataset_bytes=dataset_bytes,
            )
            if expected_inputs is not None and receipt["inputs"] != expected_inputs:
                raise ValueError("Audio evidence inputs changed before commit")
            if expected_audio_wav is not None:
                current = receipt["inputs"]["audio_wav"]
                if set(expected_audio_wav) != {"sha256", "size_bytes"} or any(
                    current[key] != value for key, value in expected_audio_wav.items()
                ):
                    raise ValueError("Audio input changed before timeline commit")
            write_staged_file(
                area,
                "audio_intelligence_generation.json",
                _pretty_json_bytes(receipt),
            )
            validate_receipt_structure(receipt)
            binding = commit_audio_intelligence(
                paths,
                area,
                validate_committed=lambda data_fd: _audio_intelligence_binding_locked(
                    paths,
                    file_receipt_reader=None,
                    data_fd=data_fd,
                ),
            )
        result = _with_cleanup_state(binding, paths)
        if area.cleanup_warnings:
            result["cleanup_required"] = True
            result["cleanup_warnings"] = list(area.cleanup_warnings)
        return result


def audio_intelligence_binding(
    paths: ProjectPaths,
    *,
    file_receipt_reader: FileReceiptReader | None = None,
    _shots_lock_held: bool = False,
) -> dict[str, Any]:
    if _shots_lock_held:
        core = _audio_intelligence_binding_locked(
            paths,
            file_receipt_reader=file_receipt_reader,
        )
        return _with_cleanup_state(core, paths)
    with advisory_file_lock(paths.data / ".shots.lock", root=paths.root):
        core = _audio_intelligence_binding_locked(
            paths,
            file_receipt_reader=file_receipt_reader,
        )
        return _with_cleanup_state(core, paths)


def cleanup_audio_intelligence_recovery(paths: ProjectPaths) -> dict[str, Any]:
    with advisory_file_lock(paths.data / ".shots.lock", root=paths.root):
        return cleanup_recovery_directories(
            paths,
            validate_current=lambda: _audio_intelligence_binding_locked(
                paths,
                file_receipt_reader=None,
            ),
        )


def _pretty_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")


def _with_cleanup_state(binding: dict[str, Any], paths: ProjectPaths) -> dict[str, Any]:
    return {**binding, **recovery_state(paths)}


__all__ = [
    "AUDIO_INTELLIGENCE_DIGEST_ALGORITHM",
    "AUDIO_INTELLIGENCE_FILE_DIGEST_MODE",
    "AUDIO_INTELLIGENCE_RECEIPT_SCHEMA_VERSION",
    "AUDIO_TIMELINE_SCHEMA_ID",
    "AUDIO_TIME_RANGE_SEMANTICS",
    "CAPABILITIES",
    "CAPABILITY_STATUSES",
    "EVENT_KINDS",
    "MAX_AUDIO_INPUT_BYTES",
    "MAX_AUDIO_INTELLIGENCE_BYTES",
    "PROPOSAL_VERIFICATIONS",
    "REVIEW_STATUSES",
    "SOURCE_TYPES",
    "VOICE_ROLES",
    "FileReceiptReader",
    "audio_intelligence_binding",
    "audio_intelligence_status",
    "cleanup_audio_intelligence_recovery",
    "proposal_sha256",
    "resolve_effective_proposal",
    "stage_and_commit_audio_intelligence",
    "validate_audio_timeline",
]
