from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any, Callable

from .audio_features import AudioFeatures, audio_proposal, baseline_timeline, measure_audio, snapshot_audio
from .audio_transcription import TranscriptionResult, transcribe_local
from .paths import ProjectPaths
from .safe_io import (
    advisory_file_lock,
    atomic_write_text,
    ensure_output_directory,
    read_regular_bytes,
)
from .schemas import (
    AnalysisProfile,
    BeatEvent,
    CanonicalMediaPackage,
    MusicProfile,
    TranscriptSegment,
    dump_json,
    load_json,
)
from .utils import format_time


AUDIO_GENERATION_SCHEMA_VERSION = 1
AUDIO_GENERATION_DIGEST_ALGORITHM = "sha256"
AUDIO_GENERATION_FILE_DIGEST_MODE = "sha256-file-v1"
MAX_AUDIO_ARTIFACT_BYTES = 64 * 1024 * 1024
AUDIO_ARTIFACT_RELATIVE_PATHS = (
    "data/transcript.json",
    "data/beats.json",
    "data/music_profile.json",
    "reports/transcript.srt",
    "reports/music_rhythm_summary.json",
)
FileReceiptReader = Callable[[Path, int], dict[str, Any]]


def analyze_audio(
    media: CanonicalMediaPackage,
    paths: ProjectPaths,
    language: str = "auto",
    skip_asr: bool = False,
    asr_model: str | None = None,
) -> tuple[list[TranscriptSegment], list[BeatEvent], list[MusicProfile]]:
    from .audio_intelligence import stage_and_commit_audio_intelligence, validate_audio_timeline
    from ._audio_intelligence_storage import file_receipt
    from .media import verify_media_generation

    if not isinstance(language, str) or (language != "auto" and re.fullmatch(r"[A-Za-z]{2,3}", language) is None):
        raise ValueError("Audio language must be auto or a two/three-letter language code")
    if media.audio_path == "":
        from .store import load_media

        if load_media(paths).audio_path != "":
            raise ValueError("No-audio request does not match the current media package")
        valid, reasons = verify_media_generation(paths)
        if not valid:
            raise ValueError("No-audio media verification failed: " + "; ".join(reasons))
        timeline_paths = (
            paths.data / "audio_intelligence.json",
            paths.data / "audio_intelligence_generation.json",
        )
        if any(os.path.lexists(path) for path in timeline_paths):
            raise ValueError("Audio timeline conflicts with verified no-audio media")
        with advisory_file_lock(paths.data / ".shots.lock", root=paths.root):
            _ensure_no_human_audio_decisions(paths)
            _stage_and_commit_audio_generation(paths, [], [], [])
        return [], [], []

    audio_path = Path(media.audio_path)
    if os.path.abspath(audio_path) != os.path.abspath(paths.assets / "audio.wav"):
        raise ValueError("Audio analysis requires the canonical project audio WAV")
    with snapshot_audio(audio_path) as snapshot:
        features = measure_audio(snapshot)
        dataset = baseline_timeline(features, media.duration_seconds)
        asr = transcribe_local(snapshot, duration=min(features.duration, media.duration_seconds),
                               language=language, model_path=asr_model, skip=skip_asr)
        _add_transcription(dataset, asr)
        dataset = validate_audio_timeline(dataset)
        transcript = asr.segments
        beats = detect_beats(snapshot, features=features)
        beats = [beat for beat in beats if beat.time < media.duration_seconds]
        music = profile_music(media.duration_seconds, beats, snapshot, media.analysis_profile, features=features)
        expected_wav = {"sha256": features.input_sha256, "size_bytes": features.input_size_bytes}
        with advisory_file_lock(paths.data / ".shots.lock", root=paths.root):
            _ensure_no_human_audio_decisions(paths)
            current_wav = file_receipt(audio_path, features.input_size_bytes)
            if any(current_wav[key] != value for key, value in expected_wav.items()):
                raise ValueError("Audio input changed during analysis")
            _stage_and_commit_audio_generation(paths, transcript, beats, music)
            stage_and_commit_audio_intelligence(
                paths, dataset, expected_audio_wav=expected_wav,
                parameters={"baseline_version": "1", "window_seconds": 0.02,
                            "silence_dbfs": -50, "minimum_silence_seconds": 0.1,
                            "asr_requested": not skip_asr, "language": language},
            )
    return transcript, beats, music


def _ensure_no_human_audio_decisions(paths: ProjectPaths) -> None:
    from ._audio_intelligence_storage import strict_json_loads
    from .audio_intelligence import validate_audio_timeline

    path = paths.data / "audio_intelligence.json"
    if not os.path.lexists(path):
        return
    dataset = validate_audio_timeline(strict_json_loads(read_regular_bytes(path, root=paths.root, max_bytes=MAX_AUDIO_ARTIFACT_BYTES)))
    if any(event["review"] is not None for event in dataset["events"]):
        raise ValueError("Audio rerun would discard human review decisions; preserve this project and analyze a new project revision")


def _add_transcription(dataset: dict[str, Any], result: TranscriptionResult) -> None:
    source_id = "local-asr-1"
    dataset["sources"].append({
        "source_id": source_id, "capability": "asr", "source_type": "adapter",
        "adapter": "vew.local_whisper", "adapter_version": "1", "engine": "whisper",
        "engine_version": None, "model": f"sha256:{result.model_sha256}" if result.model_sha256 else None,
        "device": "cpu", "status": result.status, "diagnostics": [result.reason] if result.reason else [],
    })
    selected = source_id if result.status in {"produced", "failed"} else None
    dataset["capabilities"]["asr"] = {"status": result.status, "source_id": selected, "reason": result.reason}
    for segment in result.segments:
        proposal = audio_proposal(confidence=segment.confidence, verification="machine_estimated")
        proposal.update(text=segment.text, language=segment.language)
        dataset["events"].append({
            "event_id": f"asr-{segment.segment_id}", "start_time": segment.start_time,
            "end_time": segment.end_time, "kind": "voice", "source_id": source_id,
            "proposal": proposal, "review": None,
        })
    dataset["events"].sort(key=lambda event: (event["start_time"], event["end_time"], event["event_id"]))


def verify_audio_analysis(paths: ProjectPaths) -> tuple[bool, list[str]]:
    """Verify a bound audio timeline, or empty records for a no-audio source."""
    from .audio_intelligence import audio_intelligence_binding
    from .media import verify_media_generation
    from .store import load_media

    valid, reasons = verify_audio_generation(paths)
    if not valid:
        return valid, reasons
    try:
        media = load_media(paths)
    except Exception as exc:
        return False, [f"media package is unreadable: {exc}"]
    if media.audio_path == "":
        media_valid, media_reasons = verify_media_generation(paths)
        if not media_valid:
            return False, media_reasons
        if any(
            os.path.lexists(path)
            for path in (
                paths.data / "audio_intelligence.json",
                paths.data / "audio_intelligence_generation.json",
            )
        ):
            return False, ["audio timeline conflicts with verified no-audio media"]
        if any(load_json(paths.data / name) != [] for name in ("transcript.json", "beats.json", "music_profile.json")):
            return False, ["non-empty audio records conflict with verified no-audio media"]
        return True, []
    audio_path = Path(media.audio_path)
    try:
        if audio_path.resolve(strict=True) != (paths.assets / "audio.wav").resolve() or not audio_path.is_file() or audio_path.stat().st_size <= 0:
            raise ValueError("canonical audio WAV is missing or invalid")
    except (OSError, ValueError):
        return False, ["canonical audio WAV is missing or invalid"]
    try:
        audio_intelligence_binding(paths)
    except ValueError as exc:
        return False, [str(exc)]
    return True, []


def _stage_and_commit_audio_generation(
    paths: ProjectPaths,
    transcript: list[TranscriptSegment],
    beats: list[BeatEvent],
    music: list[MusicProfile],
) -> None:
    """Stage and validate all audio outputs before publishing one generation."""
    ensure_output_directory(paths.data, root=paths.root)
    ensure_output_directory(paths.reports, root=paths.root)
    with tempfile.TemporaryDirectory(prefix=".audio-stage-", dir=paths.root) as directory:
        staged_paths = ProjectPaths(Path(directory))
        staged_paths.ensure()
        dump_json(staged_paths.data / "transcript.json", transcript)
        dump_json(staged_paths.data / "beats.json", beats)
        dump_json(staged_paths.data / "music_profile.json", music)
        write_srt(staged_paths.reports / "transcript.srt", transcript)
        dump_json(
            staged_paths.reports / "music_rhythm_summary.json",
            {"beats": beats, "music_profile": music},
        )
        _validate_audio_payloads(staged_paths, transcript, beats, music)
        dump_json(
            staged_paths.data / "audio_generation.json",
            _build_audio_generation_receipt(staged_paths),
        )
        audio_generation_binding(staged_paths)
        _commit_audio_generation(paths, staged_paths)


def _build_audio_generation_receipt(
    paths: ProjectPaths,
    *,
    file_receipt_reader: FileReceiptReader | None = None,
) -> dict[str, Any]:
    reader = file_receipt_reader or _default_file_receipt
    artifacts: dict[str, dict[str, Any]] = {}
    for relative in AUDIO_ARTIFACT_RELATIVE_PATHS:
        current = reader(paths.root / relative, MAX_AUDIO_ARTIFACT_BYTES)
        artifacts[relative] = {
            "path": relative,
            "kind": "file",
            "digest_mode": AUDIO_GENERATION_FILE_DIGEST_MODE,
            "sha256": current["sha256"],
            "size_bytes": current["size_bytes"],
        }
    generation_id = _audio_generation_id(artifacts)
    return {
        "schema_version": AUDIO_GENERATION_SCHEMA_VERSION,
        "generation_id": generation_id,
        "digest_algorithm": AUDIO_GENERATION_DIGEST_ALGORITHM,
        "artifacts": artifacts,
    }


def verify_audio_generation(paths: ProjectPaths) -> tuple[bool, list[str]]:
    try:
        audio_generation_binding(paths)
    except ValueError as exc:
        return False, [str(exc)]
    return True, []


def audio_generation_binding(
    paths: ProjectPaths,
    *,
    file_receipt_reader: FileReceiptReader | None = None,
) -> dict[str, Any]:
    """Validate the complete committed audio generation and return its binding."""
    try:
        receipt = load_json(paths.data / "audio_generation.json")
    except FileNotFoundError:
        raise ValueError("audio generation receipt is missing") from None
    except Exception as exc:
        raise ValueError(f"audio generation receipt is unreadable: {exc}") from None
    if type(receipt) is not dict:
        raise ValueError("audio generation receipt must be an object")
    if set(receipt) != {"schema_version", "generation_id", "digest_algorithm", "artifacts"}:
        raise ValueError("audio generation receipt fields are invalid")
    if receipt.get("schema_version") != AUDIO_GENERATION_SCHEMA_VERSION:
        raise ValueError("audio generation receipt schema version is unsupported")
    if receipt.get("digest_algorithm") != AUDIO_GENERATION_DIGEST_ALGORITHM:
        raise ValueError("audio generation digest algorithm is unsupported")
    artifacts = receipt.get("artifacts")
    if type(artifacts) is not dict or set(artifacts) != set(AUDIO_ARTIFACT_RELATIVE_PATHS):
        raise ValueError("audio generation artifact ids are invalid")
    reader = file_receipt_reader or _default_file_receipt
    for relative in AUDIO_ARTIFACT_RELATIVE_PATHS:
        stored = artifacts.get(relative)
        if type(stored) is not dict or set(stored) != {
            "path",
            "kind",
            "digest_mode",
            "sha256",
            "size_bytes",
        }:
            raise ValueError(f"audio artifact receipt fields are invalid: {relative}")
        if (
            stored.get("path") != relative
            or stored.get("kind") != "file"
            or stored.get("digest_mode") != AUDIO_GENERATION_FILE_DIGEST_MODE
            or type(stored.get("sha256")) is not str
            or re.fullmatch(r"[0-9a-f]{64}", stored["sha256"]) is None
            or isinstance(stored.get("size_bytes"), bool)
            or not isinstance(stored.get("size_bytes"), int)
            or stored["size_bytes"] < 0
        ):
            raise ValueError(f"audio artifact receipt is invalid: {relative}")
        try:
            current = reader(paths.root / relative, MAX_AUDIO_ARTIFACT_BYTES)
        except Exception as exc:
            raise ValueError(f"audio artifact is missing, unsafe, or unreadable: {relative} ({type(exc).__name__})") from None
        if stored["sha256"] != current.get("sha256") or stored["size_bytes"] != current.get("size_bytes"):
            raise ValueError(f"audio artifact digest mismatch: {relative}")
    if receipt.get("generation_id") != _audio_generation_id(artifacts):
        raise ValueError("audio generation id does not match its artifact receipts")
    _validate_current_audio_payloads(paths)
    return {
        "schema_version": AUDIO_GENERATION_SCHEMA_VERSION,
        "generation_id": receipt["generation_id"],
        "receipt_sha256": hashlib.sha256(_canonical_json_bytes(receipt)).hexdigest(),
    }


def _commit_audio_generation(paths: ProjectPaths, staged_paths: ProjectPaths) -> None:
    ensure_output_directory(paths.data, root=paths.root)
    ensure_output_directory(paths.reports, root=paths.root)
    staged_targets = [
        *((staged_paths.root / relative, paths.root / relative) for relative in AUDIO_ARTIFACT_RELATIVE_PATHS),
        # The commit marker is deliberately last. Missing or mismatched marker
        # bytes make an interrupted generation unusable.
        (staged_paths.data / "audio_generation.json", paths.data / "audio_generation.json"),
    ]
    with advisory_file_lock(paths.data / ".shots.lock", root=paths.root):
        existing = [destination for _staged, destination in staged_targets if os.path.lexists(destination)]
        for candidate in existing:
            if not stat.S_ISREG(candidate.lstat().st_mode):
                raise ValueError(f"Managed audio generation target is unsafe: {candidate}")

        rollback_root = staged_paths.root / "rollback"
        rollback_root.mkdir(mode=0o700)
        moved_old: list[tuple[Path, Path]] = []
        committed: list[Path] = []
        try:
            for current in existing:
                backup = rollback_root / current.relative_to(paths.root)
                backup.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.replace(current, backup)
                moved_old.append((current, backup))
            for staged, destination in staged_targets:
                os.replace(staged, destination)
                committed.append(destination)
            _fsync_audio_generation_directories(paths)
        except Exception:
            for destination in reversed(committed):
                try:
                    destination.unlink()
                except FileNotFoundError:
                    pass
            for original, backup in reversed(moved_old):
                if os.path.lexists(backup):
                    os.replace(backup, original)
            _fsync_audio_generation_directories(paths)
            raise


def _validate_audio_payloads(
    paths: ProjectPaths,
    transcript: list[TranscriptSegment],
    beats: list[BeatEvent],
    music: list[MusicProfile],
) -> None:
    expected_transcript = [item.model_dump(mode="json") for item in transcript]
    expected_beats = [item.model_dump(mode="json") for item in beats]
    expected_music = [item.model_dump(mode="json") for item in music]
    if load_json(paths.data / "transcript.json") != expected_transcript:
        raise ValueError("Staged transcript payload does not match generated transcript")
    if load_json(paths.data / "beats.json") != expected_beats:
        raise ValueError("Staged beat payload does not match generated beats")
    if load_json(paths.data / "music_profile.json") != expected_music:
        raise ValueError("Staged music payload does not match generated profile")
    if load_json(paths.reports / "music_rhythm_summary.json") != {
        "beats": expected_beats,
        "music_profile": expected_music,
    }:
        raise ValueError("Staged rhythm summary does not match generated audio data")
    srt = read_regular_bytes(paths.reports / "transcript.srt", root=paths.root, max_bytes=MAX_AUDIO_ARTIFACT_BYTES)
    if srt.decode("utf-8") != _srt_text(transcript):
        raise ValueError("Staged transcript SRT does not match generated transcript")


def _validate_current_audio_payloads(paths: ProjectPaths) -> None:
    try:
        raw_transcript = load_json(paths.data / "transcript.json")
        raw_beats = load_json(paths.data / "beats.json")
        raw_music = load_json(paths.data / "music_profile.json")
        if type(raw_transcript) is not list or type(raw_beats) is not list or type(raw_music) is not list:
            raise ValueError("audio JSON artifacts must be arrays")
        transcript = [TranscriptSegment.model_validate(item) for item in raw_transcript]
        beats = [BeatEvent.model_validate(item) for item in raw_beats]
        music = [MusicProfile.model_validate(item) for item in raw_music]
        _validate_audio_payloads(paths, transcript, beats, music)
    except Exception as exc:
        raise ValueError(f"audio generation payload validation failed: {exc}") from None


def _default_file_receipt(path: Path, max_bytes: int) -> dict[str, Any]:
    payload = read_regular_bytes(path, max_bytes=max_bytes)
    return {"sha256": hashlib.sha256(payload).hexdigest(), "size_bytes": len(payload)}


def _audio_generation_id(artifacts: dict[str, Any]) -> str:
    return hashlib.sha256(
        _canonical_json_bytes(
            {
                "digest_algorithm": AUDIO_GENERATION_DIGEST_ALGORITHM,
                "artifacts": artifacts,
            }
        )
    ).hexdigest()


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _fsync_audio_generation_directories(paths: ProjectPaths) -> None:
    if os.name != "posix":  # pragma: no cover - exercised by Windows CI
        return
    for directory in (paths.data, paths.reports):
        descriptor = os.open(
            directory,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def transcribe_audio(audio_path: Path, paths: ProjectPaths, language: str, *, model_path: str | None = None) -> list[TranscriptSegment]:
    """Compatibility entry point; callers needing capability state use transcribe_local."""
    features = measure_audio(audio_path)
    return transcribe_local(audio_path, duration=features.duration, language=language, model_path=model_path).segments


def detect_beats(audio_path: Path, *, features: AudioFeatures | None = None) -> list[BeatEvent]:
    """Legacy name retained; these are onset candidates, not verified musical beats."""
    features = features or measure_audio(audio_path)
    return [BeatEvent(time=onset.time, strength=onset.strength, source="pcm_energy_onset_candidate", confidence=0.45)
            for onset in features.onsets]


def profile_music(
    duration: float,
    beats: list[BeatEvent],
    audio_path: Path,
    profile: AnalysisProfile | str = AnalysisProfile.research,
    *,
    features: AudioFeatures | None = None,
) -> list[MusicProfile]:
    features = features or measure_audio(audio_path)
    avg_energy = features.rms
    energy_level = "high" if avg_energy > 0.08 else "medium" if avg_energy > 0.035 else "low"
    bpm = features.estimated_bpm
    tempo_bucket = "unknown" if bpm is None else "fast" if bpm > 120 else "medium" if bpm > 75 else "slow"
    return [
        MusicProfile(
            start_time=0.0,
            end_time=round(duration, 3),
            energy_level=energy_level,
            tempo_bucket=tempo_bucket,
            style_tags=[],
            mood_tags=[],
            confidence=0.0,  # This compatibility summary does not establish music presence.
        )
    ]


def _style_tags(
    energy: str,
    tempo: str,
    profile: AnalysisProfile | str = AnalysisProfile.research,
) -> list[str]:
    profile_value = profile.value if isinstance(profile, AnalysisProfile) else profile
    if energy == "high" and tempo == "fast":
        tags = ["kinetic", "percussive"]
        if profile_value == AnalysisProfile.ads.value:
            tags.append("ad-friendly")
        elif profile_value == AnalysisProfile.shortform.value:
            tags.append("shortform-friendly")
        else:
            tags.append("high-energy")
        return tags
    if energy == "low" and tempo == "slow":
        return ["ambient", "minimal", "atmospheric"]
    return ["cinematic", "rhythmic"]


def _mood_tags(energy: str, tempo: str) -> list[str]:
    if energy == "high":
        return ["urgent", "driving"]
    if tempo == "slow":
        return ["reflective", "suspended"]
    return ["controlled", "forward-moving"]


def write_srt(path: Path, transcript: list[TranscriptSegment]) -> None:
    atomic_write_text(path, _srt_text(transcript))


def _srt_text(transcript: list[TranscriptSegment]) -> str:
    lines: list[str] = []
    for idx, segment in enumerate(transcript, start=1):
        lines.extend(
            [
                str(idx),
                f"{format_time(segment.start_time)} --> {format_time(segment.end_time)}",
                segment.text,
                "",
            ]
        )
    return "\n".join(lines)
