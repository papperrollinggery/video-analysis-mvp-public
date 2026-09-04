from __future__ import annotations

import errno
import os
import secrets
import shutil
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_OUTPUT_DIRS = frozenset({"ingest", "assets", "data", "reports"})
DEFAULT_MAX_READ_BYTES = 64 * 1024 * 1024

@dataclass(slots=True)
class _PathLockEntry:
    lock: threading.RLock = field(default_factory=threading.RLock)
    users: int = 0


_PATH_LOCKS: dict[str, _PathLockEntry] = {}
_PATH_LOCKS_GUARD = threading.Lock()
_ADVISORY_LOCK_STATE = threading.local()


def infer_project_root(path: Path) -> Path:
    absolute = Path(os.path.abspath(os.fspath(path)))
    for ancestor in (absolute.parent, *absolute.parents):
        if ancestor.name in PROJECT_OUTPUT_DIRS:
            return ancestor.parent
    return absolute.parent


@contextmanager
def path_lock(path: Path) -> Iterator[None]:
    key = os.path.abspath(os.fspath(path))
    with _PATH_LOCKS_GUARD:
        entry = _PATH_LOCKS.setdefault(key, _PathLockEntry())
        # Count both holders and waiters before acquiring the per-path lock so
        # the entry cannot be removed while another thread still references it.
        entry.users += 1
    acquired = False
    try:
        entry.lock.acquire()
        acquired = True
        yield
    finally:
        if acquired:
            entry.lock.release()
        with _PATH_LOCKS_GUARD:
            entry.users -= 1
            if entry.users == 0 and _PATH_LOCKS.get(key) is entry:
                _PATH_LOCKS.pop(key, None)


@contextmanager
def advisory_file_lock(path: Path, *, root: Path | None = None) -> Iterator[None]:
    """Serialize a project transaction across threads and POSIX processes.

    The stable lock file is deliberately separate from atomically-replaced data
    files: locking the data inode itself would stop protecting the pathname as
    soon as another writer replaces it.
    """
    project_root = Path(os.path.abspath(os.fspath(root or infer_project_root(path))))
    target = Path(os.path.abspath(os.fspath(path)))
    with path_lock(target):
        held = getattr(_ADVISORY_LOCK_STATE, "held", None)
        if held is None:
            held = {}
            _ADVISORY_LOCK_STATE.held = held
        if held.get(os.fspath(target), 0):
            held[os.fspath(target)] += 1
            try:
                yield
            finally:
                held[os.fspath(target)] -= 1
            return
        with _parent_descriptor(project_root, target, create=True) as (parent_fd, name, resolved):
            _regular_entry(parent_fd, name, resolved, allow_missing=True)
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
            if parent_fd is not None:
                flags |= getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(name, flags, 0o600, dir_fd=parent_fd)
            else:  # pragma: no cover - exercised by Windows CI
                descriptor = os.open(resolved, flags, 0o600)
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError(f"Unsafe lock target: {resolved}")
                if os.name == "posix":
                    os.fchmod(descriptor, 0o600)
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_EX)
                held[os.fspath(target)] = 1
                try:
                    yield
                finally:
                    held.pop(os.fspath(target), None)
            finally:
                if os.name == "posix":
                    try:
                        import fcntl

                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                    except OSError:
                        pass
                os.close(descriptor)


def ensure_output_directory(path: Path, *, root: Path | None = None) -> Path:
    root_path = Path(os.path.abspath(os.fspath(root or infer_project_root(path))))
    target = Path(os.path.abspath(os.fspath(path)))
    try:
        target.relative_to(root_path)
    except ValueError:
        raise ValueError(f"Output directory escapes its project root: {target}") from None
    if target == root_path:
        _require_real_directory(root_path)
        return target
    with _parent_descriptor(root_path, target / ".directory-check", create=True):
        pass
    _require_real_directory(target)
    return target


def atomic_write_text(
    path: Path,
    text: str,
    *,
    encoding: str = "utf-8",
    root: Path | None = None,
) -> None:
    atomic_write_bytes(path, text.encode(encoding), root=root)


def atomic_write_bytes(path: Path, payload: bytes, *, root: Path | None = None) -> None:
    project_root = root or infer_project_root(path)
    with path_lock(path), _atomic_output(path, root=project_root) as temporary:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_TRUNC
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())


def read_regular_bytes(
    path: Path,
    *,
    root: Path | None = None,
    max_bytes: int = DEFAULT_MAX_READ_BYTES,
) -> bytes:
    """Read a bounded regular file without following a symlink target."""
    project_root = root or infer_project_root(path)
    with _parent_descriptor(project_root, path, create=False) as (parent_fd, name, target):
        _regular_entry(parent_fd, name, target, allow_missing=False)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        if parent_fd is not None:
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(name, flags, dir_fd=parent_fd)
        else:
            descriptor = os.open(target, flags)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(f"Unsafe input target: {target}")
            if info.st_size > max_bytes:
                raise ValueError(f"Input file exceeds the {max_bytes}-byte read limit: {target}")
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                payload = handle.read(max_bytes + 1)
            if len(payload) > max_bytes:
                raise ValueError(f"Input file exceeds the {max_bytes}-byte read limit: {target}")
            return payload
        finally:
            os.close(descriptor)


def remove_directory_tree(path: Path, *, root: Path) -> None:
    """Remove one real directory descriptor-relative without following symlinks."""
    with _parent_descriptor(root, path, create=False) as (parent_fd, name, target):
        info = _regular_entry_or_directory(parent_fd, name, target)
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ValueError(f"Unsafe directory target: {target}")
        if parent_fd is not None:
            shutil.rmtree(name, dir_fd=parent_fd)
            _fsync_directory(parent_fd)
        else:  # pragma: no cover - Windows CI
            shutil.rmtree(target)


def rename_directory_entry(
    parent: Path,
    source_name: str,
    target_name: str,
    *,
    root: Path,
) -> None:
    """Rename one real child directory while holding a stable parent descriptor."""
    if (
        not source_name
        or not target_name
        or source_name in {".", ".."}
        or target_name in {".", ".."}
        or "/" in source_name
        or "/" in target_name
        or "\\" in source_name
        or "\\" in target_name
    ):
        raise ValueError("Unsafe directory entry name")
    with _parent_descriptor(root, parent / ".rename-probe", create=False) as (
        parent_fd,
        _name,
        _target,
    ):
        if parent_fd is None:  # pragma: no cover - Windows CI
            source = parent / source_name
            target = parent / target_name
            _require_real_directory(source)
            if target.exists() or target.is_symlink():
                raise FileExistsError(target)
            os.replace(source, target)
            return
        source_info = os.stat(source_name, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISLNK(source_info.st_mode) or not stat.S_ISDIR(source_info.st_mode):
            raise ValueError("Unsafe source directory entry")
        try:
            os.stat(target_name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise FileExistsError(target_name)
        os.replace(
            source_name,
            target_name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        _fsync_directory(parent_fd)


@contextmanager
def atomic_output_path(path: Path, *, root: Path | None = None) -> Iterator[Path]:
    """Yield an exclusive same-directory temporary path and atomically commit it."""
    project_root = root or infer_project_root(path)
    with path_lock(path), _atomic_output(path, root=project_root) as temporary:
        yield temporary


@contextmanager
def _atomic_output(path: Path, *, root: Path) -> Iterator[Path]:
    with _parent_descriptor(root, path, create=True) as (parent_fd, name, target):
        _regular_entry(parent_fd, name, target, allow_missing=True)
        token = secrets.token_hex(12)
        suffix = "".join(path.suffixes[-1:])
        temporary_name = f".{path.stem}.{token}.tmp{suffix}"
        temporary = target.parent / temporary_name
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        if parent_fd is not None:
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(temporary_name, flags, 0o600, dir_fd=parent_fd)
        else:
            descriptor = os.open(temporary, flags, 0o600)
        os.close(descriptor)
        committed = False
        try:
            yield temporary
            _regular_entry(parent_fd, temporary_name, temporary, allow_missing=False)
            _regular_entry(parent_fd, name, target, allow_missing=True)
            if parent_fd is not None:
                os.replace(
                    temporary_name,
                    name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
                _fsync_directory(parent_fd)
            else:
                os.replace(temporary, target)
            committed = True
        finally:
            if not committed:
                try:
                    if parent_fd is not None:
                        os.unlink(temporary_name, dir_fd=parent_fd)
                    else:
                        temporary.unlink()
                except FileNotFoundError:
                    pass


@contextmanager
def _parent_descriptor(
    root: Path,
    path: Path,
    *,
    create: bool,
) -> Iterator[tuple[int | None, str, Path]]:
    root_path = Path(os.path.abspath(os.fspath(root)))
    target = path if path.is_absolute() else root_path / path
    target_path = Path(os.path.abspath(os.fspath(target)))
    try:
        relative = target_path.relative_to(root_path)
    except ValueError:
        raise ValueError(f"Output path escapes its project root: {target_path}") from None
    if not relative.parts:
        raise ValueError("Output path must name a file")

    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        current = root_path
        _require_real_directory(current)
        for part in relative.parts[:-1]:
            current = current / part
            try:
                info = current.lstat()
            except FileNotFoundError:
                if not create:
                    raise
                current.mkdir(mode=0o700)
                info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise ValueError(f"Unsafe output directory: {current}")
        yield None, relative.name, target_path
        return

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    descriptors: list[int] = []
    try:
        try:
            current_fd = os.open(root_path, flags)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise ValueError(f"Unsafe output root: {root_path}") from None
            raise
        descriptors.append(current_fd)
        for part in relative.parts[:-1]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=current_fd)
                except FileExistsError:
                    pass
            try:
                next_fd = os.open(part, flags, dir_fd=current_fd)
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise ValueError(f"Unsafe output directory component: {part}") from None
                raise
            descriptors.append(next_fd)
            current_fd = next_fd
        yield current_fd, relative.name, target_path
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _regular_entry(
    parent_fd: int | None,
    name: str,
    path: Path,
    *,
    allow_missing: bool,
) -> os.stat_result | None:
    try:
        info = (
            os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if parent_fd is not None
            else path.lstat()
        )
    except FileNotFoundError:
        if allow_missing:
            return None
        raise
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValueError(f"Unsafe output target: {path}")
    return info


def _regular_entry_or_directory(
    parent_fd: int | None,
    name: str,
    path: Path,
) -> os.stat_result:
    return (
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if parent_fd is not None
        else path.lstat()
    )


def _require_real_directory(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise ValueError(f"Output directory does not exist: {path}") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"Unsafe output directory: {path}")


def _fsync_directory(descriptor: int) -> None:
    try:
        os.fsync(descriptor)
    except OSError as exc:
        if exc.errno not in {errno.EINVAL, getattr(errno, "ENOTSUP", errno.EINVAL)}:
            raise
