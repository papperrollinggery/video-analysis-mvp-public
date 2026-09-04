from __future__ import annotations

import json
import locale
import os
import selectors
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import wave
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit, urlunsplit


class ToolError(RuntimeError):
    pass


class ProcessCancelledError(RuntimeError):
    def __init__(self, message: str, *, cleanup_verified: bool | None = None) -> None:
        super().__init__(message)
        self.cleanup_verified = cleanup_verified


SENSITIVE_COMMAND_FLAGS = frozenset({"--video-password"})
REDACTED = "[REDACTED]"
MAX_COMMAND_OUTPUT_BYTES = 8 * 1024 * 1024
_COMMAND_OUTPUT_CHUNK_BYTES = 64 * 1024
_PROCESS_TERMINATION_GRACE_SECONDS = 0.2
_PROCESS_GROUP_SIGNAL_ATTEMPTS = 3
_PROCESS_GROUP_SIGNAL_RETRY_SECONDS = 0.01
_PROCESS_CANCELLATION = threading.local()


class _CommandOutputLimitExceeded(RuntimeError):
    def __init__(self, stdout: bytes, stderr: bytes) -> None:
        self.stdout = stdout
        self.stderr = stderr


@contextmanager
def process_cancellation(cancelled: Callable[[], bool]) -> Iterator[None]:
    previous = getattr(_PROCESS_CANCELLATION, "callback", None)
    _PROCESS_CANCELLATION.callback = cancelled
    try:
        yield
    finally:
        if previous is None:
            try:
                del _PROCESS_CANCELLATION.callback
            except AttributeError:
                pass
        else:
            _PROCESS_CANCELLATION.callback = previous


def require_tool(name: str) -> str:
    local_tool = Path(sys.executable).parent / name
    if local_tool.exists() and _tool_works(str(local_tool)):
        return str(local_tool)
    path = shutil.which(name)
    if path and _tool_works(path):
        return path
    raise ToolError(f"Required tool not found or not executable: {name}")


def _tool_works(path: str) -> bool:
    for flag in ["--version", "-version"]:
        try:
            result = run_command([path, flag], timeout=20)
        except Exception:
            continue
        return result.returncode == 0
    return False


def run_command(
    args: Sequence[str],
    timeout: int | None = None,
    *,
    sensitive_values: Sequence[str] = (),
    environment: Mapping[str, str] | None = None,
    cancelled: Callable[[], bool] | None = None,
    max_output_bytes: int | None = None,
) -> subprocess.CompletedProcess[str]:
    command = list(args)
    output_limit = MAX_COMMAND_OUTPUT_BYTES if max_output_bytes is None else max_output_bytes
    effective_cancelled = cancelled or getattr(_PROCESS_CANCELLATION, "callback", None)
    replacements = _sensitive_command_replacements(command)
    replacements.update(_explicit_sensitive_replacements(sensitive_values))
    try:
        result = _run_bounded_process(
            command,
            timeout=timeout,
            environment=environment,
            cancelled=effective_cancelled,
            max_output_bytes=output_limit,
        )
    except subprocess.TimeoutExpired as exc:
        message = f"Command timed out: {_format_command(command)}"
        stderr = _redact_text(_output_text(exc.stderr).strip(), replacements)
        if stderr:
            message = f"{message}\n{stderr}"
        if getattr(exc, "process_group_cleanup_failed", False):
            message = f"{message}\nProcess-group cleanup could not be verified."
        raise ToolError(message) from None
    except _CommandOutputLimitExceeded as exc:
        message = (
            f"Command output exceeded {output_limit} bytes: "
            f"{_format_command(command)}"
        )
        stderr = _redact_text(_output_text(exc.stderr).strip(), replacements)
        if stderr:
            message = f"{message}\n{stderr}"
        if getattr(exc, "process_group_cleanup_failed", False):
            message = f"{message}\nProcess-group cleanup could not be verified."
        raise ToolError(message) from None
    if result.returncode != 0:
        stderr = _redact_text(result.stderr.strip(), replacements)
        message = f"Command failed: {_format_command(command)}"
        if stderr:
            message = f"{message}\n{stderr}"
        raise ToolError(message)
    return result


def _run_bounded_process(
    command: list[str],
    *,
    timeout: int | None,
    environment: Mapping[str, str] | None = None,
    cancelled: Callable[[], bool] | None = None,
    max_output_bytes: int = MAX_COMMAND_OUTPUT_BYTES,
) -> subprocess.CompletedProcess[str]:
    if max_output_bytes <= 0:
        raise ValueError("max_output_bytes must be positive")
    if cancelled is not None and cancelled():
        raise ProcessCancelledError(
            "Subprocess cancelled before launch", cleanup_verified=True
        )
    popen_options: dict[str, object] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": False,
    }
    if environment is not None:
        popen_options["env"] = dict(environment)
    if os.name == "posix":
        popen_options["start_new_session"] = True
    elif hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):  # pragma: no cover - POSIX project
        popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

    process = subprocess.Popen(command, **popen_options)
    if process.stdout is None or process.stderr is None:  # pragma: no cover - fixed Popen options
        _terminate_process_group(process)
        raise RuntimeError("subprocess output pipes were not created")

    stdout = bytearray()
    stderr = bytearray()
    streams = {process.stdout: stdout, process.stderr: stderr}
    selector = selectors.DefaultSelector()
    deadline = None if timeout is None else time.monotonic() + timeout
    try:
        for stream in streams:
            selector.register(stream, selectors.EVENT_READ)
        while selector.get_map():
            if cancelled is not None and cancelled():
                raise ProcessCancelledError("Subprocess cancelled by local operator")
            wait_seconds = None if deadline is None else max(0.0, deadline - time.monotonic())
            if wait_seconds == 0.0:
                raise subprocess.TimeoutExpired(
                    command,
                    timeout,
                    output=bytes(stdout),
                    stderr=bytes(stderr),
                )
            selected_wait = min(wait_seconds, 0.1) if cancelled is not None and wait_seconds is not None else (
                0.1 if cancelled is not None else wait_seconds
            )
            events = selector.select(selected_wait)
            if not events:
                if deadline is not None and time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(
                        command,
                        timeout,
                        output=bytes(stdout),
                        stderr=bytes(stderr),
                    )
                continue
            for key, _mask in events:
                remaining = max(0, max_output_bytes - len(stdout) - len(stderr))
                try:
                    chunk = os.read(
                        key.fd,
                        min(_COMMAND_OUTPUT_CHUNK_BYTES, remaining + 1),
                    )
                except BlockingIOError:  # pragma: no cover - selector race
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                target = streams[key.fileobj]
                if len(chunk) > remaining:
                    target.extend(chunk[:remaining])
                    raise _CommandOutputLimitExceeded(bytes(stdout), bytes(stderr))
                target.extend(chunk)

        while True:
            if cancelled is not None and cancelled():
                raise ProcessCancelledError("Subprocess cancelled by local operator")
            wait_seconds = None if deadline is None else max(0.0, deadline - time.monotonic())
            if wait_seconds == 0.0:
                raise subprocess.TimeoutExpired(
                    command,
                    timeout,
                    output=bytes(stdout),
                    stderr=bytes(stderr),
                )
            selected_wait = min(wait_seconds, 0.1) if cancelled is not None and wait_seconds is not None else (
                0.1 if cancelled is not None else wait_seconds
            )
            try:
                return_code = process.wait(timeout=selected_wait)
                break
            except subprocess.TimeoutExpired:
                if deadline is not None and time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(
                        command,
                        timeout,
                        output=bytes(stdout),
                        stderr=bytes(stderr),
                    ) from None
    except (subprocess.TimeoutExpired, _CommandOutputLimitExceeded) as exc:
        if not _terminate_process_group(process):
            exc.process_group_cleanup_failed = True
        raise
    except BaseException as exc:
        cleanup_verified = _terminate_process_group(process)
        if isinstance(exc, ProcessCancelledError):
            exc.cleanup_verified = cleanup_verified
        raise
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()

    return subprocess.CompletedProcess(
        command,
        return_code,
        stdout=_decode_process_output(stdout),
        stderr=_decode_process_output(stderr),
    )


def _terminate_process_group(process: subprocess.Popen[bytes]) -> bool:
    group_cleanup_verified = True
    if os.name == "posix":
        _signal_process_group(process, signal.SIGTERM)
        try:
            process.wait(timeout=_PROCESS_TERMINATION_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            pass
        group_cleanup_verified = _signal_process_group(process, signal.SIGKILL)
    else:  # pragma: no cover - POSIX project
        try:
            process.terminate()
        except OSError:
            pass
        try:
            process.wait(timeout=_PROCESS_TERMINATION_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass
    try:
        process.wait(timeout=_PROCESS_TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        return False
    except OSError:  # pragma: no cover - already reaped by a platform wrapper
        return process.poll() is not None and group_cleanup_verified
    return group_cleanup_verified


def _signal_process_group(process: subprocess.Popen[bytes], sig: signal.Signals) -> bool:
    for attempt in range(_PROCESS_GROUP_SIGNAL_ATTEMPTS):
        try:
            os.killpg(process.pid, sig)
            return True
        except ProcessLookupError:
            return True
        except PermissionError:
            # macOS can report EPERM while a terminated group is disappearing.
            # Stop the leader from creating more descendants, then retry the
            # group signal before declaring cleanup unverifiable.
            if process.poll() is None:
                try:
                    process.kill()
                except (ProcessLookupError, PermissionError):
                    pass
            if attempt + 1 < _PROCESS_GROUP_SIGNAL_ATTEMPTS:
                time.sleep(_PROCESS_GROUP_SIGNAL_RETRY_SECONDS)
    return False


def _decode_process_output(value: bytearray) -> str:
    encoding = locale.getpreferredencoding(False) or "utf-8"
    return bytes(value).decode(encoding, errors="replace").replace("\r\n", "\n").replace("\r", "\n")


def _sensitive_command_replacements(args: Sequence[str]) -> dict[str, str]:
    replacements: dict[str, str] = {}
    redact_next = False
    for arg in args:
        if redact_next:
            if arg:
                replacements[arg] = REDACTED
            redact_next = False
            continue
        if arg in SENSITIVE_COMMAND_FLAGS:
            redact_next = True
            continue
        for flag in SENSITIVE_COMMAND_FLAGS:
            prefix = f"{flag}="
            if arg.startswith(prefix) and arg[len(prefix) :]:
                replacements[arg[len(prefix) :]] = REDACTED
                break
        sanitized = sanitize_url_for_storage(arg, reject_userinfo=False)
        if sanitized != arg:
            replacements[arg] = sanitized
            _add_sensitive_url_components(arg, replacements)
    return replacements


def _explicit_sensitive_replacements(values: Sequence[str]) -> dict[str, str]:
    replacements: dict[str, str] = {}
    for value in values:
        if not value:
            continue
        sanitized = sanitize_url_for_storage(value, reject_userinfo=False)
        replacements[value] = sanitized if sanitized != value else REDACTED
        if sanitized == value:
            continue
        _add_sensitive_url_components(value, replacements)
    return replacements


def _add_sensitive_url_components(value: str, replacements: dict[str, str]) -> None:
    try:
        parsed = urlsplit(value)
        if parsed.query:
            replacements[parsed.query] = REDACTED
            for raw_field in parsed.query.split("&"):
                raw_key, separator, raw_component = raw_field.partition("=")
                if raw_key:
                    replacements[raw_key] = REDACTED
                if separator and raw_component:
                    replacements[raw_component] = REDACTED
            for key, component in parse_qsl(parsed.query, keep_blank_values=True):
                if key:
                    replacements[key] = REDACTED
                if component:
                    replacements[component] = REDACTED
        if parsed.fragment:
            replacements[parsed.fragment] = REDACTED
            decoded_fragment = unquote(parsed.fragment)
            if decoded_fragment:
                replacements[decoded_fragment] = REDACTED
    except ValueError:
        pass


def _format_command(args: Sequence[str]) -> str:
    redacted: list[str] = []
    redact_next = False
    for arg in args:
        if redact_next:
            redacted.append(REDACTED)
            redact_next = False
            continue
        if arg in SENSITIVE_COMMAND_FLAGS:
            redacted.append(arg)
            redact_next = True
            continue
        replaced = sanitize_url_for_storage(arg, reject_userinfo=False)
        for flag in SENSITIVE_COMMAND_FLAGS:
            prefix = f"{flag}="
            if arg.startswith(prefix):
                replaced = f"{prefix}{REDACTED}"
                break
        redacted.append(replaced)
    return shlex.join(redacted)


def _redact_text(value: str, replacements: dict[str, str]) -> str:
    for secret in sorted(replacements, key=len, reverse=True):
        value = value.replace(secret, replacements[secret])
    return value


def sanitize_url_for_storage(value: str, *, reject_userinfo: bool = True) -> str:
    """Return an http(s) URL without credentials, query, or fragment.

    Non-URL strings are returned unchanged so this helper can safely be used by
    command rendering and recursive metadata sanitization.
    """
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return value
    if parsed.username is not None or parsed.password is not None:
        if reject_userinfo:
            raise ValueError("Source URL must not contain userinfo credentials")
        hostname = parsed.hostname or ""
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        netloc = hostname
        try:
            port = parsed.port
        except ValueError:
            port = None
        if port is not None:
            netloc = f"{netloc}:{port}"
    else:
        netloc = parsed.netloc
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _output_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


def run_json(
    args: Sequence[str],
    timeout: int | None = None,
    *,
    sensitive_values: Sequence[str] = (),
) -> dict:
    result = run_command(args, timeout=timeout, sensitive_values=sensitive_values)
    return json.loads(result.stdout)


def rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def format_time(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    ms = total_ms % 1000
    total_s = total_ms // 1000
    s = total_s % 60
    total_m = total_s // 60
    m = total_m % 60
    h = total_m // 60
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def format_clock(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    s = total % 60
    m = (total // 60) % 60
    h = total // 3600
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def read_wav_energy(path: Path, window_seconds: float = 0.25) -> list[tuple[float, float]]:
    with wave.open(str(path), "rb") as wf:
        channels = wf.getnchannels()
        sample_width = wf.getsampwidth()
        sample_rate = wf.getframerate()
        frame_count = wf.getnframes()
        window_frames = max(1, int(sample_rate * window_seconds))
        energies: list[tuple[float, float]] = []
        pos = 0
        max_possible = float(2 ** (8 * sample_width - 1))
        while pos < frame_count:
            raw = wf.readframes(window_frames)
            if not raw:
                break
            count = len(raw) // sample_width
            if count == 0:
                break
            total = 0.0
            samples = 0
            for offset in range(0, len(raw), sample_width * channels):
                for ch in range(channels):
                    start = offset + ch * sample_width
                    chunk = raw[start : start + sample_width]
                    if len(chunk) != sample_width:
                        continue
                    value = int.from_bytes(chunk, "little", signed=True)
                    total += abs(value) / max_possible
                    samples += 1
            energies.append((pos / sample_rate, total / max(samples, 1)))
            pos += window_frames
    return energies
