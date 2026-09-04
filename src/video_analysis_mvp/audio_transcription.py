"""Local-only Whisper adapter with bounded, provenance-aware inputs and outputs."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._audio_intelligence_metadata import bounded_text
from .media import _open_regular_no_symlinks
from .schemas import TranscriptSegment
from .utils import ToolError, run_command

MAX_MODEL_BYTES = 4 * 1024 * 1024 * 1024
MAX_OUTPUT_BYTES = 4 * 1024 * 1024
MAX_SEGMENTS = 10_000
MAX_TOTAL_TEXT_CHARS = 1_000_000
_LANGUAGE_RE = re.compile(r"[A-Za-z]{2,3}")


@dataclass(frozen=True)
class TranscriptionResult:
    status: str
    reason: str | None
    segments: list[TranscriptSegment]
    model_sha256: str | None = None


@dataclass(frozen=True)
class _FileBinding:
    device: int
    inode: int
    size: int
    sha256: str


class _UnsafeFile(ValueError):
    pass


class _InvalidOutput(ValueError):
    pass


def transcribe_local(
    audio_path: Path,
    *,
    duration: float,
    language: str = "auto",
    model_path: str | None = None,
    skip: bool = False,
) -> TranscriptionResult:
    """Run only an explicitly supplied local checkpoint; never download a model."""
    if skip:
        return TranscriptionResult("skipped", "ASR was skipped", [])
    if not model_path:
        return TranscriptionResult("unknown", "local ASR model was not configured", [])
    if not _valid_duration(duration) or not _valid_requested_language(language):
        return TranscriptionResult("failed", "local ASR configuration is invalid", [])

    # Do not inspect a potentially sensitive local model path when there is no
    # installed executable capable of consuming it.
    whisper = shutil.which("whisper")
    if not whisper:
        return TranscriptionResult(
            "unknown", "local whisper executable is unavailable", []
        )

    model = Path(model_path)
    # Known model names may download. Parent traversal is also forbidden:
    # lexical normalization in safe-open must not differ from CLI path lookup.
    if not model.is_absolute() or ".." in model.parts:
        return TranscriptionResult("failed", "local ASR model is unsafe", [])
    try:
        before = _model_binding(model)
    except _UnsafeFile:
        return TranscriptionResult("failed", "local ASR model is unsafe", [])

    with tempfile.TemporaryDirectory(prefix="video-analysis-asr-") as directory:
        output_dir = Path(directory)
        environment = _minimal_environment(output_dir)
        args = [
            whisper,
            str(audio_path),
            "--model",
            str(model),
            "--task",
            "transcribe",
            "--output_dir",
            str(output_dir),
            "--output_format",
            "json",
            "--device",
            "cpu",
            "--fp16",
            "False",
            "--verbose",
            "False",
            "--threads",
            "4",
        ]
        if language != "auto":
            args.extend(["--language", language.lower()])
        try:
            run_command(args, timeout=300, environment=environment)
        except (ToolError, OSError, ValueError):
            return TranscriptionResult(
                "failed", "local whisper execution failed", [], before.sha256
            )

        # This catches observable model replacement or mutation. It cannot
        # prove that a hostile OS actor did not swap and restore a pathname
        # between checks, so the digest is not an OS-level loading guarantee.
        try:
            after = _model_binding(model)
        except _UnsafeFile:
            return TranscriptionResult(
                "failed", "local ASR model changed during transcription", []
            )
        if after != before:
            return TranscriptionResult(
                "failed", "local ASR model changed during transcription", []
            )

        expected_output = output_dir / f"{audio_path.stem}.json"
        try:
            raw_output = _read_regular_bytes(expected_output, MAX_OUTPUT_BYTES)
            payload = _strict_json_object(raw_output)
            segments = _parse_segments(
                payload, duration=duration, requested_language=language
            )
        except (ValueError, RecursionError, OSError, OverflowError):
            return TranscriptionResult(
                "failed", "local whisper output was invalid", [], before.sha256
            )

    return TranscriptionResult("produced", None, segments, before.sha256)


def _minimal_environment(tempdir: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "HOME": str(tempdir),
        "TMPDIR": str(tempdir),
        "XDG_CACHE_HOME": str(tempdir),
    }


def _valid_duration(value: float) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def _valid_requested_language(value: str) -> bool:
    return isinstance(value, str) and (
        value == "auto" or _LANGUAGE_RE.fullmatch(value) is not None
    )


def _model_binding(path: Path) -> _FileBinding:
    return _file_binding(path, MAX_MODEL_BYTES, include_payload=False)


def _read_regular_bytes(path: Path, max_bytes: int) -> bytes:
    result = _file_binding(path, max_bytes, include_payload=True)
    assert isinstance(result, tuple)
    _binding, payload = result
    return payload


def _file_binding(
    path: Path, max_bytes: int, *, include_payload: bool
) -> _FileBinding | tuple[_FileBinding, bytes]:
    try:
        with _open_regular_no_symlinks(path) as descriptor:
            opened = os.fstat(descriptor)
            if opened.st_size < 0 or opened.st_size > max_bytes:
                raise _UnsafeFile
            snapshot = _stat_identity(opened)
            digest = hashlib.sha256()
            chunks: list[bytes] = []
            total = 0
            while total <= max_bytes:
                chunk = os.read(descriptor, min(1024 * 1024, max_bytes + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise _UnsafeFile
                digest.update(chunk)
                if include_payload:
                    chunks.append(chunk)
            current = os.fstat(descriptor)
            if total != opened.st_size or _stat_identity(current) != snapshot:
                raise _UnsafeFile
            binding = _FileBinding(
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                digest.hexdigest(),
            )
    except (OSError, ValueError) as exc:
        raise _UnsafeFile from exc
    if include_payload:
        return binding, b"".join(chunks)
    return binding


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _strict_json_object(payload: bytes) -> dict[str, Any]:
    if len(payload) > MAX_OUTPUT_BYTES:
        raise _InvalidOutput

    def reject_constant(value: str) -> None:
        raise _InvalidOutput(f"invalid JSON constant: {value}")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise _InvalidOutput("duplicate JSON key")
            result[key] = value
        return result

    decoded = payload.decode("utf-8", errors="strict")
    result = json.loads(
        decoded,
        parse_constant=reject_constant,
        object_pairs_hook=reject_duplicates,
    )
    if type(result) is not dict:
        raise _InvalidOutput("JSON root is not an object")
    return result


def _parse_segments(
    payload: dict[str, Any], *, duration: float, requested_language: str
) -> list[TranscriptSegment]:
    raw_segments = payload.get("segments")
    if type(raw_segments) is not list or len(raw_segments) > MAX_SEGMENTS:
        raise _InvalidOutput("segments are invalid")
    if requested_language == "auto":
        actual_language = payload.get("language", "unknown")
        if actual_language != "unknown" and (
            type(actual_language) is not str
            or _LANGUAGE_RE.fullmatch(actual_language) is None
        ):
            raise _InvalidOutput("language is invalid")
        language = (
            actual_language.lower() if isinstance(actual_language, str) else "unknown"
        )
    else:
        language = requested_language.lower()

    result: list[TranscriptSegment] = []
    total_text_chars = 0
    previous_start = -1.0
    for index, raw in enumerate(raw_segments, start=1):
        if type(raw) is not dict:
            raise _InvalidOutput("segment is not an object")
        start = raw.get("start")
        end = raw.get("end")
        text = raw.get("text")
        if (
            not _valid_time(start)
            or not _valid_time(end)
            or start < 0
            or end < start
            or end > duration
            or type(text) is not str
        ):
            raise _InvalidOutput("segment bounds are invalid")
        if start < previous_start:
            raise _InvalidOutput("segments are not ordered")
        previous_start = start
        text = text.strip()
        # Whisper can emit a zero-duration whitespace timing marker. It is not
        # a transcript line and must not make an otherwise empty result appear
        # to contain speech.
        if not text:
            continue
        if end <= start:
            raise _InvalidOutput("segment text is invalid")
        # Use the timeline's exact UTF-8/NUL contract before declaring ASR
        # produced, so malformed model text cannot abort baseline publication.
        text = bounded_text(text, "ASR segment text")
        total_text_chars += len(text)
        if total_text_chars > MAX_TOTAL_TEXT_CHARS:
            raise _InvalidOutput("transcript text is too large")
        result.append(
            TranscriptSegment(
                segment_id=f"tr_{index:04d}",
                start_time=float(start),
                end_time=float(end),
                text=text,
                language=language,
                speaker="unknown",
                confidence=0.55,
            )
        )
    return result


def _valid_time(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


__all__ = ["TranscriptionResult", "transcribe_local"]
