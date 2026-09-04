from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterator, Mapping
from urllib.parse import urlparse


class VisionProvider(str, Enum):
    openai = "openai"
    minimax_mcp = "minimax_mcp"
    bridgedeck = "bridgedeck"


DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MINIMAX_API_HOST = "https://api.minimaxi.com"
OFFICIAL_OPENAI_ORIGINS = frozenset({"https://api.openai.com"})
OFFICIAL_MINIMAX_ORIGINS = frozenset({"https://api.minimaxi.com", "https://api.minimax.io"})
MAX_CONFIG_BYTES = 1024 * 1024
_CONFIG_NAME = "runtime_config.json"
_SETTINGS_NAME = "_settings"
_LOCK_NAME = ".runtime_config.lock"
_LOCK_OPEN_ATTEMPTS = 16
_CONFIG_LOCKS: dict[str, threading.RLock] = {}
_CONFIG_LOCKS_GUARD = threading.Lock()
_CONFIG_LOCK_STATE = threading.local()


@dataclass(frozen=True)
class RuntimeConfig:
    vision_provider: str = VisionProvider.openai.value
    openai_api_key: str = ""
    openai_base_url: str = DEFAULT_OPENAI_BASE_URL
    openai_model: str = "gpt-5.4-mini"
    minimax_api_key: str = ""
    minimax_api_host: str = DEFAULT_MINIMAX_API_HOST
    bridgedeck_base_url: str = ""
    bridgedeck_model: str = ""
    audio_adapter_executable: str = ""
    audio_adapter_timeout_seconds: int = 120


@dataclass(frozen=True)
class _ConfigRecord:
    config: RuntimeConfig
    existed: bool
    fingerprint: tuple[int, int, int, int, str] | None


def config_path(workspace_root: Path) -> Path:
    return workspace_root / _SETTINGS_NAME / _CONFIG_NAME


def normalize_provider(value: str | VisionProvider | None) -> str:
    candidate = str(value.value if isinstance(value, VisionProvider) else value or "").strip().lower()
    try:
        return VisionProvider(candidate).value
    except ValueError as exc:
        allowed = ", ".join(item.value for item in VisionProvider)
        raise ValueError(f"Unsupported vision provider {candidate!r}; expected one of: {allowed}") from exc


def load_runtime_config(workspace_root: Path) -> RuntimeConfig:
    """Load settings without following symlinks or silently accepting corruption."""
    return _load_record(workspace_root, create_settings=False).config


def save_runtime_config(
    workspace_root: Path,
    updates: dict[str, str],
    keep_blank_secrets: bool = True,
) -> RuntimeConfig:
    """Atomically replace a private config after a fail-closed read of the old one."""
    if os.name == "posix":
        _ensure_directory_tree(workspace_root)
    else:  # pragma: no cover
        workspace_root.mkdir(parents=True, exist_ok=True)
    with _runtime_config_lock(workspace_root):
        return _save_runtime_config_locked(workspace_root, updates, keep_blank_secrets)


def _save_runtime_config_locked(
    workspace_root: Path,
    updates: dict[str, str],
    keep_blank_secrets: bool,
) -> RuntimeConfig:
    record = _load_record(workspace_root, create_settings=True)
    current = record.config
    openai_base_url = validate_endpoint(
        updates.get("openai_base_url", "").strip() or current.openai_base_url,
        "OpenAI base URL",
    )
    minimax_api_host = validate_endpoint(
        updates.get("minimax_api_host", "").strip() or current.minimax_api_host,
        "MiniMax API host",
    )
    provider_value = updates.get("vision_provider")
    provider = normalize_provider(provider_value if provider_value is not None else current.vision_provider)
    bridge_url, bridge_model = validate_bridgedeck_config(
        updates.get("bridgedeck_base_url", current.bridgedeck_base_url),
        updates.get("bridgedeck_model", current.bridgedeck_model),
        required=provider == VisionProvider.bridgedeck.value,
    )
    audio_executable, audio_timeout = validate_audio_adapter_config(
        updates.get("audio_adapter_executable", current.audio_adapter_executable),
        updates.get("audio_adapter_timeout_seconds", current.audio_adapter_timeout_seconds),
    )
    data = {
        "vision_provider": provider,
        "openai_api_key": _secret_value(
            updates.get("openai_api_key", ""),
            current.openai_api_key,
            keep_blank_secrets and openai_base_url == current.openai_base_url,
        ),
        "openai_base_url": openai_base_url,
        "openai_model": updates.get("openai_model") or current.openai_model,
        "minimax_api_key": _secret_value(
            updates.get("minimax_api_key", ""),
            current.minimax_api_key,
            keep_blank_secrets and minimax_api_host == current.minimax_api_host,
        ),
        "minimax_api_host": minimax_api_host,
        "bridgedeck_base_url": bridge_url,
        "bridgedeck_model": bridge_model,
        "audio_adapter_executable": audio_executable,
        "audio_adapter_timeout_seconds": audio_timeout,
    }
    payload = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if os.name == "posix":
        _save_posix(workspace_root, payload, record)
    else:  # pragma: no cover - exercised by Windows CI
        _save_portable(workspace_root, payload, record)
    return load_runtime_config(workspace_root)


@contextmanager
def _runtime_config_lock(workspace_root: Path) -> Iterator[None]:
    key = os.path.abspath(os.fspath(workspace_root))
    with _CONFIG_LOCKS_GUARD:
        process_lock = _CONFIG_LOCKS.setdefault(key, threading.RLock())
    with process_lock:
        held = getattr(_CONFIG_LOCK_STATE, "held", None)
        if held is None:
            held = {}
            _CONFIG_LOCK_STATE.held = held
        if held.get(key, 0):
            held[key] += 1
            try:
                yield
            finally:
                held[key] -= 1
            return
        if os.name != "posix":  # pragma: no cover - exercised by Windows CI
            held[key] = 1
            try:
                yield
            finally:
                held.pop(key, None)
            return
        with _open_settings_dir(workspace_root, create=True) as settings_fd:
            if settings_fd is None:  # pragma: no cover - create=True guarantees a directory
                raise ValueError("Runtime settings directory was not created")
            descriptor = _open_runtime_config_lock(settings_fd)
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("Runtime config lock must be a regular file")
                os.fchmod(descriptor, 0o600)
                import fcntl

                held[key] = 1
                try:
                    yield
                finally:
                    held.pop(key, None)
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def _open_runtime_config_lock(settings_fd: int) -> int:
    """Open one stable lock inode without the Darwin O_CREAT creation race.

    Opening a missing file with non-exclusive ``O_CREAT | O_NOFOLLOW`` can
    transiently return ``ENOENT`` when several processes create the pathname at
    once on Darwin.  Split "open existing" from exclusive creation, retry only
    the two expected creation races, and verify the directory entry still names
    the descriptor after taking the advisory lock.
    """
    import fcntl

    base_flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    create_flags = base_flags | os.O_CREAT | os.O_EXCL
    last_error: OSError | None = None
    for _attempt in range(_LOCK_OPEN_ATTEMPTS):
        descriptor: int | None = None
        locked = False
        acquired = False
        try:
            try:
                descriptor = os.open(_LOCK_NAME, base_flags, dir_fd=settings_fd)
            except FileNotFoundError:
                try:
                    descriptor = os.open(
                        _LOCK_NAME,
                        create_flags,
                        0o600,
                        dir_fd=settings_fd,
                    )
                except FileExistsError as exc:
                    last_error = exc
                    continue
                except FileNotFoundError as exc:
                    last_error = exc
                    continue

            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("Runtime config lock must be a regular file")
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            locked = True

            try:
                current = os.stat(_LOCK_NAME, dir_fd=settings_fd, follow_symlinks=False)
            except FileNotFoundError as exc:
                last_error = exc
                continue
            if (
                not stat.S_ISREG(current.st_mode)
                or current.st_dev != info.st_dev
                or current.st_ino != info.st_ino
            ):
                last_error = FileNotFoundError("Runtime config lock pathname changed during acquisition")
                continue
            acquired = True
            return descriptor
        except ValueError:
            raise
        except OSError as exc:
            raise ValueError("Runtime config lock cannot be opened safely") from exc
        finally:
            if descriptor is not None and not acquired:
                if locked:
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                    except OSError:
                        pass
                os.close(descriptor)

    raise ValueError("Runtime config lock cannot be opened safely") from last_error


def _load_record(workspace_root: Path, *, create_settings: bool) -> _ConfigRecord:
    if os.name == "posix":
        return _load_record_posix(workspace_root, create_settings=create_settings)
    return _load_record_portable(workspace_root, create_settings=create_settings)  # pragma: no cover


def _load_record_posix(workspace_root: Path, *, create_settings: bool) -> _ConfigRecord:
    if not os.path.lexists(workspace_root):
        if not create_settings:
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = _open_directory_tree(workspace_root, flags)
            except FileNotFoundError:
                return _ConfigRecord(RuntimeConfig(), False, None)
            except OSError as exc:
                raise ValueError(f"Workspace root cannot be opened safely: {workspace_root}") from exc
            else:
                os.close(descriptor)
        _ensure_directory_tree(workspace_root)
    with _open_settings_dir(workspace_root, create=create_settings) as settings_fd:
        if settings_fd is None:
            return _ConfigRecord(RuntimeConfig(), False, None)
        try:
            descriptor = os.open(
                _CONFIG_NAME,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=settings_fd,
            )
        except FileNotFoundError:
            return _ConfigRecord(RuntimeConfig(), False, None)
        except OSError as exc:
            raise ValueError("Runtime config cannot be opened safely") from exc
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("Runtime config must be a regular file")
            os.fchmod(descriptor, 0o600)
            raw = _read_fd_bounded(descriptor, MAX_CONFIG_BYTES)
        finally:
            os.close(descriptor)
        config = _parse_config(raw)
        return _ConfigRecord(config, True, _fingerprint(info, raw))


@contextmanager
def _open_settings_dir(workspace_root: Path, *, create: bool) -> Iterator[int | None]:
    root_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        root_fd = _open_directory_tree(workspace_root, root_flags)
    except OSError as exc:
        raise ValueError(f"Workspace root cannot be opened safely: {workspace_root}") from exc
    settings_fd: int | None = None
    try:
        if create:
            try:
                os.mkdir(_SETTINGS_NAME, 0o700, dir_fd=root_fd)
            except FileExistsError:
                pass
        try:
            settings_fd = os.open(_SETTINGS_NAME, root_flags, dir_fd=root_fd)
        except FileNotFoundError:
            yield None
            return
        except OSError as exc:
            raise ValueError("Runtime settings directory cannot be opened safely") from exc
        os.fchmod(settings_fd, 0o700)
        yield settings_fd
    finally:
        if settings_fd is not None:
            os.close(settings_fd)
        os.close(root_fd)


def _canonicalize_system_prefix(path: Path) -> Path:
    absolute = Path(os.path.abspath(os.fspath(path)))
    if not absolute.is_absolute() or len(absolute.parts) < 2:
        return absolute
    first = Path(absolute.anchor) / absolute.parts[1]
    try:
        info = first.lstat()
    except OSError:
        return absolute
    if not stat.S_ISLNK(info.st_mode):
        return absolute
    return first.resolve(strict=True).joinpath(*absolute.parts[2:])


def _open_directory_tree(path: Path, flags: int) -> int:
    absolute = _canonicalize_system_prefix(path)
    descriptor = os.open(absolute.anchor or "/", flags)
    try:
        for component in absolute.parts[1:]:
            next_fd = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_fd
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _ensure_directory_tree(path: Path) -> None:
    absolute = _canonicalize_system_prefix(path)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(absolute.anchor or "/", flags)
    try:
        for component in absolute.parts[1:]:
            try:
                os.mkdir(component, 0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            next_fd = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_fd
    except OSError as exc:
        raise ValueError(f"Workspace root cannot be created safely: {absolute}") from exc
    finally:
        os.close(descriptor)


def _save_posix(workspace_root: Path, payload: bytes, expected: _ConfigRecord) -> None:
    with _open_settings_dir(workspace_root, create=True) as settings_fd:
        if settings_fd is None:  # pragma: no cover - create=True guarantees a directory
            raise ValueError("Runtime settings directory was not created")
        temporary = f".runtime_config.{secrets.token_hex(12)}.tmp"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=settings_fd,
        )
        try:
            os.fchmod(descriptor, 0o600)
            _write_all(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            _assert_unchanged_posix(settings_fd, expected)
            os.replace(
                temporary,
                _CONFIG_NAME,
                src_dir_fd=settings_fd,
                dst_dir_fd=settings_fd,
            )
            os.fsync(settings_fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=settings_fd)
            except FileNotFoundError:
                pass


def _assert_unchanged_posix(settings_fd: int, expected: _ConfigRecord) -> None:
    try:
        descriptor = os.open(
            _CONFIG_NAME,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=settings_fd,
        )
    except FileNotFoundError:
        if expected.existed:
            raise ValueError("Runtime config changed while it was being updated")
        return
    except OSError as exc:
        raise ValueError("Runtime config changed to an unsafe file while it was being updated") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Runtime config changed to a non-regular file")
        raw = _read_fd_bounded(descriptor, MAX_CONFIG_BYTES)
    finally:
        os.close(descriptor)
    if not expected.existed or _fingerprint(info, raw) != expected.fingerprint:
        raise ValueError("Runtime config changed while it was being updated")


def _load_record_portable(workspace_root: Path, *, create_settings: bool) -> _ConfigRecord:
    path = config_path(workspace_root)
    if create_settings:
        path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        return _ConfigRecord(RuntimeConfig(), False, None)
    if path.is_symlink() or path.parent.is_symlink() or not path.is_file():
        raise ValueError("Runtime config path is unsafe")
    try:
        raw = path.read_bytes()
        info = path.stat()
    except OSError as exc:
        raise ValueError("Runtime config cannot be read") from exc
    if len(raw) > MAX_CONFIG_BYTES:
        raise ValueError("Runtime config is too large")
    return _ConfigRecord(_parse_config(raw), True, _fingerprint(info, raw))


def _save_portable(workspace_root: Path, payload: bytes, expected: _ConfigRecord) -> None:
    path = config_path(workspace_root)
    if path.parent.is_symlink() or path.is_symlink():
        raise ValueError("Runtime config path is unsafe")
    current = _load_record_portable(workspace_root, create_settings=True)
    if current.existed != expected.existed or current.fingerprint != expected.fingerprint:
        raise ValueError("Runtime config changed while it was being updated")
    temporary = path.parent / f".runtime_config.{secrets.token_hex(12)}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        _write_all(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _parse_config(raw: bytes) -> RuntimeConfig:
    if not raw:
        raise ValueError("Runtime config is empty or malformed")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Runtime config is malformed JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("Runtime config must be a JSON object")
    openai_base_url = validate_endpoint(
        str(data.get("openai_base_url") or DEFAULT_OPENAI_BASE_URL),
        "OpenAI base URL",
    )
    minimax_api_host = validate_endpoint(
        str(data.get("minimax_api_host") or DEFAULT_MINIMAX_API_HOST),
        "MiniMax API host",
    )
    provider = normalize_provider(str(data.get("vision_provider") or VisionProvider.openai.value))
    bridge_url, bridge_model = validate_bridgedeck_config(
        data.get("bridgedeck_base_url", ""), data.get("bridgedeck_model", ""),
        required=provider == VisionProvider.bridgedeck.value,
    )
    audio_executable, audio_timeout = validate_audio_adapter_config(
        data.get("audio_adapter_executable", ""),
        data.get("audio_adapter_timeout_seconds", 120),
    )
    return RuntimeConfig(
        vision_provider=provider,
        openai_api_key=str(data.get("openai_api_key") or ""),
        openai_base_url=openai_base_url,
        openai_model=str(data.get("openai_model") or "gpt-5.4-mini"),
        minimax_api_key=str(data.get("minimax_api_key") or ""),
        minimax_api_host=minimax_api_host,
        bridgedeck_base_url=bridge_url,
        bridgedeck_model=bridge_model,
        audio_adapter_executable=audio_executable,
        audio_adapter_timeout_seconds=audio_timeout,
    )


def _fingerprint(info: os.stat_result, raw: bytes) -> tuple[int, int, int, int, str]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        hashlib.sha256(raw).hexdigest(),
    )


def _read_fd_bounded(descriptor: int, maximum: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, min(65536, maximum + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > maximum:
            raise ValueError("Runtime config is too large")
    return b"".join(chunks)


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:  # pragma: no cover - defensive OS boundary
            raise OSError("short write while saving runtime config")
        view = view[written:]


def _secret_value(new_value: str, current_value: str, keep_blank: bool) -> str:
    if new_value.strip():
        return new_value.strip()
    return current_value if keep_blank else ""


def validate_endpoint(value: str, label: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{label} must be an http(s) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{label} must not contain credentials")
    if parsed.params or parsed.query or parsed.fragment:
        raise ValueError(f"{label} must not contain params, query, or fragment")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError(f"{label} must use HTTPS unless it targets loopback")
    return value.rstrip("/")


def endpoint_origin(value: str) -> str:
    parsed = urlparse(validate_endpoint(value, "Provider endpoint"))
    return f"{parsed.scheme}://{parsed.netloc}"


def validate_bridgedeck_config(
    base_url: object, model: object, *, required: bool = True
) -> tuple[str, str]:
    if type(base_url) is not str or type(model) is not str:
        raise ValueError("BridgeDeck endpoint and model must be strings")
    endpoint = base_url.strip().rstrip("/")
    selected_model = model.strip()
    if endpoint:
        parsed = urlparse(validate_endpoint(endpoint, "BridgeDeck endpoint"))
        try:
            port = parsed.port
        except ValueError:
            raise ValueError("BridgeDeck endpoint has an invalid port") from None
        if (
            parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}
            or port is None or not 1 <= port <= 65535
            or re.fullmatch(r"/accounts/[A-Za-z0-9][A-Za-z0-9._-]{0,127}/v1", parsed.path) is None
        ):
            raise ValueError("BridgeDeck requires an explicit numeric-loopback, account-scoped HTTP endpoint")
    if selected_model and (
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", selected_model) is None
        or selected_model.lower().startswith(("sk-", "ghp_"))
    ):
        raise ValueError("BridgeDeck model identifier is invalid")
    if required and (not endpoint or not selected_model):
        raise ValueError("Configure a BridgeDeck account-scoped endpoint and explicit model before selecting it")
    return endpoint, selected_model


def validate_audio_adapter_config(executable: object, timeout_seconds: object) -> tuple[str, int]:
    if type(executable) is not str:
        raise ValueError("Audio adapter executable must be a string")
    selected = executable.strip()
    if selected and ("\x00" in selected or len(selected.encode("utf-8")) > 4096 or not Path(selected).is_absolute()):
        raise ValueError("Audio adapter executable must be a bounded absolute path")
    if type(timeout_seconds) is str and timeout_seconds.isdecimal():
        timeout_seconds = int(timeout_seconds)
    if type(timeout_seconds) is not int or type(timeout_seconds) is bool or not 1 <= timeout_seconds <= 600:
        raise ValueError("Audio adapter timeout must be an integer from 1 to 600 seconds")
    return selected, timeout_seconds


def resolve_provider_key(
    config: RuntimeConfig,
    provider: str | VisionProvider,
    *,
    selected_endpoint: str | None = None,
    environment: Mapping[str, str] | None = None,
) -> str:
    """Return a key only when it is eligible for the exact provider endpoint."""
    selected_provider = normalize_provider(provider)
    if selected_provider == VisionProvider.bridgedeck.value:
        # BridgeDeck owns its local authentication; never discover or forward
        # ambient OpenAI/MiniMax credentials to this adapter.
        return ""
    env = os.environ if environment is None else environment
    if selected_provider == VisionProvider.openai.value:
        configured_endpoint = validate_endpoint(config.openai_base_url, "Configured OpenAI endpoint")
        selected = validate_endpoint(selected_endpoint or configured_endpoint, "OpenAI endpoint")
        configured_key = config.openai_api_key
        environment_key = env.get("OPENAI_API_KEY", "")
        official_origins = OFFICIAL_OPENAI_ORIGINS
    else:
        configured_endpoint = validate_endpoint(config.minimax_api_host, "Configured MiniMax endpoint")
        selected = validate_endpoint(selected_endpoint or configured_endpoint, "MiniMax endpoint")
        configured_key = config.minimax_api_key
        environment_key = env.get("MINIMAX_API_KEY", "")
        official_origins = OFFICIAL_MINIMAX_ORIGINS
    if configured_key and selected == configured_endpoint:
        return configured_key
    if endpoint_origin(selected) in official_origins:
        return environment_key
    return ""


def mask_secret(value: str) -> str:
    if not value:
        return "Not configured"
    return "Configured"
