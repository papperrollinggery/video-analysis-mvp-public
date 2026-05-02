from __future__ import annotations

import mimetypes
import shutil
import uuid
from pathlib import Path

from .paths import ProjectPaths, slugify
from .schemas import AnalysisProfile, CanonicalMediaPackage, SourceType, dump_json
from .store import write_manifest
from .utils import require_tool, run_command, run_json


VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}


def infer_source_type(source: str) -> SourceType:
    if source.startswith(("http://", "https://")):
        return SourceType.url
    return SourceType.file


def create_project_id(source: str) -> str:
    stem = Path(source).stem if not source.startswith(("http://", "https://")) else source.split("?")[0].rstrip("/").split("/")[-1]
    return f"{slugify(stem)}-{uuid.uuid4().hex[:8]}"


def ffprobe_metadata(path: Path) -> dict:
    require_tool("ffprobe")
    return run_json(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ]
    )


def parse_video_metadata(metadata: dict) -> tuple[float, float, str, float]:
    streams = metadata.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    fmt = metadata.get("format", {})
    duration = float(video.get("duration") or fmt.get("duration") or 0)
    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    frame_rate = _parse_fraction(video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1")
    resolution = f"{width}x{height}" if width and height else "unknown"
    aspect_ratio = round(width / height, 3) if width and height else 0.0
    return duration, frame_rate, resolution, aspect_ratio


def _parse_fraction(value: str) -> float:
    try:
        numerator, denominator = value.split("/")
        return round(float(numerator) / max(float(denominator), 1.0), 3)
    except Exception:
        return 0.0


def ingest_source(
    source: str,
    paths: ProjectPaths,
    profile: AnalysisProfile,
    password: str | None = None,
    review_height: int = 360,
) -> CanonicalMediaPackage:
    require_tool("ffmpeg")
    source_type = infer_source_type(source)
    master_path = paths.ingest / "master.mp4"
    yt_metadata: dict = {}

    if source_type == SourceType.file:
        input_path = Path(source).expanduser().resolve()
        if not input_path.exists():
            raise FileNotFoundError(f"Video file not found: {input_path}")
        if input_path.suffix.lower() not in VIDEO_EXTENSIONS and not (mimetypes.guess_type(input_path)[0] or "").startswith("video"):
            raise ValueError(f"Input does not look like a supported video: {input_path}")
        shutil.copy2(input_path, master_path)
    else:
        require_tool("yt-dlp")
        yt_metadata = _download_url(source, master_path, password, review_height)

    review_path = paths.assets / "review.mp4"
    audio_path = paths.assets / "audio.wav"
    _build_review_copy(master_path, review_path, review_height)
    _extract_audio(review_path, audio_path)
    metadata = ffprobe_metadata(review_path)
    duration, frame_rate, resolution, aspect_ratio = parse_video_metadata(metadata)
    package = CanonicalMediaPackage(
        project_id=paths.root.name,
        source_type=source_type,
        source=source,
        local_master_path=str(master_path),
        review_copy_path=str(review_path),
        audio_path=str(audio_path),
        duration_seconds=duration,
        frame_rate=frame_rate,
        resolution=resolution,
        aspect_ratio=aspect_ratio,
        status="created",
        analysis_profile=profile,
        metadata={"ffprobe": metadata, "yt_dlp": yt_metadata},
    )
    dump_json(paths.data / "media_package.json", package)
    write_manifest(
        paths,
        package,
        "ingested",
        {
            "media_package": str(paths.data / "media_package.json"),
            "review_copy": str(review_path),
            "audio_wav": str(audio_path),
        },
    )
    return package


def _download_url(source: str, master_path: Path, password: str | None, review_height: int) -> dict:
    metadata_args = ["yt-dlp", "--dump-single-json", source]
    if password:
        metadata_args[1:1] = ["--video-password", password]
    metadata = run_json(metadata_args, timeout=120)
    output_template = str(master_path.with_suffix(".%(ext)s"))
    args = [
        "yt-dlp",
        "-f",
        f"bestvideo[height<={review_height}]+bestaudio/best[height<={review_height}]/best",
        "--merge-output-format",
        "mp4",
        "-o",
        output_template,
        source,
    ]
    if password:
        args[1:1] = ["--video-password", password]
    run_command(args, timeout=600)
    downloaded = master_path
    if not downloaded.exists():
        candidates = sorted(master_path.parent.glob("master.*"))
        if not candidates:
            raise FileNotFoundError("yt-dlp completed but no master video was produced")
        candidates[0].rename(master_path)
    return {
        "title": metadata.get("title"),
        "duration": metadata.get("duration"),
        "uploader": metadata.get("uploader"),
        "webpage_url": metadata.get("webpage_url"),
    }


def _build_review_copy(master_path: Path, review_path: Path, review_height: int) -> None:
    run_command(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(master_path),
            "-vf",
            f"scale=-2:min({review_height}\\,ih)",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "24",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            str(review_path),
        ],
        timeout=600,
    )


def _extract_audio(video_path: Path, audio_path: Path) -> None:
    run_command(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(video_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            str(audio_path),
        ],
        timeout=300,
    )
