from __future__ import annotations

import hashlib
import ipaddress
import math
import mimetypes
import os
import shlex
import socket
import stat
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

from .paths import ProjectPaths, slugify
from .safe_io import advisory_file_lock, atomic_output_path
from .schemas import AnalysisProfile, CanonicalMediaPackage, SourceType, dump_json
from .store import write_manifest
from .utils import require_tool, run_command, run_json, sanitize_url_for_storage

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}
DEFAULT_MAX_DURATION_SECONDS = 60.0
DEFAULT_MAX_SOURCE_BYTES = 2 * 1024 * 1024 * 1024
FFPROBE_TIMEOUT_SECONDS = 60


def infer_source_type(source: str) -> SourceType:
    if source.startswith(("http://", "https://")):
        return SourceType.url
    return SourceType.file


def normalized_source(source: str) -> tuple[SourceType, str]:
    source_type = infer_source_type(source)
    if source_type == SourceType.url:
        parsed = urlsplit(source)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Source URL must be a valid http(s) URL")
        return source_type, sanitize_url_for_storage(source, reject_userinfo=True)
    expanded = Path(source).expanduser()
    # Relative CLI paths have one explicit meaning: resolve against the caller's
    # current working directory. Web intake canonicalizes to absolute first.
    absolute = _canonicalize_system_prefix(Path(os.path.abspath(os.fspath(expanded))))
    return source_type, str(absolute)


def _canonicalize_system_prefix(path: Path) -> Path:
    """Resolve only a symlinked top-level OS alias (for example macOS /var).

    Later user-controlled components remain lexical so the descriptor walk can
    reject them instead of silently following them.
    """
    if os.name != "posix" or not path.is_absolute() or len(path.parts) < 2:
        return path
    first = Path(path.anchor) / path.parts[1]
    try:
        info = first.lstat()
    except OSError:
        return path
    if not stat.S_ISLNK(info.st_mode):
        return path
    resolved = first.resolve(strict=True)
    return resolved.joinpath(*path.parts[2:])


def create_project_id(source: str) -> str:
    source_type, safe_source = normalized_source(source)
    stem = Path(safe_source).stem if source_type == SourceType.file else urlsplit(safe_source).path.rstrip("/").split("/")[-1]
    return f"{slugify(stem)}-{uuid.uuid4().hex[:8]}"


def ffprobe_metadata(path: Path) -> dict:
    ffprobe = require_tool("ffprobe")
    return run_json(
        [
            ffprobe,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        timeout=FFPROBE_TIMEOUT_SECONDS,
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
    max_duration_seconds: float = DEFAULT_MAX_DURATION_SECONDS,
    max_source_bytes: int = DEFAULT_MAX_SOURCE_BYTES,
) -> CanonicalMediaPackage:
    if not math.isfinite(max_duration_seconds) or max_duration_seconds <= 0:
        raise ValueError("max_duration_seconds must be a positive finite number")
    if isinstance(max_source_bytes, bool) or not isinstance(max_source_bytes, int) or max_source_bytes <= 0:
        raise ValueError("max_source_bytes must be a positive integer")
    with advisory_file_lock(paths.root / ".ingest.lock", root=paths.root):
        _assert_fresh_ingest_targets(paths)
        return _ingest_source_locked(
            source,
            paths,
            profile,
            password=password,
            review_height=review_height,
            max_duration_seconds=max_duration_seconds,
            max_source_bytes=max_source_bytes,
        )


def _ingest_source_locked(
    source: str,
    paths: ProjectPaths,
    profile: AnalysisProfile,
    *,
    password: str | None,
    review_height: int,
    max_duration_seconds: float,
    max_source_bytes: int,
) -> CanonicalMediaPackage:
    source_type, persisted_source = normalized_source(source)
    master_path = paths.ingest / "master.mp4"
    review_path = paths.assets / "review.mp4"
    audio_path = paths.assets / "audio.wav"
    yt_metadata: dict[str, Any] = {}

    try:
        if source_type == SourceType.file:
            input_path = Path(persisted_source)
            if input_path.suffix.lower() not in VIDEO_EXTENSIONS and not (
                mimetypes.guess_type(input_path)[0] or ""
            ).startswith("video"):
                raise ValueError(f"Input does not look like a supported video: {input_path}")
            _copy_local_source(input_path, master_path, max_source_bytes)
        else:
            yt_metadata = _download_url(
                source,
                master_path,
                password,
                review_height,
                max_source_bytes=max_source_bytes,
            )

        master_metadata = _validate_media_file(
            master_path,
            max_duration_seconds=max_duration_seconds,
            max_source_bytes=max_source_bytes,
        )
        source_has_audio = _has_audio_stream(master_metadata)
        _build_review_copy(master_path, review_path, review_height)
        metadata = _validate_media_file(
            review_path,
            max_duration_seconds=max_duration_seconds,
            max_source_bytes=max_source_bytes,
        )
        if _has_audio_stream(metadata) != source_has_audio:
            raise ValueError("Review copy audio streams do not match the source media")
        if source_has_audio:
            _extract_audio(review_path, audio_path)
        duration, frame_rate, resolution, aspect_ratio = parse_video_metadata(metadata)
    except Exception:
        _cleanup_failed_ingest(paths)
        raise

    package = CanonicalMediaPackage(
        project_id=paths.root.name,
        source_type=source_type,
        source=persisted_source,
        local_master_path=str(master_path),
        review_copy_path=str(review_path),
        audio_path=str(audio_path) if source_has_audio else "",
        duration_seconds=duration,
        frame_rate=frame_rate,
        resolution=resolution,
        aspect_ratio=aspect_ratio,
        status="created",
        analysis_profile=profile,
        metadata={
            "ffprobe": _sanitize_metadata(metadata),
            "yt_dlp": _sanitize_metadata(yt_metadata),
            "media_receipt": {
                "schema_version": "1.0",
                "master": _media_file_receipt(master_path, master_metadata),
                "review": _media_file_receipt(review_path, metadata),
                "audio_stream_present": source_has_audio,
            },
        },
    )
    dump_json(paths.data / "media_package.json", package)
    artifacts = {
        "media_package": str(paths.data / "media_package.json"),
        "review_copy": str(review_path),
    }
    if source_has_audio:
        artifacts["audio_wav"] = str(audio_path)
    write_manifest(
        paths,
        package,
        "ingested",
        artifacts,
    )
    return package


def _assert_fresh_ingest_targets(paths: ProjectPaths) -> None:
    managed = [
        *paths.ingest.glob("master.*"),
        paths.assets / "review.mp4",
        paths.assets / "audio.wav",
        paths.data / "media_package.json",
        paths.manifest,
    ]
    if any(os.path.lexists(candidate) for candidate in managed):
        raise FileExistsError("Project already contains ingest artifacts; create a new project id")


def _copy_local_source(input_path: Path, master_path: Path, maximum: int) -> None:
    with _open_regular_no_symlinks(input_path) as source_fd:
        info = os.fstat(source_fd)
        if info.st_size <= 0:
            raise ValueError("Video source is empty")
        if info.st_size > maximum:
            raise ValueError(f"Video source exceeds the {maximum}-byte ingest limit")
        _copy_fd_atomic(source_fd, master_path, maximum)


@contextmanager
def _open_regular_no_symlinks(path: Path) -> Iterator[int]:
    absolute = _canonicalize_system_prefix(Path(os.path.abspath(os.fspath(path))))
    if os.name != "posix":  # pragma: no cover - exercised by Windows CI
        if absolute.is_symlink():
            raise ValueError("Video source must not be a symlink")
        descriptor = os.open(absolute, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("Video source must be a regular file")
            yield descriptor
        finally:
            os.close(descriptor)
        return

    flags_dir = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(absolute.anchor or "/", flags_dir)
    try:
        parts = absolute.parts[1:] if absolute.is_absolute() else absolute.parts
        if not parts:
            raise ValueError("Video source path is invalid")
        for component in parts[:-1]:
            if component in {"", ".", ".."}:
                raise ValueError("Video source path contains an unsafe component")
            next_fd = os.open(component, flags_dir, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_fd
        file_fd = os.open(
            parts[-1],
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
            dir_fd=descriptor,
        )
        try:
            if not stat.S_ISREG(os.fstat(file_fd).st_mode):
                raise ValueError("Video source must be a regular file")
            yield file_fd
        finally:
            os.close(file_fd)
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"Video file not found: {absolute}") from exc
    except OSError as exc:
        raise ValueError("Video source cannot be opened without following symlinks") from exc
    finally:
        os.close(descriptor)


def _copy_fd_atomic(source_fd: int, target: Path, maximum: int) -> None:
    with atomic_output_path(target) as temporary:
        output_fd = os.open(
            temporary,
            os.O_WRONLY
            | os.O_TRUNC
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        total = 0
        try:
            while True:
                chunk = os.read(source_fd, 1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > maximum:
                    raise ValueError(f"Video source exceeds the {maximum}-byte ingest limit")
                view = memoryview(chunk)
                while view:
                    written = os.write(output_fd, view)
                    if written <= 0:  # pragma: no cover
                        raise OSError("short write while copying source media")
                    view = view[written:]
            os.fsync(output_fd)
        finally:
            os.close(output_fd)


def _download_url(
    source: str,
    master_path: Path,
    password: str | None,
    review_height: int,
    *,
    max_source_bytes: int,
) -> dict:
    # Validate before the original URL (which may contain a sensitive query) is
    # handed to a subprocess. Userinfo is never accepted.
    _reject_config_injection(source, label="Source URL")
    if password:
        _reject_config_injection(password, label="Video password")
    safe_source = sanitize_url_for_storage(source, reject_userinfo=True)
    parsed = urlsplit(source)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Source URL must be a valid http(s) URL")
    _validate_initial_url_target(source)
    yt_dlp = require_tool("yt-dlp")
    # External downloaders write into an isolated temporary directory. Only a
    # validated regular file is copied into the project through atomic commit.
    with tempfile.TemporaryDirectory(prefix="video-analysis-download-") as directory:
        download_root = Path(directory)
        batch_path = download_root / "source.batch"
        _write_private_text_file(batch_path, f"{source}\n")
        private_options = ["--ignore-config", "--no-playlist"]
        if password:
            config_path = download_root / "credentials.conf"
            _write_private_text_file(
                config_path,
                f"--video-password {shlex.quote(password)}\n",
            )
            private_options.extend(["--config-locations", str(config_path)])
        sensitive_values = tuple(value for value in (source, password) if value)
        metadata_args = [
            yt_dlp,
            *private_options,
            "--dump-single-json",
            "--batch-file",
            str(batch_path),
        ]
        metadata = run_json(
            metadata_args,
            timeout=120,
            sensitive_values=sensitive_values,
        )
        if metadata.get("_type") in {"playlist", "multi_video"} or isinstance(
            metadata.get("entries"), list
        ):
            raise ValueError("URL ingest accepts exactly one video, not a playlist")
        output_template = str(download_root / "master.%(ext)s")
        args = [
            yt_dlp,
            *private_options,
            "--max-filesize",
            str(max_source_bytes),
            "-f",
            f"bestvideo[height<={review_height}]+bestaudio/best[height<={review_height}]/best",
            "--merge-output-format",
            "mp4",
            "-o",
            output_template,
            "--batch-file",
            str(batch_path),
        ]
        run_command(
            args,
            timeout=600,
            sensitive_values=sensitive_values,
        )
        candidates = [
            candidate
            for candidate in sorted(download_root.glob("master.*"))
            if candidate.suffix.lower() in VIDEO_EXTENSIONS and candidate.is_file() and not candidate.is_symlink()
        ]
        if not candidates:
            raise FileNotFoundError("yt-dlp completed but no master video was produced")
        _copy_local_source(candidates[0].resolve(), master_path, max_source_bytes)
    return {
        "title": metadata.get("title"),
        "duration": metadata.get("duration"),
        "uploader": metadata.get("uploader"),
        "webpage_url": sanitize_url_for_storage(
            str(metadata.get("webpage_url") or safe_source),
            reject_userinfo=False,
        ),
    }


def _reject_config_injection(value: str, *, label: str) -> None:
    if any(character in value for character in ("\r", "\n", "\0")):
        raise ValueError(f"{label} must not contain CR, LF, or NUL characters")


def _write_private_text_file(path: Path, value: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    if os.name == "posix":
        flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor = -1
            handle.write(value)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _validate_initial_url_target(source: str) -> None:
    parsed = urlsplit(source)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Source URL must contain a valid port") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Source URL must be a valid http(s) URL")

    hostname = parsed.hostname.rstrip(".")
    if not hostname:
        raise ValueError("Source URL must contain a valid hostname")
    try:
        addresses = [ipaddress.ip_address(hostname.split("%", 1)[0])]
    except ValueError:
        try:
            answers = socket.getaddrinfo(
                hostname,
                port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        except socket.gaierror as exc:
            raise ValueError("Source URL hostname could not be resolved") from exc
        addresses = []
        for family, _socket_type, _protocol, _canonical_name, socket_address in answers:
            if family not in {socket.AF_INET, socket.AF_INET6}:
                continue
            try:
                addresses.append(ipaddress.ip_address(str(socket_address[0]).split("%", 1)[0]))
            except ValueError:
                continue
        if not addresses:
            raise ValueError("Source URL hostname could not be resolved")

    if any(_is_non_public_address(address) for address in addresses):
        raise ValueError("Source URL must resolve only to public network addresses")


def _is_non_public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    candidates = [address]
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        candidates.append(address.ipv4_mapped)
    return any(
        not candidate.is_global
        or candidate.is_loopback
        or candidate.is_private
        or candidate.is_link_local
        or candidate.is_reserved
        or candidate.is_unspecified
        or candidate.is_multicast
        for candidate in candidates
    )


def _validate_media_file(path: Path, *, max_duration_seconds: float, max_source_bytes: int) -> dict:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise ValueError("Ingest did not produce a media file") from exc
    if path.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise ValueError("Ingested media must be a regular non-symlink file")
    if info.st_size <= 0:
        raise ValueError("Ingested media is empty")
    if info.st_size > max_source_bytes:
        raise ValueError(f"Ingested media exceeds the {max_source_bytes}-byte limit")
    metadata = ffprobe_metadata(path)
    streams = metadata.get("streams")
    if not isinstance(streams, list) or not any(
        isinstance(stream, dict) and stream.get("codec_type") == "video" for stream in streams
    ):
        raise ValueError("Ingested file does not contain a video stream")
    duration, _frame_rate, _resolution, _aspect_ratio = parse_video_metadata(metadata)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Ingested media duration is missing or invalid")
    if duration > max_duration_seconds:
        raise ValueError(
            f"Ingested media duration {duration:.3f}s exceeds the {max_duration_seconds:.3f}s limit"
        )
    return metadata


def _media_file_receipt(path: Path, metadata: dict[str, Any]) -> dict[str, Any]:
    duration, frame_rate, resolution, aspect_ratio = parse_video_metadata(metadata)
    return {
        "sha256": _sha256_regular_file(path),
        "size_bytes": path.stat().st_size,
        "duration_seconds": duration,
        "frame_rate": frame_rate,
        "resolution": resolution,
        "aspect_ratio": aspect_ratio,
    }


def _has_audio_stream(metadata: dict[str, Any]) -> bool:
    streams = metadata.get("streams")
    return isinstance(streams, list) and any(
        isinstance(stream, dict) and stream.get("codec_type") == "audio"
        for stream in streams
    )


def verify_media_generation(paths: ProjectPaths) -> tuple[bool, list[str]]:
    """Verify that the canonical media files still match the ingest receipt."""
    try:
        from .store import load_media

        media = load_media(paths)
    except Exception as exc:
        return False, [f"media package is unreadable: {exc}"]
    receipt = media.metadata.get("media_receipt")
    if not isinstance(receipt, dict) or receipt.get("schema_version") != "1.0":
        return False, ["media receipt is missing or unsupported"]
    reasons: list[str] = []
    declared_audio_present = media.audio_path != ""
    expected = {
        "master": (Path(media.local_master_path), receipt.get("master")),
        "review": (Path(media.review_copy_path), receipt.get("review")),
    }
    for label, (path, stored) in expected.items():
        if not isinstance(stored, dict):
            reasons.append(f"{label} media receipt is missing")
            continue
        try:
            if path.is_symlink():
                raise ValueError("symlinks are not allowed")
            canonical = path.resolve(strict=True)
            canonical.relative_to(paths.root.resolve())
            if not canonical.is_file() or canonical.is_symlink():
                raise ValueError("not a regular project file")
            if canonical.stat().st_size != stored.get("size_bytes"):
                reasons.append(f"{label} media size does not match its receipt")
            if _sha256_regular_file(canonical) != stored.get("sha256"):
                reasons.append(f"{label} media digest does not match its receipt")
            if not declared_audio_present:
                try:
                    actual_audio_present = _has_audio_stream(ffprobe_metadata(canonical))
                except Exception as exc:
                    reasons.append(f"{label} media audio stream could not be verified: {exc}")
                else:
                    if actual_audio_present:
                        reasons.append(
                            f"{label} media audio stream does not match the media package declaration"
                        )
        except (OSError, ValueError) as exc:
            reasons.append(f"{label} media is missing or unsafe: {exc}")
    declared_in_receipt = receipt.get("audio_stream_present")
    if not declared_audio_present and declared_in_receipt is not False:
        reasons.append("media receipt audio stream declaration is invalid")
    elif declared_audio_present and declared_in_receipt is not None and declared_in_receipt is not True:
        reasons.append("media receipt audio stream declaration is invalid")
    if declared_audio_present:
        audio = Path(media.audio_path)
        try:
            if audio.is_symlink():
                raise ValueError("symlinks are not allowed")
            canonical_audio = audio.resolve(strict=True)
            canonical_audio.relative_to(paths.root.resolve())
            if canonical_audio != (paths.assets / "audio.wav").resolve() or not canonical_audio.is_file() or canonical_audio.is_symlink() or canonical_audio.stat().st_size <= 0:
                raise ValueError("not the canonical non-empty regular project audio WAV")
        except (OSError, ValueError) as exc:
            reasons.append(f"audio media is missing or unsafe: {exc}")
    elif os.path.lexists(paths.assets / "audio.wav"):
        reasons.append("audio WAV exists even though the source media has no audio stream")
    return not reasons, reasons


def _sha256_regular_file(path: Path) -> str:
    digest = hashlib.sha256()
    with _open_regular_no_symlinks(path) as descriptor:
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _cleanup_failed_ingest(paths: ProjectPaths) -> None:
    for candidate in [
        paths.ingest / "master.mp4",
        paths.assets / "review.mp4",
        paths.assets / "audio.wav",
        paths.data / "media_package.json",
        paths.manifest,
    ]:
        try:
            if candidate.is_file() or candidate.is_symlink():
                candidate.unlink()
        except OSError:
            pass


def _sanitize_metadata(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _sanitize_metadata(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_metadata(item) for item in value]
    if isinstance(value, str):
        return sanitize_url_for_storage(value, reject_userinfo=False)
    return value


def _build_review_copy(master_path: Path, review_path: Path, review_height: int) -> None:
    ffmpeg = require_tool("ffmpeg")
    with atomic_output_path(review_path) as temporary:
        run_command(
            [
                ffmpeg,
                "-y",
                "-i",
                str(master_path),
                "-map",
                "0:v:0",
                "-map",
                "0:a?",
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
                str(temporary),
            ],
            timeout=600,
        )


def _extract_audio(video_path: Path, audio_path: Path) -> None:
    ffmpeg = require_tool("ffmpeg")
    with atomic_output_path(audio_path) as temporary:
        run_command(
            [
                ffmpeg,
                "-y",
                "-i",
                str(video_path),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                str(temporary),
            ],
            timeout=300,
        )
