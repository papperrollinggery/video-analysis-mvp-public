"""Bounded, network-free PCM measurements. Energy is not sound identity."""

from __future__ import annotations

import hashlib
import itertools
import math
import os
import statistics
import struct
import tempfile
import time
import wave
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._audio_intelligence_schema import CAPABILITIES, validate_audio_timeline
from .media import _open_regular_no_symlinks

MAX_PCM_BYTES = 256 * 1024 * 1024
MAX_PCM_SAMPLES = 120_000_000
MAX_DURATION_SECONDS = 3600.0
MAX_MEASUREMENT_SECONDS = 30.0
WINDOW_SECONDS = 0.02
SILENCE_RMS = 10 ** (-50 / 20)
MIN_SILENCE_SECONDS = 0.1


@dataclass(frozen=True)
class AudioWindow:
    start: float
    end: float
    rms: float
    peak: float


@dataclass(frozen=True)
class AudioOnset:
    time: float
    strength: float


@dataclass(frozen=True)
class AudioFeatures:
    duration: float
    sample_rate: int
    channels: int
    sample_width: int
    rms: float
    windows: tuple[AudioWindow, ...]
    onsets: tuple[AudioOnset, ...]
    silence_ranges: tuple[tuple[float, float], ...]
    estimated_bpm: float | None
    tempo_confidence: float
    input_sha256: str
    input_size_bytes: int


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


@contextmanager
def snapshot_audio(path: Path) -> Iterator[Path]:
    """Give every analyzer the same private bytes, never a moving source path."""
    with tempfile.TemporaryDirectory(prefix="vew-audio-snapshot-") as directory:
        target = Path(directory) / "audio.wav"
        with _open_regular_no_symlinks(path) as source_fd:
            before = os.fstat(source_fd)
            if not 0 < before.st_size <= MAX_PCM_BYTES:
                raise ValueError("PCM input byte limit exceeded or input is empty")
            copied = 0
            with target.open("xb") as output:
                while chunk := os.read(source_fd, 1024 * 1024):
                    copied += len(chunk)
                    if copied > MAX_PCM_BYTES:
                        raise ValueError("PCM input byte limit exceeded")
                    output.write(chunk)
            if copied != before.st_size or _identity(before) != _identity(
                os.fstat(source_fd)
            ):
                raise ValueError("Audio input changed while creating snapshot")
        yield target


def measure_audio(
    path: Path, *, max_duration_seconds: float = MAX_DURATION_SECONDS
) -> AudioFeatures:
    if (
        not math.isfinite(max_duration_seconds)
        or not 0 < max_duration_seconds <= MAX_DURATION_SECONDS
    ):
        raise ValueError("PCM duration limit is invalid")
    with _open_regular_no_symlinks(path) as descriptor:
        before = os.fstat(descriptor)
        if not 0 < before.st_size <= MAX_PCM_BYTES:
            raise ValueError("PCM input byte limit exceeded or input is empty")
        with os.fdopen(os.dup(descriptor), "rb") as handle:
            try:
                with wave.open(handle, "rb") as reader:
                    result = _measure_pcm(reader, max_duration_seconds)
            except (wave.Error, EOFError, struct.error) as exc:
                raise ValueError("Invalid or unsupported PCM WAV input") from exc
            handle.seek(0)
            digest = hashlib.sha256()
            size = 0
            while chunk := handle.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_PCM_BYTES:
                    raise ValueError("PCM input byte limit exceeded")
                digest.update(chunk)
        if size != before.st_size or _identity(before) != _identity(
            os.fstat(descriptor)
        ):
            raise ValueError("Audio input changed during measurement")
    return AudioFeatures(
        **result, input_sha256=digest.hexdigest(), input_size_bytes=size
    )


def _measure_pcm(reader: wave.Wave_read, duration_limit: float) -> dict[str, Any]:
    channels, width, rate, frame_count, compression, _ = reader.getparams()
    if (
        compression != "NONE"
        or width not in {1, 2, 3, 4}
        or not 1 <= channels <= 8
        or not 8000 <= rate <= 192000
    ):
        raise ValueError(
            "Unsupported PCM format; expected 8-192 kHz, 1-8 channels, 8/16/24/32-bit integer PCM"
        )
    duration = frame_count / rate
    if (
        frame_count <= 0
        or duration > duration_limit
        or frame_count * channels > MAX_PCM_SAMPLES
    ):
        raise ValueError("PCM duration or sample limit exceeded, or input is empty")
    chunk_frames = max(1, round(rate * WINDOW_SECONDS))
    windows: list[AudioWindow] = []
    total_squares = 0
    position = 0
    scale = 2 ** (width * 8 - 1)
    deadline = time.monotonic() + MAX_MEASUREMENT_SECONDS
    while position < frame_count:
        if time.monotonic() > deadline:
            raise ValueError("PCM measurement time limit exceeded")
        count = min(chunk_frames, frame_count - position)
        raw = reader.readframes(count)
        if len(raw) != count * channels * width:
            raise ValueError("PCM sample data is truncated")
        if width == 1:
            values = [value - 128 for value in raw]
        elif width == 3:
            values = [
                int.from_bytes(raw[offset : offset + 3], "little", signed=True)
                for offset in range(0, len(raw), 3)
            ]
        else:
            values = [
                item[0]
                for item in struct.iter_unpack("<h" if width == 2 else "<i", raw)
            ]
        squares = sum(value * value for value in values)
        total_squares += squares
        windows.append(
            AudioWindow(
                position / rate,
                (position + count) / rate,
                math.sqrt(squares / len(values)) / scale,
                max(abs(value) for value in values) / scale,
            )
        )
        position += count
    onsets = _onsets(windows)
    bpm, confidence = _pulse_tempo(onsets)
    return {
        "duration": duration,
        "sample_rate": rate,
        "channels": channels,
        "sample_width": width,
        "rms": math.sqrt(total_squares / (frame_count * channels)) / scale,
        "windows": tuple(windows),
        "onsets": tuple(onsets),
        "silence_ranges": _silence(windows),
        "estimated_bpm": bpm,
        "tempo_confidence": confidence,
    }


def _onsets(windows: list[AudioWindow]) -> list[AudioOnset]:
    found: list[AudioOnset] = []
    previous = 0.0
    for window in windows:
        rise = window.rms - previous
        if (
            window.rms >= max(0.02, previous * 2)
            and rise >= 0.015
            and (not found or window.start - found[-1].time >= 0.12)
        ):
            found.append(AudioOnset(window.start, min(1.0, rise)))
        previous = window.rms
    return found


def _pulse_tempo(onsets: list[AudioOnset]) -> tuple[float | None, float]:
    if len(onsets) < 5:
        return None, 0.0
    intervals = [right.time - left.time for left, right in itertools.pairwise(onsets)]
    median = statistics.median(intervals)
    deviations = [abs(value - median) / median for value in intervals]
    bpm = 60 / median
    if (
        not 20 <= bpm <= 300
        or statistics.median(deviations) > 0.08
        or max(deviations) > 0.2
    ):
        return None, 0.0
    # Regularity cannot identify musical meter or exclude repetitive speech/SFX.
    return round(bpm, 2), round(max(0.3, 0.65 - statistics.mean(deviations)), 3)


def _silence(windows: list[AudioWindow]) -> tuple[tuple[float, float], ...]:
    ranges: list[tuple[float, float]] = []
    start: float | None = None
    for window in windows:
        if window.rms <= SILENCE_RMS and start is None:
            start = window.start
        if window.rms > SILENCE_RMS and start is not None:
            if window.start - start >= MIN_SILENCE_SECONDS - 1e-9:
                ranges.append((start, window.start))
            start = None
    if start is not None and windows[-1].end - start >= MIN_SILENCE_SECONDS - 1e-9:
        ranges.append((start, windows[-1].end))
    return tuple(ranges)


def audio_proposal(
    *,
    label: str = "",
    energy: float | None = None,
    onset_density: float | None = None,
    estimated_bpm: float | None = None,
    confidence: float = 1.0,
    verification: str = "measured",
) -> dict[str, Any]:
    return {
        "label": label,
        "text": "",
        "language": "unknown",
        "speaker_id": None,
        "voice_role": "unknown",
        "energy": energy,
        "onset_density": onset_density,
        "estimated_bpm": estimated_bpm,
        "confidence": confidence,
        "verification": verification,
    }


def baseline_timeline(features: AudioFeatures, duration: float) -> dict[str, Any]:
    if (
        not math.isfinite(duration)
        or duration <= 0
        or abs(features.duration - duration) > 0.1
    ):
        raise ValueError("PCM and media duration mismatch exceeds 100 ms tolerance")
    events: list[dict[str, Any]] = []

    def add(
        identifier: str, start: float, end: float, kind: str, proposal: dict[str, Any]
    ) -> None:
        end = min(end, duration)
        if start < end:
            events.append(
                {
                    "event_id": identifier,
                    "start_time": start,
                    "end_time": end,
                    "kind": kind,
                    "source_id": "pcm-baseline-1",
                    "proposal": proposal,
                    "review": None,
                }
            )

    for index in range(0, len(features.windows), 25):
        group = features.windows[index : index + 25]
        length = group[-1].end - group[0].start
        energy = math.sqrt(
            sum(window.rms**2 * (window.end - window.start) for window in group)
            / length
        )
        add(
            f"energy-{index // 25:06d}",
            group[0].start,
            group[-1].end,
            "mixed",
            audio_proposal(label="PCM RMS; audio identity unclassified", energy=energy),
        )
    for index, (start, end) in enumerate(features.silence_ranges):
        add(
            f"silence-{index:06d}",
            start,
            end,
            "silence",
            audio_proposal(label="Below -50 dBFS RMS threshold for at least 100 ms"),
        )
    for index, onset in enumerate(features.onsets):
        add(
            f"onset-{index:06d}",
            onset.time,
            onset.time + WINDOW_SECONDS,
            "mixed",
            audio_proposal(
                label="Energy-onset candidate; sound identity unknown",
                confidence=0.45,
                verification="machine_estimated",
            ),
        )
    if features.estimated_bpm is not None:
        start, end = (
            features.onsets[0].time,
            min(features.duration, features.onsets[-1].time + WINDOW_SECONDS),
        )
        add(
            "pulse-tempo-000000",
            start,
            end,
            "mixed",
            audio_proposal(
                label="Regular energy-pulse tempo estimate; musical meter unknown",
                onset_density=len(features.onsets) / (end - start),
                estimated_bpm=features.estimated_bpm,
                confidence=features.tempo_confidence,
                verification="machine_estimated",
            ),
        )
    dataset = {
        "schema_id": "audio-timeline/v1",
        "time_range_semantics": "[start,end)",
        "media_duration_seconds": duration,
        "sources": [
            {
                "source_id": "pcm-baseline-1",
                "capability": "baseline_features",
                "source_type": "deterministic_detector",
                "adapter": "vew.pcm_baseline",
                "adapter_version": "1",
                "engine": "python-stdlib-pcm",
                "engine_version": None,
                "model": None,
                "device": "cpu",
                "status": "produced",
                "diagnostics": [],
            }
        ],
        "capabilities": {
            name: {
                "status": "unknown",
                "source_id": None,
                "reason": "adapter not configured",
            }
            for name in sorted(CAPABILITIES)
        },
        "events": sorted(
            events,
            key=lambda event: (
                event["start_time"],
                event["end_time"],
                event["event_id"],
            ),
        ),
    }
    dataset["capabilities"]["baseline_features"] = {
        "status": "produced",
        "source_id": "pcm-baseline-1",
        "reason": None,
    }
    return validate_audio_timeline(dataset)
