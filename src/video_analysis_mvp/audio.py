from __future__ import annotations

import json
import shutil
from pathlib import Path

from .paths import ProjectPaths
from .schemas import BeatEvent, CanonicalMediaPackage, MusicProfile, TranscriptSegment, dump_json
from .utils import format_time, read_wav_energy, run_command


def analyze_audio(
    media: CanonicalMediaPackage,
    paths: ProjectPaths,
    language: str = "auto",
    skip_asr: bool = False,
) -> tuple[list[TranscriptSegment], list[BeatEvent], list[MusicProfile]]:
    audio_path = Path(media.audio_path)
    transcript = [] if skip_asr else transcribe_audio(audio_path, paths, language)
    beats = detect_beats(audio_path)
    music = profile_music(media.duration_seconds, beats, audio_path)
    dump_json(paths.data / "transcript.json", transcript)
    dump_json(paths.data / "beats.json", beats)
    dump_json(paths.data / "music_profile.json", music)
    write_srt(paths.reports / "transcript.srt", transcript)
    dump_json(paths.reports / "music_rhythm_summary.json", {"beats": beats, "music_profile": music})
    return transcript, beats, music


def transcribe_audio(audio_path: Path, paths: ProjectPaths, language: str) -> list[TranscriptSegment]:
    if not shutil.which("whisper"):
        return []
    whisper_dir = paths.assets / "whisper"
    whisper_dir.mkdir(parents=True, exist_ok=True)
    args = [
        "whisper",
        str(audio_path),
        "--model",
        "turbo",
        "--task",
        "transcribe",
        "--output_dir",
        str(whisper_dir),
        "--output_format",
        "json",
    ]
    if language and language != "auto":
        args.extend(["--language", language])
    try:
        run_command(args, timeout=1200)
    except Exception:
        return []
    json_files = sorted(whisper_dir.glob("*.json"))
    if not json_files:
        return []
    data = json.loads(json_files[0].read_text(encoding="utf-8"))
    segments: list[TranscriptSegment] = []
    for idx, seg in enumerate(data.get("segments", []), start=1):
        text = str(seg.get("text") or "").strip()
        if not text:
            continue
        segments.append(
            TranscriptSegment(
                segment_id=f"tr_{idx:04d}",
                start_time=float(seg.get("start") or 0),
                end_time=float(seg.get("end") or 0),
                text=text,
                language=language if language != "auto" else str(data.get("language") or "unknown"),
                speaker="unknown",
                confidence=0.55,
            )
        )
    return segments


def detect_beats(audio_path: Path) -> list[BeatEvent]:
    energy = read_wav_energy(audio_path, window_seconds=0.25)
    if not energy:
        return []
    values = [value for _, value in energy]
    avg = sum(values) / len(values)
    threshold = max(avg * 1.65, 0.02)
    beats: list[BeatEvent] = []
    last_time = -1.0
    for idx in range(1, len(energy) - 1):
        time, value = energy[idx]
        if value <= threshold:
            continue
        if value < energy[idx - 1][1] or value < energy[idx + 1][1]:
            continue
        if last_time >= 0 and time - last_time < 0.45:
            continue
        strength = min(1.0, value / max(threshold, 0.001))
        beats.append(BeatEvent(time=round(time, 3), strength=round(strength, 3)))
        last_time = time
    return beats


def profile_music(duration: float, beats: list[BeatEvent], audio_path: Path) -> list[MusicProfile]:
    energy = read_wav_energy(audio_path, window_seconds=1.0)
    avg_energy = sum(value for _, value in energy) / max(len(energy), 1)
    beat_density = len(beats) / max(duration, 1.0) * 60
    energy_level = "high" if avg_energy > 0.08 else "medium" if avg_energy > 0.035 else "low"
    tempo_bucket = "fast" if beat_density > 90 else "medium" if beat_density > 45 else "slow"
    style_tags = _style_tags(energy_level, tempo_bucket)
    mood_tags = _mood_tags(energy_level, tempo_bucket)
    return [
        MusicProfile(
            start_time=0.0,
            end_time=round(duration, 3),
            energy_level=energy_level,
            tempo_bucket=tempo_bucket,
            style_tags=style_tags,
            mood_tags=mood_tags,
            confidence=0.32,
        )
    ]


def _style_tags(energy: str, tempo: str) -> list[str]:
    if energy == "high" and tempo == "fast":
        return ["kinetic", "percussive", "ad/shortform-friendly"]
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
    path.parent.mkdir(parents=True, exist_ok=True)
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
    path.write_text("\n".join(lines), encoding="utf-8")
