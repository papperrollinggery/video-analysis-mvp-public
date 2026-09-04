from __future__ import annotations

import hashlib
import math
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ._audio_intelligence_metadata import validate_parameters
from ._audio_intelligence_schema import (
    AUDIO_TIMELINE_SCHEMA_ID,
    canonical_json_bytes,
    validate_audio_timeline,
    validate_capabilities,
    validate_sha256,
)
from ._audio_intelligence_storage import (
    AUDIO_INTELLIGENCE_FILE_DIGEST_MODE,
    MAX_AUDIO_INPUT_BYTES,
    MAX_AUDIO_INTELLIGENCE_BYTES,
    file_receipt,
    file_receipt_from_bytes,
    file_receipt_matches,
    read_file_bytes_and_receipt,
    read_relative_file_bytes_and_receipt,
    strict_json_loads,
)
from .artifacts import artifact_path
from .audio import audio_generation_binding
from .paths import ProjectPaths
from .schemas import CanonicalMediaPackage

AUDIO_INTELLIGENCE_RECEIPT_SCHEMA_VERSION = 1
AUDIO_INTELLIGENCE_DIGEST_ALGORITHM = "sha256"
RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "dataset_schema",
        "generation_id",
        "state",
        "digest_algorithm",
        "parameters",
        "capabilities",
        "inputs",
        "artifacts",
    }
)
INPUT_KEYS = frozenset({"audio_generation", "media_package", "audio_wav"})
AUDIO_GENERATION_BINDING_KEYS = frozenset(
    {"schema_version", "generation_id", "receipt_sha256"}
)
FILE_RECEIPT_KEYS = frozenset({"path", "kind", "digest_mode", "sha256", "size_bytes"})
ARTIFACT_KEYS = frozenset({"audio_intelligence"})
FileReceiptReader = Callable[[Path, int], dict[str, Any]]


def audio_intelligence_binding_locked(
    paths: ProjectPaths,
    *,
    file_receipt_reader: FileReceiptReader | None,
    data_fd: int | None = None,
) -> dict[str, Any]:
    try:
        raw_receipt, _receipt_file = _read_data_file(
            paths,
            "audio_intelligence_generation.json",
            MAX_AUDIO_INTELLIGENCE_BYTES,
            data_fd=data_fd,
        )
        receipt = strict_json_loads(raw_receipt)
    except FileNotFoundError:
        raise ValueError("audio intelligence receipt is missing") from None
    except ValueError as exc:
        raise ValueError(f"audio intelligence receipt is invalid: {exc}") from None
    except (OSError, UnicodeError) as exc:
        raise ValueError(
            f"audio intelligence receipt is unreadable: {type(exc).__name__}"
        ) from None
    receipt = validate_receipt_structure(receipt)
    reader = file_receipt_reader or file_receipt

    try:
        current_audio = audio_generation_binding(paths)
    except ValueError as exc:
        raise ValueError(f"bound audio generation is invalid: {exc}") from None
    if receipt["inputs"]["audio_generation"] != current_audio:
        raise ValueError(
            "audio intelligence audio-generation binding is stale or forged"
        )

    try:
        raw_media, current_media = _read_data_file(
            paths,
            "media_package.json",
            MAX_AUDIO_INTELLIGENCE_BYTES,
            data_fd=data_fd,
        )
        media = _parse_media_package(raw_media)
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(
            "audio intelligence input is missing, unsafe, or unreadable: "
            f"media_package ({type(exc).__name__})"
        ) from None
    if not file_receipt_matches(receipt["inputs"]["media_package"], current_media):
        raise ValueError("audio intelligence input digest mismatch: media_package")

    try:
        current_wav = reader(paths.assets / "audio.wav", MAX_AUDIO_INPUT_BYTES)
    except (OSError, ValueError) as exc:
        raise ValueError(
            "audio intelligence input is missing, unsafe, or unreadable: "
            f"audio_wav ({type(exc).__name__})"
        ) from None
    if not file_receipt_matches(receipt["inputs"]["audio_wav"], current_wav):
        raise ValueError("audio intelligence input digest mismatch: audio_wav")

    stored_dataset = receipt["artifacts"]["audio_intelligence"]
    try:
        raw_dataset, current_dataset = _read_data_file(
            paths,
            "audio_intelligence.json",
            MAX_AUDIO_INTELLIGENCE_BYTES,
            data_fd=data_fd,
        )
        dataset = validate_audio_timeline(strict_json_loads(raw_dataset))
    except ValueError as exc:
        raise ValueError(
            f"audio intelligence dataset is missing, unsafe, or invalid: {exc}"
        ) from None
    except (OSError, UnicodeError) as exc:
        raise ValueError(
            "audio intelligence dataset is missing, unsafe, or invalid: "
            f"{type(exc).__name__}"
        ) from None
    if not file_receipt_matches(stored_dataset, current_dataset):
        raise ValueError("audio intelligence dataset digest mismatch")
    if receipt["capabilities"] != dataset["capabilities"]:
        raise ValueError("audio intelligence capabilities do not match the dataset")
    _validate_media_binding(paths, dataset, media=media)
    return {
        "schema_version": AUDIO_INTELLIGENCE_RECEIPT_SCHEMA_VERSION,
        "dataset_schema": AUDIO_TIMELINE_SCHEMA_ID,
        "generation_id": receipt["generation_id"],
        "receipt_sha256": hashlib.sha256(raw_receipt).hexdigest(),
        "dataset_sha256": stored_dataset["sha256"],
        "capabilities": dataset["capabilities"],
    }


def build_audio_intelligence_receipt(
    paths: ProjectPaths,
    dataset: dict[str, Any],
    parameters: dict[str, Any],
    *,
    dataset_bytes: bytes,
    file_receipt_reader: FileReceiptReader | None = None,
) -> dict[str, Any]:
    reader = file_receipt_reader or file_receipt
    audio_binding = audio_generation_binding(paths)
    raw_media, current_media = read_file_bytes_and_receipt(
        artifact_path(paths.root, "media_package"),
        MAX_AUDIO_INTELLIGENCE_BYTES,
    )
    _validate_media_binding(paths, dataset, media=_parse_media_package(raw_media))
    inputs = {
        "audio_generation": audio_binding,
        "media_package": _named_file_receipt("data/media_package.json", current_media),
        "audio_wav": _named_file_receipt(
            "assets/audio.wav",
            reader(paths.assets / "audio.wav", MAX_AUDIO_INPUT_BYTES),
        ),
    }
    artifacts = {
        "audio_intelligence": _named_file_receipt(
            "data/audio_intelligence.json",
            file_receipt_from_bytes(dataset_bytes),
        )
    }
    core = {
        "schema_version": AUDIO_INTELLIGENCE_RECEIPT_SCHEMA_VERSION,
        "dataset_schema": AUDIO_TIMELINE_SCHEMA_ID,
        "state": "committed",
        "digest_algorithm": AUDIO_INTELLIGENCE_DIGEST_ALGORITHM,
        "parameters": parameters,
        "capabilities": dataset["capabilities"],
        "inputs": inputs,
        "artifacts": artifacts,
    }
    return {
        **core,
        "generation_id": hashlib.sha256(canonical_json_bytes(core)).hexdigest(),
    }


def validate_receipt_structure(receipt: Any) -> dict[str, Any]:
    if type(receipt) is not dict or set(receipt) != RECEIPT_KEYS:
        raise ValueError("audio intelligence receipt fields are invalid")
    if (
        type(receipt.get("schema_version")) is not int
        or receipt["schema_version"] != AUDIO_INTELLIGENCE_RECEIPT_SCHEMA_VERSION
    ):
        raise ValueError("audio intelligence receipt schema version is unsupported")
    if receipt.get("dataset_schema") != AUDIO_TIMELINE_SCHEMA_ID:
        raise ValueError("audio intelligence dataset schema is unsupported")
    if receipt.get("state") != "committed":
        raise ValueError("audio intelligence receipt is not committed")
    if receipt.get("digest_algorithm") != AUDIO_INTELLIGENCE_DIGEST_ALGORITHM:
        raise ValueError("audio intelligence receipt digest algorithm is unsupported")
    parameters = validate_parameters(receipt.get("parameters"))
    capabilities = validate_capabilities(receipt.get("capabilities"))
    inputs = receipt.get("inputs")
    if type(inputs) is not dict or set(inputs) != INPUT_KEYS:
        raise ValueError("audio intelligence receipt inputs are invalid")
    audio_binding = inputs.get("audio_generation")
    if (
        type(audio_binding) is not dict
        or set(audio_binding) != AUDIO_GENERATION_BINDING_KEYS
    ):
        raise ValueError(
            "audio intelligence audio-generation binding fields are invalid"
        )
    if (
        type(audio_binding.get("schema_version")) is not int
        or audio_binding["schema_version"] != 1
    ):
        raise ValueError("audio intelligence audio-generation schema is unsupported")
    validate_sha256(audio_binding.get("generation_id"), "audio generation id")
    validate_sha256(
        audio_binding.get("receipt_sha256"), "audio generation receipt digest"
    )
    media_receipt = _validate_named_file_receipt(
        inputs.get("media_package"), expected_path="data/media_package.json"
    )
    wav_receipt = _validate_named_file_receipt(
        inputs.get("audio_wav"), expected_path="assets/audio.wav"
    )
    artifacts = receipt.get("artifacts")
    if type(artifacts) is not dict or set(artifacts) != ARTIFACT_KEYS:
        raise ValueError("audio intelligence receipt artifacts are invalid")
    dataset_receipt = _validate_named_file_receipt(
        artifacts.get("audio_intelligence"),
        expected_path="data/audio_intelligence.json",
    )
    core = {
        "schema_version": AUDIO_INTELLIGENCE_RECEIPT_SCHEMA_VERSION,
        "dataset_schema": AUDIO_TIMELINE_SCHEMA_ID,
        "state": "committed",
        "digest_algorithm": AUDIO_INTELLIGENCE_DIGEST_ALGORITHM,
        "parameters": parameters,
        "capabilities": capabilities,
        "inputs": {
            "audio_generation": {
                "schema_version": 1,
                "generation_id": audio_binding["generation_id"],
                "receipt_sha256": audio_binding["receipt_sha256"],
            },
            "media_package": media_receipt,
            "audio_wav": wav_receipt,
        },
        "artifacts": {"audio_intelligence": dataset_receipt},
    }
    expected_generation_id = hashlib.sha256(canonical_json_bytes(core)).hexdigest()
    if receipt.get("generation_id") != expected_generation_id:
        raise ValueError("audio intelligence generation id is invalid")
    return {**core, "generation_id": expected_generation_id}


def _validate_media_binding(
    paths: ProjectPaths,
    dataset: dict[str, Any],
    *,
    media: CanonicalMediaPackage,
) -> None:
    if media.project_id != paths.root.name:
        raise ValueError("audio intelligence media package project binding is invalid")
    if not math.isclose(
        float(media.duration_seconds),
        float(dataset["media_duration_seconds"]),
        rel_tol=0.0,
        abs_tol=1e-6,
    ):
        raise ValueError("audio timeline duration does not match the media package")
    expected_audio = Path(os.path.abspath(os.fspath(paths.assets / "audio.wav")))
    declared_audio = Path(os.path.abspath(os.fspath(media.audio_path)))
    if declared_audio != expected_audio:
        raise ValueError("audio intelligence media package audio path is non-canonical")


def _named_file_receipt(relative_path: str, receipt: dict[str, Any]) -> dict[str, Any]:
    return _validate_named_file_receipt(
        {
            "path": relative_path,
            "kind": "file",
            "digest_mode": AUDIO_INTELLIGENCE_FILE_DIGEST_MODE,
            "sha256": receipt.get("sha256"),
            "size_bytes": receipt.get("size_bytes"),
        },
        expected_path=relative_path,
    )


def _validate_named_file_receipt(value: Any, *, expected_path: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != FILE_RECEIPT_KEYS:
        raise ValueError(
            f"audio intelligence file receipt fields are invalid: {expected_path}"
        )
    if (
        value.get("path") != expected_path
        or value.get("kind") != "file"
        or value.get("digest_mode") != AUDIO_INTELLIGENCE_FILE_DIGEST_MODE
    ):
        raise ValueError(
            f"audio intelligence file receipt contract is invalid: {expected_path}"
        )
    digest = value.get("sha256")
    validate_sha256(digest, f"audio intelligence file digest {expected_path}")
    size = value.get("size_bytes")
    if type(size) is not int or size < 0:
        raise ValueError(f"audio intelligence file size is invalid: {expected_path}")
    return {
        "path": expected_path,
        "kind": "file",
        "digest_mode": AUDIO_INTELLIGENCE_FILE_DIGEST_MODE,
        "sha256": digest,
        "size_bytes": size,
    }


def _read_data_file(
    paths: ProjectPaths,
    name: str,
    maximum: int,
    *,
    data_fd: int | None,
) -> tuple[bytes, dict[str, Any]]:
    if data_fd is not None:
        return read_relative_file_bytes_and_receipt(data_fd, name, maximum)
    return read_file_bytes_and_receipt(paths.data / name, maximum)


def _parse_media_package(payload: bytes) -> CanonicalMediaPackage:
    return CanonicalMediaPackage.model_validate(strict_json_loads(payload))
