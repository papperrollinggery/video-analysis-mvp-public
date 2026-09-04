"""Bounded optional audio-enrichment adapters over the canonical timeline."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from ._audio_intelligence_schema import canonical_json_bytes, validate_audio_timeline
from ._audio_intelligence_storage import MAX_AUDIO_INPUT_BYTES, file_receipt
from .audio_intelligence import stage_and_commit_audio_intelligence
from .audio_synthesis import audio_timeline_source
from .config import RuntimeConfig, load_runtime_config
from .paths import ProjectPaths
from .vision import _communicate_bounded

REQUEST_SCHEMA = "audio-adapter-request/v1"
RESPONSE_SCHEMA = "audio-adapter-response/v1"
RUN_RECEIPT_SCHEMA = "audio-adapter-run/v1"
MAX_ADAPTER_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_ADAPTER_REQUEST_BYTES = MAX_ADAPTER_RESPONSE_BYTES


def prepare_audio_provider_request(paths: ProjectPaths) -> dict[str, Any]:
    timeline, binding = audio_timeline_source(paths)
    if timeline is None or binding is None:
        raise ValueError("Run the deterministic audio baseline before enrichment")
    wav = file_receipt(paths.assets / "audio.wav", MAX_AUDIO_INPUT_BYTES)
    core = {
        "schema_id": REQUEST_SCHEMA,
        "project_id": paths.root.name,
        "input_binding": binding,
        "audio_wav": {"path": "assets/audio.wav", "sha256": wav["sha256"], "size_bytes": wav["size_bytes"]},
        "baseline_timeline": timeline,
        "constraints": {
            "preserve_baseline_sources_and_events": True,
            "new_sources_must_use_source_type": "adapter",
            "new_events_must_not_assert_human_review": True,
            "speaker_ids_are_anonymous_clusters": True,
        },
    }
    request = {**core, "request_id": _digest(core)}
    if len(canonical_json_bytes(request)) > MAX_ADAPTER_REQUEST_BYTES:
        raise ValueError("Audio adapter request exceeds its bounded size")
    return request


def apply_audio_provider_response(
    paths: ProjectPaths,
    request: dict[str, Any],
    response: Any,
    *,
    adapter: str,
) -> dict[str, Any]:
    if adapter != "codex-current-task":
        raise ValueError("Direct audio apply is reserved for codex-current-task")
    return _apply_audio_provider_response(
        paths,
        request,
        response,
        adapter=adapter,
        provider_called=False,
    )


def _apply_audio_provider_response(
    paths: ProjectPaths,
    request: dict[str, Any],
    response: Any,
    *,
    adapter: str,
    provider_called: bool,
) -> dict[str, Any]:
    current = prepare_audio_provider_request(paths)
    if type(request) is not dict or request != current:
        raise ValueError("Audio adapter request is stale; prepare again")
    request = current
    timeline = _validate_response(
        response,
        request,
        adapter=adapter,
        codex_current_task=not provider_called,
    )
    result = stage_and_commit_audio_intelligence(
        paths,
        timeline,
        parameters={
            "enrichment_adapter": adapter,
            "request_id": request["request_id"],
            "model_identity_verified": False,
        },
        expected_audio_wav={key: request["audio_wav"][key] for key in ("sha256", "size_bytes")},
        expected_generation_id=request["input_binding"]["generation_id"],
    )
    return {
        "schema_id": RUN_RECEIPT_SCHEMA,
        "status": "applied",
        "baseline_preserved": True,
        "provider_called": provider_called,
        "model_identity_verified": False,
        "generation_id": result["generation_id"],
        "cleanup_required": result["cleanup_required"],
    }


def run_configured_audio_adapter(
    paths: ProjectPaths,
    *,
    config: RuntimeConfig | None = None,
) -> dict[str, Any]:
    selected = config or load_runtime_config(paths.root.parent)
    if not selected.audio_adapter_executable:
        return _fallback("disabled", provider_called=False)
    try:
        executable = _validated_executable(Path(selected.audio_adapter_executable))
    except ValueError:
        return _fallback("missing_or_unsafe", provider_called=False)
    try:
        request = prepare_audio_provider_request(paths)
    except (OSError, ValueError):
        return _fallback("baseline_unavailable", provider_called=False)
    payload = canonical_json_bytes(request)
    with tempfile.TemporaryDirectory(prefix=".vew-audio-adapter-") as private:
        environment = {
            "PATH": os.pathsep.join((str(executable.parent), str(Path(sys.executable).parent), "/usr/bin", "/bin")),
            "HOME": private,
            "TMPDIR": private,
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PYTHONIOENCODING": "utf-8",
        }
        if os.name == "nt" and os.getenv("SystemRoot"):  # pragma: no cover
            environment["SystemRoot"] = os.environ["SystemRoot"]
        try:
            process = subprocess.Popen(
                [str(executable), str(paths.assets / "audio.wav")],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                start_new_session=os.name == "posix",
            )
        except OSError:
            return _fallback("missing_or_unsafe", provider_called=False)
        try:
            stdout, _stderr = _communicate_bounded(
                process,
                payload,
                timeout=selected.audio_adapter_timeout_seconds,
                maximum=MAX_ADAPTER_RESPONSE_BYTES,
                input_maximum=MAX_ADAPTER_REQUEST_BYTES,
            )
        except RuntimeError as exc:
            message = str(exc)
            reason = "timeout" if "timed out" in message else "output_limit" if "exceeds" in message else "crash"
            return _fallback(reason, provider_called=True)
    try:
        response = json.loads(stdout.decode("utf-8"))
        return _apply_audio_provider_response(
            paths,
            request,
            response,
            adapter=executable.name,
            provider_called=True,
        )
    except (UnicodeError, json.JSONDecodeError, OSError, ValueError):
        return _fallback("invalid_response", provider_called=True)


def audio_adapter_capability(workspace_root: Path) -> tuple[bool, str]:
    try:
        config = load_runtime_config(workspace_root)
    except ValueError:
        return False, "configuration invalid"
    if not config.audio_adapter_executable:
        return False, "disabled; deterministic baseline remains available"
    try:
        _validated_executable(Path(config.audio_adapter_executable))
    except ValueError:
        return False, "configured executable is missing or unsafe"
    return True, f"configured with {config.audio_adapter_timeout_seconds}s timeout; execution not yet verified"


def _validate_response(
    response: Any,
    request: dict[str, Any],
    *,
    adapter: str,
    codex_current_task: bool,
) -> dict[str, Any]:
    if type(response) is not dict or set(response) != {"schema_id", "request_id", "timeline"}:
        raise ValueError("Audio adapter response fields are invalid")
    if len(canonical_json_bytes(response)) > MAX_ADAPTER_RESPONSE_BYTES:
        raise ValueError("Audio adapter response exceeds its bounded size")
    if response["schema_id"] != RESPONSE_SCHEMA or response["request_id"] != request["request_id"]:
        raise ValueError("Audio adapter response binding is invalid")
    timeline = validate_audio_timeline(response["timeline"])
    baseline = request["baseline_timeline"]
    if timeline["media_duration_seconds"] != baseline["media_duration_seconds"]:
        raise ValueError("Audio adapter changed media duration")
    baseline_sources = {item["source_id"]: item for item in baseline["sources"]}
    current_sources = {item["source_id"]: item for item in timeline["sources"]}
    if any(current_sources.get(key) != value for key, value in baseline_sources.items()):
        raise ValueError("Audio adapter changed baseline sources")
    baseline_events = {item["event_id"]: item for item in baseline["events"]}
    current_events = {item["event_id"]: item for item in timeline["events"]}
    if any(current_events.get(key) != value for key, value in baseline_events.items()):
        raise ValueError("Audio adapter changed baseline events")
    new_sources = [item for key, item in current_sources.items() if key not in baseline_sources]
    new_events = [item for key, item in current_events.items() if key not in baseline_events]
    if not new_sources or not new_events:
        raise ValueError("Audio adapter produced no enrichment")
    if any(item["source_type"] != "adapter" or item["adapter"] != adapter for item in new_sources):
        raise ValueError("Audio adapter source identity is invalid")
    if codex_current_task and any(
        item["engine"] != "host-managed-unverified"
        or item["engine_version"] is not None
        or item["model"] != "host-managed-unverified"
        or item["device"] is not None
        for item in new_sources
    ):
        raise ValueError("Codex audio source model identity must remain unverified")
    new_source_ids = {item["source_id"] for item in new_sources}
    if any(item["source_id"] not in new_source_ids or item["review"] is not None for item in new_events):
        raise ValueError("Audio adapter event provenance is invalid")
    return timeline


def _validated_executable(path: Path) -> Path:
    if not path.is_absolute():
        raise ValueError("Audio adapter executable must be absolute")
    try:
        info = path.lstat()
    except OSError as exc:
        raise ValueError("Audio adapter executable is unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or not os.access(path, os.X_OK):
        raise ValueError("Audio adapter executable is unsafe")
    return path


def _fallback(reason: str, *, provider_called: bool) -> dict[str, Any]:
    return {
        "schema_id": RUN_RECEIPT_SCHEMA,
        "status": "fallback",
        "reason": reason,
        "baseline_preserved": True,
        "provider_called": provider_called,
        "model_identity_verified": False,
    }


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()
