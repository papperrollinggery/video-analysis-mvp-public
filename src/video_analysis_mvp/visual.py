from __future__ import annotations

import csv
import math
from pathlib import Path

from .paths import ProjectPaths
from .schemas import CanonicalMediaPackage, Scene, Shot, dump_json
from .utils import format_clock, run_command


def analyze_visual(media: CanonicalMediaPackage, paths: ProjectPaths, interval_seconds: float = 8.0) -> tuple[list[Shot], list[Scene]]:
    video_path = Path(media.review_copy_path)
    _extract_keyframes(video_path, paths.keyframes, interval_seconds)
    _build_contact_sheet(video_path, paths.assets / "contact_sheet.jpg", interval_seconds)
    shots = _build_shots(media, interval_seconds)
    scenes = _build_scenes(shots)
    dump_json(paths.data / "shots.json", shots)
    dump_json(paths.data / "scenes.json", scenes)
    write_shots_csv(paths.reports / "shot_breakdown.csv", shots)
    return shots, scenes


def _extract_keyframes(video_path: Path, keyframes_dir: Path, interval_seconds: float) -> None:
    keyframes_dir.mkdir(parents=True, exist_ok=True)
    for old in keyframes_dir.glob("frame-*.jpg"):
        old.unlink()
    run_command(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(video_path),
            "-vf",
            f"fps=1/{interval_seconds}",
            str(keyframes_dir / "frame-%04d.jpg"),
        ],
        timeout=300,
    )


def _build_contact_sheet(video_path: Path, output_path: Path, interval_seconds: float) -> None:
    run_command(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(video_path),
            "-vf",
            f"fps=1/{interval_seconds},scale=320:-1,tile=3x4",
            "-frames:v",
            "1",
            str(output_path),
        ],
        timeout=300,
    )


def _build_shots(media: CanonicalMediaPackage, interval_seconds: float) -> list[Shot]:
    duration = max(media.duration_seconds, 0.1)
    target = _target_shot_length(media.analysis_profile.value, duration)
    count = max(1, math.ceil(duration / target))
    shots: list[Shot] = []
    for index in range(count):
        start = round(index * duration / count, 3)
        end = round((index + 1) * duration / count, 3)
        length = round(end - start, 3)
        shots.append(
            Shot(
                shot_id=f"shot_{index + 1:04d}",
                scene_no=f"{(index // 4) + 1:03d}",
                shot_no=index + 1,
                setup_id=chr(65 + (index % 26)),
                take_no="",
                start_time=start,
                end_time=end,
                duration=length,
                timecode=f"{format_clock(start)}-{format_clock(end)}",
                frame_ref=f"frame-{min(index + 1, max(1, math.ceil(duration / interval_seconds))):04d}.jpg",
                shot_scale="to annotate",
                camera_angle="to annotate",
                camera_motion="to annotate",
                lens="not inferable from final video",
                equipment="not inferable from final video",
                composition="to annotate",
                subject="to annotate",
                action="to annotate",
                int_ext="unknown",
                visual_description="to annotate from frame",
                direction_notes="to annotate blocking, screen direction, and action beat",
                lighting_vfx="to annotate lighting, VFX, and AI artifacts",
                sound_sync="sync",
                audio_notes="audio/rhythm detected; dialogue requires ASR or subtitle import",
                beat_density=0.0,
                rhythm_notes="pending audio sync",
                preferred_take="",
                estimated_production_time="",
                review_notes="machine segmented; visual fields require human/model annotation",
                confidence=0.28,
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


def _build_scenes(shots: list[Shot]) -> list[Scene]:
    if not shots:
        return []
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
                scene_function=_scene_function(len(scenes)),
                pace_label=pace,
                confidence=0.3,
            )
        )
    return scenes


def _scene_function(index: int) -> str:
    return ["opening hook", "development", "turning point", "resolution"][min(index, 3)]


def write_shots_csv(path: Path, shots: list[Shot]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "scene_no",
        "shot_no",
        "shot_id",
        "setup_id",
        "take_no",
        "timecode",
        "duration",
        "frame_ref",
        "shot_scale",
        "camera_angle",
        "camera_motion",
        "lens",
        "equipment",
        "composition",
        "subject",
        "location",
        "int_ext",
        "props",
        "visual_description",
        "content_summary",
        "scene_type",
        "action",
        "style_notes",
        "prompt_en",
        "prompt_zh",
        "direction_notes",
        "lighting_vfx",
        "onscreen_text",
        "dialogue",
        "sound_design",
        "sound_sync",
        "audio_notes",
        "music_state",
        "beat_density",
        "rhythm_notes",
        "motifs",
        "continuity_notes",
        "preferred_take",
        "estimated_production_time",
        "shoot_day",
        "review_notes",
        "confidence",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for shot in shots:
            raw = shot.model_dump(mode="json")
            row = {field: raw.get(field, "") for field in fields}
            if "motifs" in row:
                row["motifs"] = ";".join(row.get("motifs") or [])
            writer.writerow(row)
