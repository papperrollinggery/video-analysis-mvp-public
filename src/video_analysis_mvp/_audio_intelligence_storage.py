from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import secrets
import stat
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .media import _open_regular_no_symlinks
from .paths import ProjectPaths

MAX_AUDIO_INTELLIGENCE_BYTES = 64 * 1024 * 1024
MAX_AUDIO_INPUT_BYTES = 2 * 1024 * 1024 * 1024
AUDIO_INTELLIGENCE_FILE_DIGEST_MODE = "sha256-file-v1"
STAGED_FILES = ("audio_intelligence.json", "audio_intelligence_generation.json")
RECOVERY_PATTERN = re.compile(r"\.audio-intelligence-recovery-[0-9a-f]{24}")
RECOVERY_FILES = frozenset(STAGED_FILES)


@dataclass(frozen=True)
class StagingArea:
    root: Path
    root_fd: int
    stage_root_fd: int
    data_fd: int
    stage_name: str
    fallback_data: Path | None = None
    cleanup_warnings: list[str] = field(default_factory=list)

    @property
    def is_descriptor_backed(self) -> bool:
        return self.data_fd >= 0


@contextmanager
def staging_area(root: Path) -> Iterator[StagingArea]:
    if os.name != "posix" or not hasattr(
        os, "O_NOFOLLOW"
    ):  # pragma: no cover - Windows CI
        with _portable_staging_area(root) as area:
            yield area
        return

    flags = _directory_open_flags()
    root_fd = os.open(root, flags)
    stage_root_fd = -1
    data_fd = -1
    stage_name = ""
    cleanup_warnings: list[str] = []
    try:
        stage_name = _create_directory(root_fd, ".audio-intelligence-stage-")
        stage_root_fd = os.open(stage_name, flags, dir_fd=root_fd)
        os.mkdir("data", mode=0o700, dir_fd=stage_root_fd)
        data_fd = os.open("data", flags, dir_fd=stage_root_fd)
        _fsync_directory_fd(stage_root_fd)
        _fsync_directory_fd(root_fd)
        yield StagingArea(
            root,
            root_fd,
            stage_root_fd,
            data_fd,
            stage_name,
            cleanup_warnings=cleanup_warnings,
        )
    finally:
        cleanup_warnings.extend(
            _cleanup_staging_descriptors(root_fd, stage_root_fd, data_fd, stage_name)
        )


@contextmanager
def _portable_staging_area(root: Path) -> Iterator[StagingArea]:  # pragma: no cover
    temporary = tempfile.TemporaryDirectory(
        prefix=".audio-intelligence-stage-", dir=root
    )
    data = Path(temporary.name) / "data"
    area = StagingArea(root, -1, -1, -1, Path(temporary.name).name, data)
    try:
        data.mkdir(mode=0o700)
        yield area
    finally:
        try:
            temporary.cleanup()
        except OSError as exc:
            area.cleanup_warnings.append(f"stage_cleanup:{type(exc).__name__}")


def _cleanup_staging_descriptors(
    root_fd: int, stage_root_fd: int, data_fd: int, stage_name: str
) -> list[str]:
    warnings: list[str] = []
    operations: list[tuple[str, Callable[[], None]]] = []
    if data_fd >= 0:
        operations.extend(
            ("stage_file_cleanup", lambda name=name: os.unlink(name, dir_fd=data_fd))
            for name in STAGED_FILES
        )
        operations.append(("stage_data_sync", lambda: _fsync_directory_fd(data_fd)))
    if stage_root_fd >= 0:
        operations.append(
            ("stage_data_cleanup", lambda: os.rmdir("data", dir_fd=stage_root_fd))
        )
    if stage_name:
        operations.append(
            ("stage_root_cleanup", lambda: os.rmdir(stage_name, dir_fd=root_fd))
        )
        operations.append(("stage_root_sync", lambda: _fsync_directory_fd(root_fd)))
    try:
        for label, operation in operations:
            try:
                operation()
            except FileNotFoundError:
                pass
            except OSError as exc:
                warnings.append(f"{label}:{type(exc).__name__}")
    finally:
        for descriptor in (data_fd, stage_root_fd, root_fd):
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError as exc:
                    warnings.append(f"stage_descriptor_close:{type(exc).__name__}")
    return warnings


def write_staged_file(area: StagingArea, name: str, payload: bytes) -> None:
    if name not in STAGED_FILES:
        raise ValueError(f"unsupported audio intelligence staged file: {name}")
    if area.is_descriptor_backed:
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
        )
        descriptor = os.open(name, flags, 0o600, dir_fd=area.data_fd)
    else:  # pragma: no cover - Windows CI
        assert area.fallback_data is not None
        descriptor = os.open(
            area.fallback_data / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
    try:
        _write_all(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if area.is_descriptor_backed:
        _fsync_directory_fd(area.data_fd)
    else:  # pragma: no cover - Windows CI
        _fsync_directory_path(area.fallback_data)


def commit_audio_intelligence(
    paths: ProjectPaths,
    area: StagingArea,
    *,
    validate_committed: Callable[[int | None], Any],
) -> Any:
    if not area.is_descriptor_backed:  # pragma: no cover - Windows CI
        return _commit_audio_intelligence_fallback(paths, area, validate_committed)

    data_fd = os.open("data", _directory_open_flags(), dir_fd=area.root_fd)
    try:
        return _commit_descriptor_generation(
            area=area,
            data_fd=data_fd,
            validate_committed=validate_committed,
        )
    finally:
        os.close(data_fd)


def _commit_descriptor_generation(
    *,
    area: StagingArea,
    data_fd: int,
    validate_committed: Callable[[int | None], Any],
) -> Any:
    recovery_name, recovery_fd = _create_recovery_directory(area.root_fd)
    moved_old: list[str] = []
    committed: list[str] = []
    try:
        try:
            _move_current_generation(data_fd, recovery_fd, moved_old)
            _sync_transaction_directories(area.root_fd, data_fd, recovery_fd)
            _install_staged_generation(area.data_fd, data_fd, committed)
            result = validate_committed(data_fd)
            _assert_commit_location(area, data_fd)
        except BaseException as primary_error:
            _rollback_descriptor_generation(
                root_fd=area.root_fd,
                data_fd=data_fd,
                recovery_fd=recovery_fd,
                recovery_name=recovery_name,
                committed=committed,
                moved_old=moved_old,
                primary_error=primary_error,
            )
            raise
        _cleanup_committed_recovery(
            root_fd=area.root_fd,
            recovery_fd=recovery_fd,
            recovery_name=recovery_name,
            moved_old=moved_old,
        )
        return result
    finally:
        os.close(recovery_fd)


def _move_current_generation(data_fd: int, recovery_fd: int, moved: list[str]) -> None:
    for name in STAGED_FILES:
        if not _regular_entry_exists(data_fd, name):
            continue
        os.replace(name, name, src_dir_fd=data_fd, dst_dir_fd=recovery_fd)
        moved.append(name)
    _fsync_directory_fd(recovery_fd)


def _install_staged_generation(
    stage_fd: int, data_fd: int, committed: list[str]
) -> None:
    for name in STAGED_FILES:
        os.replace(name, name, src_dir_fd=stage_fd, dst_dir_fd=data_fd)
        committed.append(name)
    _fsync_directory_fd(data_fd)


def _rollback_descriptor_generation(
    *,
    root_fd: int,
    data_fd: int,
    recovery_fd: int,
    recovery_name: str,
    committed: list[str],
    moved_old: list[str],
    primary_error: BaseException,
) -> None:
    errors = _restore_audio_intelligence_generation(
        data_fd=data_fd,
        recovery_fd=recovery_fd,
        committed=committed,
        moved_old=moved_old,
    )
    _sync_transaction_directories(root_fd, data_fd, recovery_fd)
    if errors:
        raise RuntimeError(
            "audio intelligence rollback was incomplete; previous bytes are retained in "
            f"{recovery_name}"
        ) from primary_error
    _remove_recovery_directory(
        root_fd=root_fd,
        recovery_fd=recovery_fd,
        recovery_name=recovery_name,
        names=(),
    )


def _cleanup_committed_recovery(
    *, root_fd: int, recovery_fd: int, recovery_name: str, moved_old: list[str]
) -> None:
    try:
        _remove_recovery_directory(
            root_fd=root_fd,
            recovery_fd=recovery_fd,
            recovery_name=recovery_name,
            names=moved_old,
        )
    except OSError:
        # The committed generation remains valid. Public binding/status exposes
        # cleanup_required and cleanup_audio_intelligence_recovery can retry.
        pass


def _sync_transaction_directories(root_fd: int, data_fd: int, recovery_fd: int) -> None:
    _fsync_directory_fd(data_fd)
    _fsync_directory_fd(recovery_fd)
    _fsync_directory_fd(root_fd)


def _assert_commit_location(area: StagingArea, data_fd: int) -> None:
    root_info = os.stat(area.root, follow_symlinks=False)
    held_root = os.fstat(area.root_fd)
    data_info = os.stat("data", dir_fd=area.root_fd, follow_symlinks=False)
    held_data = os.fstat(data_fd)
    for current, held in ((root_info, held_root), (data_info, held_data)):
        if not stat.S_ISDIR(current.st_mode) or (
            current.st_dev,
            current.st_ino,
        ) != (held.st_dev, held.st_ino):
            raise ValueError("audio intelligence commit directory was replaced")


def cleanup_recovery_directories(
    paths: ProjectPaths,
    *,
    validate_current: Callable[[], Any],
) -> dict[str, Any]:
    validate_current()
    if os.name != "posix" or not hasattr(
        os, "O_NOFOLLOW"
    ):  # pragma: no cover - Windows CI
        return _cleanup_recovery_directories_fallback(paths)
    root_fd = os.open(paths.root, _directory_open_flags())
    try:
        for name in _recovery_names_from_fd(root_fd):
            recovery_fd = os.open(name, _directory_open_flags(), dir_fd=root_fd)
            try:
                entries = set(os.listdir(recovery_fd))
                unexpected = entries - RECOVERY_FILES
                if unexpected:
                    raise ValueError(
                        f"audio intelligence recovery contains unexpected entries: {name}"
                    )
                for filename in sorted(entries):
                    if not _regular_entry_exists(recovery_fd, filename):
                        raise ValueError(
                            f"audio intelligence recovery entry is unsafe: {name}/{filename}"
                        )
                _remove_recovery_directory(
                    root_fd=root_fd,
                    recovery_fd=recovery_fd,
                    recovery_name=name,
                    names=sorted(entries),
                )
            finally:
                os.close(recovery_fd)
        return recovery_state(paths)
    finally:
        os.close(root_fd)


def recovery_state(paths: ProjectPaths) -> dict[str, Any]:
    if os.name != "posix" or not hasattr(
        os, "O_NOFOLLOW"
    ):  # pragma: no cover - Windows CI
        names = sorted(
            item.name
            for item in paths.root.glob(".audio-intelligence-recovery-*")
            if item.is_dir() and not item.is_symlink()
        )
        return {"cleanup_required": bool(names), "recovery_directories": names}
    root_fd = os.open(paths.root, _directory_open_flags())
    try:
        names = _recovery_names_from_fd(root_fd)
    finally:
        os.close(root_fd)
    return {"cleanup_required": bool(names), "recovery_directories": names}


def file_receipt(path: Path, maximum: int) -> dict[str, Any]:
    with _open_regular_no_symlinks(path) as descriptor:
        _payload, receipt = _read_descriptor(descriptor, maximum, retain_bytes=False)
    return receipt


def file_receipt_from_bytes(payload: bytes) -> dict[str, Any]:
    return {"sha256": hashlib.sha256(payload).hexdigest(), "size_bytes": len(payload)}


def read_file_bytes_and_receipt(
    path: Path, maximum: int
) -> tuple[bytes, dict[str, Any]]:
    with _open_regular_no_symlinks(path) as descriptor:
        return _read_descriptor(descriptor, maximum)


def read_relative_file_bytes_and_receipt(
    directory_fd: int,
    name: str,
    maximum: int,
) -> tuple[bytes, dict[str, Any]]:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptor = os.open(name, flags, dir_fd=directory_fd)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"audio intelligence file is not regular: {name}")
        return _read_descriptor(descriptor, maximum)
    finally:
        os.close(descriptor)


def strict_json_loads(payload: bytes) -> Any:
    return json.loads(
        payload.decode("utf-8"),
        parse_constant=_reject_json_constant,
        object_pairs_hook=_unique_json_object,
    )


def file_receipt_matches(stored: dict[str, Any], current: dict[str, Any]) -> bool:
    return stored["sha256"] == current.get("sha256") and stored[
        "size_bytes"
    ] == current.get("size_bytes")


def fsync_directory_path(path: Path) -> None:
    if os.name != "posix":  # pragma: no cover - Windows CI
        return
    descriptor = os.open(path, _directory_open_flags())
    try:
        _fsync_directory_fd(descriptor)
    finally:
        os.close(descriptor)


def _read_descriptor(
    descriptor: int, maximum: int, *, retain_bytes: bool = True
) -> tuple[bytes, dict[str, Any]]:
    before = os.fstat(descriptor)
    if before.st_size > maximum:
        raise ValueError(f"audio intelligence file exceeds {maximum} bytes")
    chunks: list[bytes] = []
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = os.read(descriptor, min(1024 * 1024, maximum - size + 1))
        if not chunk:
            break
        size += len(chunk)
        if size > maximum:
            raise ValueError(f"audio intelligence file exceeds {maximum} bytes")
        if retain_bytes:
            chunks.append(chunk)
        digest.update(chunk)
    after = os.fstat(descriptor)
    if _file_identity(before) != _file_identity(after):
        raise ValueError("audio intelligence file changed while it was read")
    return b"".join(chunks), {"sha256": digest.hexdigest(), "size_bytes": size}


def _commit_audio_intelligence_fallback(
    paths: ProjectPaths,
    area: StagingArea,
    validate_committed: Callable[[int | None], Any],
) -> Any:  # pragma: no cover - Windows CI
    assert area.fallback_data is not None
    targets = tuple(
        (area.fallback_data / name, paths.data / name) for name in STAGED_FILES
    )
    existing = _validate_fallback_targets(targets)
    recovery = Path(
        tempfile.mkdtemp(prefix=".audio-intelligence-recovery-", dir=paths.root)
    )
    moved_old: list[tuple[Path, Path]] = []
    committed: list[Path] = []
    try:
        for current in existing:
            backup = recovery / current.name
            os.replace(current, backup)
            moved_old.append((current, backup))
        for staged, destination in targets:
            os.replace(staged, destination)
            committed.append(destination)
        fsync_directory_path(paths.data)
        result = validate_committed(None)
    except BaseException as primary_error:
        _rollback_fallback_generation(
            paths=paths,
            recovery=recovery,
            committed=committed,
            moved_old=moved_old,
            primary_error=primary_error,
        )
        raise
    _cleanup_fallback_recovery(recovery, moved_old)
    return result


def _validate_fallback_targets(
    targets: tuple[tuple[Path, Path], ...],
) -> list[Path]:  # pragma: no cover - Windows CI
    existing = [
        destination for _staged, destination in targets if os.path.lexists(destination)
    ]
    for candidate in existing:
        info = candidate.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ValueError(
                f"Managed audio intelligence target is unsafe: {candidate}"
            )
    return existing


def _rollback_fallback_generation(
    *,
    paths: ProjectPaths,
    recovery: Path,
    committed: list[Path],
    moved_old: list[tuple[Path, Path]],
    primary_error: BaseException,
) -> None:  # pragma: no cover - Windows CI
    errors = _remove_fallback_committed(committed)
    errors.extend(_restore_fallback_generation(moved_old))
    fsync_directory_path(paths.data)
    if errors:
        raise RuntimeError(
            "audio intelligence rollback was incomplete; previous bytes are retained in "
            f"{recovery.name}"
        ) from primary_error
    recovery.rmdir()


def _remove_fallback_committed(committed: list[Path]) -> list[str]:
    errors: list[str] = []
    for destination in reversed(committed):
        try:
            destination.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            errors.append(f"remove {destination.name}: {type(exc).__name__}")
    return errors


def _restore_fallback_generation(
    moved_old: list[tuple[Path, Path]],
) -> list[str]:
    errors: list[str] = []
    for original, backup in reversed(moved_old):
        try:
            if os.path.lexists(backup):
                os.replace(backup, original)
        except OSError as exc:
            errors.append(f"restore {original.name}: {type(exc).__name__}")
    return errors


def _cleanup_fallback_recovery(
    recovery: Path, moved_old: list[tuple[Path, Path]]
) -> None:  # pragma: no cover - Windows CI
    try:
        for _original, backup in moved_old:
            try:
                backup.unlink()
            except FileNotFoundError:
                pass
        recovery.rmdir()
    except OSError:
        pass


def _cleanup_recovery_directories_fallback(
    paths: ProjectPaths,
) -> dict[str, Any]:  # pragma: no cover
    for directory in sorted(paths.root.glob(".audio-intelligence-recovery-*")):
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError(
                f"audio intelligence recovery directory is unsafe: {directory.name}"
            )
        entries = {item.name for item in directory.iterdir()}
        if entries - RECOVERY_FILES:
            raise ValueError(
                f"audio intelligence recovery contains unexpected entries: {directory.name}"
            )
        for name in entries:
            (directory / name).unlink()
        directory.rmdir()
    return recovery_state(paths)


def _create_recovery_directory(root_fd: int) -> tuple[str, int]:
    name = _create_directory(root_fd, ".audio-intelligence-recovery-")
    descriptor = -1
    try:
        descriptor = os.open(name, _directory_open_flags(), dir_fd=root_fd)
        _fsync_directory_fd(root_fd)
        return name, descriptor
    except BaseException as primary_error:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.rmdir(name, dir_fd=root_fd)
        except OSError:
            raise RuntimeError(
                f"audio intelligence recovery creation failed and left {name}"
            ) from primary_error
        try:
            _fsync_directory_fd(root_fd)
        except OSError:
            raise RuntimeError(
                "audio intelligence recovery creation failed; the empty directory was "
                "removed but parent-directory durability could not be confirmed"
            ) from primary_error
        raise


def _create_directory(parent_fd: int, prefix: str) -> str:
    for _attempt in range(16):
        name = f"{prefix}{secrets.token_hex(12)}"
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            continue
        return name
    raise RuntimeError(f"could not allocate directory: {prefix}")


def _regular_entry_exists(directory_fd: int, name: str) -> bool:
    try:
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValueError(f"Managed audio intelligence target is unsafe: {name}")
    return True


def _restore_audio_intelligence_generation(
    *,
    data_fd: int,
    recovery_fd: int,
    committed: list[str],
    moved_old: list[str],
) -> list[str]:
    errors: list[str] = []
    for name in reversed(committed):
        try:
            os.unlink(name, dir_fd=data_fd)
        except FileNotFoundError:
            pass
        except OSError as exc:
            errors.append(f"remove {name}: {type(exc).__name__}")
    for name in reversed(moved_old):
        try:
            if _regular_entry_exists(recovery_fd, name):
                os.replace(name, name, src_dir_fd=recovery_fd, dst_dir_fd=data_fd)
        except (OSError, ValueError) as exc:
            errors.append(f"restore {name}: {type(exc).__name__}")
    return errors


def _remove_recovery_directory(
    *,
    root_fd: int,
    recovery_fd: int,
    recovery_name: str,
    names: Any,
) -> None:
    for name in names:
        try:
            os.unlink(name, dir_fd=recovery_fd)
        except FileNotFoundError:
            pass
    _fsync_directory_fd(recovery_fd)
    os.rmdir(recovery_name, dir_fd=root_fd)
    _fsync_directory_fd(root_fd)


def _recovery_names_from_fd(root_fd: int) -> list[str]:
    names: list[str] = []
    for name in os.listdir(root_fd):
        if RECOVERY_PATTERN.fullmatch(name) is None:
            continue
        info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"audio intelligence recovery directory is unsafe: {name}")
        names.append(name)
    return sorted(names)


def _file_identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError("audio intelligence staged write made no progress")
        offset += written


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key is not allowed: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")


def _directory_open_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _fsync_directory_path(path: Path | None) -> None:
    if path is not None:
        fsync_directory_path(path)


def _fsync_directory_fd(descriptor: int) -> None:
    try:
        os.fsync(descriptor)
    except OSError as exc:
        if exc.errno not in {errno.EINVAL, getattr(errno, "ENOTSUP", errno.EINVAL)}:
            raise
