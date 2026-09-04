from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .config import (
    DEFAULT_MINIMAX_API_HOST,
    DEFAULT_OPENAI_BASE_URL,
    RuntimeConfig,
    VisionProvider,
    endpoint_origin,
    load_runtime_config,
    normalize_provider,
    resolve_provider_key,
    validate_endpoint,
    validate_bridgedeck_config,
)
from .bridge_vision import BRIDGE_PROVIDER_CONTRACT, BridgeDeckError, analyze_bridgedeck_image
from .image_evidence import MAX_IMAGE_BYTES, inspect_image_bytes
from .paths import ProjectPaths
from .safe_io import advisory_file_lock
from .schemas import CanonicalMediaPackage, Shot, StatusEnvelope, dump_json, load_json


VISION_SYSTEM_PROMPT = """You are a professional video evidence analyst.
Analyze one frame from a video shot. Return ONLY valid JSON.
Do not invent lens or equipment when it is not visually inferable.
Use concise production vocabulary. Keep observations distinct from interpretation and make the result reviewable at shot level.
"""

OBSERVATION_FIELDS = [
    "content_summary",
    "content_summary_zh",
    "shot_scale",
    "camera_angle",
    "camera_motion",
    "composition",
    "subject",
    "subject_zh",
    "action",
    "action_zh",
    "location",
    "int_ext",
    "props",
    "onscreen_text",
    "lighting_vfx",
    "style_notes",
    "style_notes_zh",
]
ADS_INTERPRETATION_FIELDS = [
    "scene_type",
    "story_beat",
    "remake_notes",
    "remake_notes_zh",
    "prompt_en",
    "prompt_zh",
]
MINIMAX_MCP_PACKAGE = "minimax-coding-plan-mcp==0.0.4"
MINIMAX_MCP_VERSION = "0.0.4"
MINIMAX_MCP_EXECUTABLE = "minimax-coding-plan-mcp"
VISION_RECEIPT_SCHEMA_VERSION = "1.0"
MAX_FRAME_BYTES = MAX_IMAGE_BYTES
MAX_PROVIDER_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_MCP_INPUT_BYTES = 4 * 1024 * 1024
MAX_MCP_OUTPUT_BYTES = 2 * 1024 * 1024
MINIMAX_TIMEOUT_SECONDS = 180
MINIMAX_DRAIN_GRACE_SECONDS = 1.0
OPENAI_TIMEOUT_SECONDS = 120
OPENAI_MAX_OUTPUT_TOKENS = 2000


@dataclass(frozen=True)
class FrameInput:
    reference: str
    data: bytes
    sha256: str
    size_bytes: int
    media_type: str
    width: int
    height: int

    def receipt(self, shot_id: str) -> dict[str, Any]:
        return {
            "shot_id": shot_id,
            "frame_ref": self.reference,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "media_type": self.media_type,
            "width": self.width,
            "height": self.height,
        }


def annotate_project_with_vision(
    paths: ProjectPaths,
    model: str | None = None,
    base_url: str | None = None,
    limit: int | None = None,
    provider: str | None = None,
) -> StatusEnvelope:
    _require_positive_limit(limit)
    runtime_config = load_runtime_config(paths.root.parent)
    selected_provider = normalize_provider(
        provider
        if provider is not None
        else os.getenv("VIDEO_ANALYSIS_VISION_PROVIDER") or runtime_config.vision_provider
    )
    if selected_provider == VisionProvider.minimax_mcp.value:
        return annotate_project_with_minimax_mcp(paths, limit=limit, runtime_config=runtime_config)
    if selected_provider == VisionProvider.bridgedeck.value:
        return annotate_project_with_bridgedeck(
            paths, model=model, base_url=base_url, limit=limit, runtime_config=runtime_config
        )

    endpoint_base = validate_endpoint(base_url or runtime_config.openai_base_url, "OpenAI base URL")
    api_key = resolve_provider_key(
        runtime_config,
        VisionProvider.openai,
        selected_endpoint=endpoint_base,
    )
    if not api_key:
        return StatusEnvelope(
            status="error",
            summary="Vision annotation requires an OpenAI key bound to the selected endpoint.",
            next_actions=[
                "Configure an OpenAI key in this workbench. Ambient OPENAI_API_KEY is accepted only for api.openai.com."
            ],
            artifacts={"shots": str(paths.data / "shots.json")},
            error="No OpenAI key is bound to the selected endpoint",
        )
    selected_model = model or runtime_config.openai_model
    profile = _project_profile(paths)
    shots = [Shot.model_validate(item) for item in load_json(paths.data / "shots.json")]
    selected = shots[:limit] if limit is not None else shots
    return _annotate_selected_shots(
        paths,
        shots,
        selected,
        provider=VisionProvider.openai.value,
        provider_source="openai_chat_completions",
        provider_label="OpenAI vision",
        model=selected_model,
        endpoint=endpoint_base,
        adapter_version=None,
        analyze=lambda frame, shot: analyze_frame(
            frame,
            shot,
            api_key,
            model=selected_model,
            base_url=endpoint_base,
            profile=profile,
        ),
        apply=lambda shot, data: apply_vision_data(
            shot,
            data,
            profile=profile,
            annotation_source=VisionProvider.openai.value,
        ),
        next_action="Regenerate the report to refresh the shot evidence tables.",
    )


def annotate_project_with_bridgedeck(
    paths: ProjectPaths,
    model: str | None = None,
    base_url: str | None = None,
    limit: int | None = None,
    *,
    runtime_config: RuntimeConfig | None = None,
) -> StatusEnvelope:
    _require_positive_limit(limit)
    config = runtime_config or load_runtime_config(paths.root.parent)
    endpoint, selected_model = validate_bridgedeck_config(
        base_url or config.bridgedeck_base_url, model or config.bridgedeck_model
    )
    profile = _project_profile(paths)
    shots = [Shot.model_validate(item) for item in load_json(paths.data / "shots.json")]
    selected = shots[:limit] if limit is not None else shots
    required_fields = _required_fields(profile)
    return _annotate_selected_shots(
        paths, shots, selected,
        provider=VisionProvider.bridgedeck.value,
        provider_source="bridgedeck_responses",
        provider_label="BridgeDeck Responses",
        model=selected_model,
        endpoint=endpoint,
        adapter_version=None,
        analyze=lambda frame, shot: analyze_bridgedeck_image(
            base_url=endpoint, model=selected_model,
            image_bytes=frame.data, media_type=frame.media_type,
            instructions=VISION_SYSTEM_PROMPT,
            prompt={"shot_no": shot.shot_no, "timecode": shot.timecode, "analysis_profile": profile, "required_json_fields": required_fields, "notes": _profile_notes(profile)},
            required_fields=required_fields,
        ),
        apply=lambda shot, data: apply_vision_data(
            shot, data, profile=profile, annotation_source=VisionProvider.bridgedeck.value
        ),
        next_action="Review the observations before Finalize. BridgeDeck owns authentication and may ignore upstream token limits; only client timeout and response-size limits are enforced here.",
        provider_contract=BRIDGE_PROVIDER_CONTRACT,
    )


def annotate_project_with_minimax_mcp(
    paths: ProjectPaths,
    limit: int | None = None,
    *,
    runtime_config: RuntimeConfig | None = None,
) -> StatusEnvelope:
    _require_positive_limit(limit)
    config = runtime_config or load_runtime_config(paths.root.parent)
    # Reject a corrupted/unknown provider even when this function is called directly.
    normalize_provider(VisionProvider.minimax_mcp.value)
    host = validate_endpoint(config.minimax_api_host or DEFAULT_MINIMAX_API_HOST, "MiniMax API host")
    api_key = resolve_provider_key(
        config,
        VisionProvider.minimax_mcp,
        selected_endpoint=host,
    )
    if not api_key:
        return StatusEnvelope(
            status="error",
            summary="MiniMax MCP vision requires a key bound to the selected endpoint.",
            next_actions=[
                "Configure a MiniMax key in this workbench. Ambient MINIMAX_API_KEY is accepted only for official MiniMax hosts.",
                "Install and verify minimax-coding-plan-mcp==0.0.4 before rerunning vision.",
            ],
            artifacts={"shots": str(paths.data / "shots.json")},
            error="No MiniMax key is bound to the selected endpoint",
        )
    executable, adapter_version = _prepare_minimax_mcp()
    profile = _project_profile(paths)
    shots = [Shot.model_validate(item) for item in load_json(paths.data / "shots.json")]
    selected = shots[:limit] if limit is not None else shots

    def apply_minimax_data(shot: Shot, data: dict[str, Any]) -> None:
        apply_vision_data(
            shot,
            data,
            profile=profile,
            annotation_source=VisionProvider.minimax_mcp.value,
        )
        shot.review_notes = "MiniMax MCP vision annotated; verify against source before final evidence use"

    return _annotate_selected_shots(
        paths,
        shots,
        selected,
        provider=VisionProvider.minimax_mcp.value,
        provider_source="minimax_mcp_understand_image",
        provider_label="MiniMax MCP vision",
        model="provider-managed",
        endpoint=host,
        adapter_version=adapter_version,
        analyze=lambda frame, shot: analyze_frame_with_minimax_mcp(
            frame,
            shot,
            api_key,
            host,
            profile=profile,
            executable=executable,
        ),
        apply=apply_minimax_data,
        next_action="Regenerate the report to refresh shot tables.",
    )


def _require_positive_limit(limit: int | None) -> None:
    if limit is not None and (isinstance(limit, bool) or limit <= 0):
        raise ValueError("Vision annotation limit must be greater than zero.")


def _annotate_selected_shots(
    paths: ProjectPaths,
    shots: list[Shot],
    selected: list[Shot],
    *,
    provider: str,
    provider_source: str,
    provider_label: str,
    model: str,
    endpoint: str,
    adapter_version: str | None,
    analyze: Callable[[FrameInput, Shot], dict[str, Any]],
    apply: Callable[[Shot, dict[str, Any]], None],
    next_action: str,
    before_commit: Callable[[], None] | None = None,
    agent_submission: dict[str, Any] | None = None,
    provider_contract: dict[str, Any] | None = None,
) -> StatusEnvelope:
    provider = _normalize_annotation_source(provider)
    started_at = _utc_now()
    run_id = str(uuid.uuid4())
    selected_ids = [_shot_reference(shot) for shot in selected]
    if len(selected_ids) != len(set(selected_ids)):
        raise ValueError("Shot ids must be unique before vision annotation")
    shot_receipts = [
        {
            "shot_id": _shot_reference(shot),
            "shot_sha256": canonical_shot_digest(shot),
            "frame_sha256": None,
        }
        for shot in selected
    ]
    shot_receipt_by_id = {item["shot_id"]: item for item in shot_receipts}
    proposals: dict[str, Shot] = {}
    diagnostics: list[str] = []
    frame_receipts: list[dict[str, Any]] = []

    for shot in selected:
        shot_id = _shot_reference(shot)
        exclusion = _provider_exclusion_reason(shot)
        if exclusion:
            diagnostics.append(_skip_diagnostic(shot, exclusion))
            continue
        try:
            frame = _read_project_frame(paths, shot.frame_ref)
        except ValueError as exc:
            diagnostics.append(_skip_diagnostic(shot, str(exc)))
            continue
        frame_receipts.append(frame.receipt(shot_id))
        shot_receipt_by_id[shot_id]["frame_sha256"] = frame.sha256

        updated_shot = Shot.model_validate(shot.model_dump(mode="json"))
        try:
            data = validate_vision_payload(analyze(frame, updated_shot), profile=_project_profile(paths))
            apply(updated_shot, data)
        except Exception as exc:
            detail = str(exc) if provider == VisionProvider.bridgedeck.value and isinstance(exc, BridgeDeckError) else f"provider analysis failed ({type(exc).__name__})"
            diagnostics.append(
                _skip_diagnostic(shot, detail)
            )
            continue

        proposals[shot_id] = updated_shot

    annotations: list[Shot] = []
    annotated_ids: list[str] = []
    initial_digest_by_id = {
        _shot_reference(shot): canonical_shot_digest(shot)
        for shot in selected
    }
    with advisory_file_lock(paths.data / ".shots.lock", root=paths.root):
        if before_commit is not None:
            before_commit()
        current_shots = [
            Shot.model_validate(item)
            for item in load_json(paths.data / "shots.json")
        ]
        current_indexes: dict[str, int] = {}
        for index, current in enumerate(current_shots):
            current_id = _shot_reference(current)
            if current_id in current_indexes:
                raise ValueError("Shot ids must remain unique during vision annotation")
            current_indexes[current_id] = index

        for shot in selected:
            shot_id = _shot_reference(shot)
            proposal = proposals.get(shot_id)
            if proposal is None:
                continue
            current_index = current_indexes.get(shot_id)
            if current_index is None:
                diagnostics.append(_skip_diagnostic(shot, "shot was removed while provider analysis was running"))
                continue
            current = current_shots[current_index]
            current_exclusion = _provider_exclusion_reason(current)
            if current_exclusion:
                diagnostics.append(_skip_diagnostic(current, current_exclusion))
                continue
            if canonical_shot_digest(current) != initial_digest_by_id[shot_id]:
                diagnostics.append(
                    _skip_diagnostic(current, "shot changed while provider analysis was running")
                )
                continue
            current_shots[current_index] = proposal
            annotations.append(proposal)
            annotated_ids.append(shot_id)
            # Bind successful receipts to the exact post-provider shot that was
            # merged into the latest on-disk table.
            shot_receipt_by_id[shot_id]["shot_sha256"] = canonical_shot_digest(proposal)

        annotated_id_set = set(annotated_ids)
        skipped_ids = [shot_id for shot_id in selected_ids if shot_id not in annotated_id_set]
        selected_count = len(selected)
        annotated_count = len(annotations)
        skipped_count = selected_count - annotated_count
        # A failed/invalid provider run, or a stale compare-and-swap, must not
        # rewrite shots.json merely by serializing an old snapshot.
        if annotations:
            dump_json(paths.data / "shots.json", current_shots)
        receipt = {
            "schema_version": VISION_RECEIPT_SCHEMA_VERSION,
            "run_id": run_id,
            "started_at": started_at,
            "completed_at": _utc_now(),
            "provider": provider,
            "provider_source": provider_source,
            "model": model,
            "endpoint_origin": "codex-current-task" if provider == "codex" else endpoint_origin(endpoint),
            "adapter": (
                {"package": "minimax-coding-plan-mcp", "version": adapter_version}
                if adapter_version is not None
                else None
            ),
            "selected_shot_ids": selected_ids,
            "annotated_shot_ids": annotated_ids,
            "skipped_shot_ids": skipped_ids,
            "diagnostics": diagnostics,
            "media_binding": _media_binding(paths),
            "shot_receipts": shot_receipts,
            "input_frames": frame_receipts,
            "annotations": [item.model_dump(mode="json") for item in annotations],
        }
        if provider == "codex":
            receipt["agent_submission"] = agent_submission
        if provider == VisionProvider.bridgedeck.value:
            receipt["provider_contract"] = provider_contract
        dump_json(paths.data / "vision_annotations.json", receipt)

    status = "success" if selected_count > 0 and skipped_count == 0 else "warning"
    next_actions = [next_action]
    if skipped_count:
        next_actions.insert(
            0,
            "Review diagnostics, fix skipped frames or provider failures, and rerun vision annotation.",
        )
    if selected_count == 0:
        next_actions.insert(0, "Add shots with valid keyframes before running vision annotation.")
    return StatusEnvelope(
        status=status,
        summary=(
            f"{provider_label}: annotated {annotated_count} of {selected_count} selected shots; "
            f"skipped {skipped_count}."
        ),
        next_actions=next_actions,
        artifacts={
            "shots": str(paths.data / "shots.json"),
            "vision_annotations": str(paths.data / "vision_annotations.json"),
        },
        diagnostics=diagnostics,
    )


def _provider_exclusion_reason(shot: Shot) -> str | None:
    if (shot.annotation_source or "").strip().lower() == "human":
        return "human annotation is protected from provider overwrite"
    if (shot.readiness_status or "").strip().lower() == "rejected":
        return "rejected shot is excluded from provider submission"
    return None


def _normalize_annotation_source(value: str) -> str:
    if value == "codex":
        return value
    return normalize_provider(value)


def _read_project_frame(paths: ProjectPaths, frame_ref: str) -> FrameInput:
    reference = str(frame_ref or "").strip()
    if not reference:
        raise ValueError("frame_ref is empty")
    relative = Path(reference)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("frame_ref escapes the keyframes directory")
    try:
        with _open_relative_regular(paths.keyframes, relative) as descriptor:
            return _frame_from_fd(descriptor, reference)
    except FileNotFoundError as exc:
        raise ValueError("frame file is missing") from exc
    except IsADirectoryError as exc:
        raise ValueError("frame_ref is not a regular file") from exc
    except OSError as exc:
        raise ValueError("frame_ref cannot be opened without following symlinks") from exc


@contextmanager
def _open_relative_regular(root: Path, relative: Path) -> Iterator[int]:
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_fd = _open_directory_tree(root, directory_flags)
    try:
        for component in relative.parts[:-1]:
            next_fd = os.open(component, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        file_fd = os.open(
            relative.parts[-1],
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_fd,
        )
        try:
            if not stat.S_ISREG(os.fstat(file_fd).st_mode):
                raise IsADirectoryError(relative)
            yield file_fd
        finally:
            os.close(file_fd)
    finally:
        os.close(directory_fd)


def _open_directory_tree(path: Path, flags: int) -> int:
    absolute = Path(os.path.abspath(os.fspath(path)))
    if os.name != "posix":  # pragma: no cover - exercised by Windows CI
        return os.open(absolute, flags)
    parts = list(absolute.parts)
    if len(parts) > 1:
        first = Path(absolute.anchor) / parts[1]
        try:
            info = first.lstat()
        except OSError:
            info = None
        if info is not None and stat.S_ISLNK(info.st_mode):
            resolved = first.resolve(strict=True)
            absolute = resolved.joinpath(*parts[2:])
            parts = list(absolute.parts)
    descriptor = os.open(absolute.anchor or "/", flags)
    try:
        for component in parts[1:]:
            next_fd = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_fd
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _frame_from_fd(descriptor: int, reference: str) -> FrameInput:
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("frame_ref is not a regular file")
    if info.st_size <= 0:
        raise ValueError("frame file is empty")
    if info.st_size > MAX_FRAME_BYTES:
        raise ValueError(f"frame file exceeds the {MAX_FRAME_BYTES}-byte limit")
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, min(65536, MAX_FRAME_BYTES + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > MAX_FRAME_BYTES:
            raise ValueError(f"frame file exceeds the {MAX_FRAME_BYTES}-byte limit")
    data = b"".join(chunks)
    evidence = inspect_image_bytes(data, max_bytes=MAX_FRAME_BYTES)
    return FrameInput(
        reference=reference,
        data=evidence.data,
        sha256=evidence.sha256,
        size_bytes=evidence.size_bytes,
        media_type=evidence.media_type,
        width=evidence.width,
        height=evidence.height,
    )


def _skip_diagnostic(shot: Shot, reason: str) -> str:
    return f"{_shot_reference(shot)}: skipped — {reason}"


def _shot_reference(shot: Shot) -> str:
    return shot.shot_id or f"shot_no={shot.shot_no}"


def canonical_shot_digest(shot: Shot | dict[str, Any]) -> str:
    """SHA-256 of canonical UTF-8 JSON for a complete shot state."""
    value = shot.model_dump(mode="json") if isinstance(shot, Shot) else shot
    return _canonical_json_digest(value)


def _canonical_json_digest(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _media_binding(paths: ProjectPaths) -> dict[str, Any]:
    package_path = paths.data / "media_package.json"
    if not package_path.exists():
        return {
            "status": "unavailable",
            "media_package_sha256": None,
            "receipt_schema_version": None,
            "master_sha256": None,
            "review_sha256": None,
        }
    media = CanonicalMediaPackage.model_validate(load_json(package_path))
    receipt = media.metadata.get("media_receipt") if isinstance(media.metadata, dict) else None
    receipt = receipt if isinstance(receipt, dict) else {}
    master = receipt.get("master") if isinstance(receipt.get("master"), dict) else {}
    review = receipt.get("review") if isinstance(receipt.get("review"), dict) else {}
    master_digest = master.get("sha256") if isinstance(master.get("sha256"), str) else None
    review_digest = review.get("sha256") if isinstance(review.get("sha256"), str) else None
    version = receipt.get("schema_version") if isinstance(receipt.get("schema_version"), str) else None
    return {
        "status": "bound" if version and master_digest and review_digest else "unavailable",
        "media_package_sha256": _canonical_json_digest(media.model_dump(mode="json")),
        "receipt_schema_version": version,
        "master_sha256": master_digest,
        "review_sha256": review_digest,
    }


def analyze_frame(
    frame: FrameInput | Path,
    shot: Shot,
    api_key: str,
    model: str | None,
    base_url: str | None,
    profile: str = "research",
) -> dict[str, Any]:
    frame_input = frame if isinstance(frame, FrameInput) else _read_standalone_frame(frame)
    endpoint_base = validate_endpoint(base_url or DEFAULT_OPENAI_BASE_URL, "OpenAI base URL")
    endpoint = endpoint_base + "/chat/completions"
    selected_model = model or "gpt-5.4-mini"
    image_url = (
        f"data:{frame_input.media_type};base64," + base64.b64encode(frame_input.data).decode("ascii")
    )
    required_fields = _required_fields(profile)
    user_prompt = {
        "shot_no": shot.shot_no,
        "timecode": shot.timecode,
        "analysis_profile": profile,
        "required_json_fields": required_fields,
        "notes": _profile_notes(profile),
    }
    properties = {
        field: {"type": "string", "minLength": 1}
        for field in required_fields
        if field != "confidence"
    }
    properties["confidence"] = {"type": "number", "minimum": 0, "maximum": 1}
    payload = {
        "model": selected_model,
        "messages": [
            {"role": "system", "content": VISION_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": json.dumps(user_prompt, ensure_ascii=False)},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            },
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "shot_observation",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": properties,
                    "required": required_fields,
                    "additionalProperties": False,
                },
            },
        },
        "max_completion_tokens": OPENAI_MAX_OUTPUT_TOKENS,
    }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    opener = urllib.request.build_opener(_NoRedirectHandler())
    try:
        # validate_endpoint limits transport to HTTPS or explicit loopback HTTP.
        with opener.open(request, timeout=OPENAI_TIMEOUT_SECONDS) as response:  # nosec B310
            raw = _read_bounded(response, MAX_PROVIDER_RESPONSE_BYTES)
    except urllib.error.HTTPError as exc:
        try:
            _read_bounded(exc, MAX_PROVIDER_RESPONSE_BYTES)
        except ValueError:
            pass
        finally:
            exc.close()
        raise RuntimeError(f"Vision API request failed with HTTP {exc.code}") from exc
    result = _strict_json_object(raw.decode("utf-8"))
    try:
        choices = result["choices"]
        content = choices[0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("Vision API response is missing choices[0].message.content") from exc
    if not isinstance(content, str):
        raise ValueError("Vision API content must be a JSON string")
    return validate_vision_payload(_strict_json_object(content), profile=profile)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise urllib.error.HTTPError(req.full_url, code, "Redirects are disabled", headers, fp)


def _read_bounded(stream: Any, maximum: int) -> bytes:
    data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError(f"Provider response exceeds the {maximum}-byte limit")
    return data


def _read_standalone_frame(path: Path) -> FrameInput:
    parent = path.parent if path.parent != Path("") else Path.cwd()
    try:
        with _open_relative_regular(parent, Path(path.name)) as descriptor:
            return _frame_from_fd(descriptor, path.name)
    except OSError as exc:
        raise ValueError("Frame cannot be opened safely") from exc


def analyze_frame_with_minimax_mcp(
    frame: FrameInput | Path,
    shot: Shot,
    api_key: str,
    api_host: str | None = None,
    profile: str = "research",
    *,
    executable: str | None = None,
) -> dict[str, Any]:
    frame_input = frame if isinstance(frame, FrameInput) else _read_standalone_frame(frame)
    prompt = {
        "role": "professional film shot analyst",
        "task": "Analyze this single keyframe from a video shot. Return ONLY valid JSON.",
        "shot_no": shot.shot_no,
        "timecode": shot.timecode,
        "analysis_profile": profile,
        "required_json_fields": _required_fields(profile),
        "rules": [
            "Do not invent lens or equipment when not visually inferable.",
            "Use concise production vocabulary.",
            "If uncertain, use 'uncertain' and lower confidence.",
            "Keep observations distinct from interpretation and make the result reviewable at shot level.",
            _profile_notes(profile),
        ],
    }
    with tempfile.TemporaryDirectory(prefix="vew-minimax-") as directory:
        snapshot_dir = Path(directory)
        snapshot_dir.chmod(0o700)
        suffix = ".png" if frame_input.media_type == "image/png" else ".jpg"
        snapshot = snapshot_dir / f"frame{suffix}"
        descriptor = os.open(snapshot, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            view = memoryview(frame_input.data)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:  # pragma: no cover
                    raise OSError("short write while creating MiniMax frame snapshot")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        response = _call_minimax_understand_image(
            str(snapshot),
            json.dumps(prompt, ensure_ascii=False),
            api_key,
            api_host=api_host,
            executable=executable,
        )
    return validate_vision_payload(_parse_jsonish(response), profile=profile)


def _call_minimax_understand_image(
    image_source: str,
    prompt: str,
    api_key: str,
    api_host: str | None = None,
    *,
    executable: str | None = None,
) -> str:
    host = validate_endpoint(api_host or DEFAULT_MINIMAX_API_HOST, "MiniMax API host")
    if executable is not None and not Path(executable).is_absolute():
        raise RuntimeError("MiniMax MCP executable must be an absolute path")
    command = [executable] if executable else _minimax_mcp_command()
    image_path = Path(image_source)
    env = _minimal_minimax_env(api_key, host, image_path.parent)
    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "video-analysis-mvp", "version": "0.2.0"},
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "understand_image",
                "arguments": {"image_source": image_source, "prompt": prompt},
            },
        },
    ]
    input_bytes = b"".join(
        json.dumps(request, ensure_ascii=False).encode("utf-8") + b"\n" for request in requests
    )
    proc = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=False,
        start_new_session=os.name == "posix",
    )
    stdout, _stderr = _communicate_bounded(
        proc,
        input_bytes,
        timeout=MINIMAX_TIMEOUT_SECONDS,
        maximum=MAX_MCP_OUTPUT_BYTES,
    )
    tool_response: dict[str, Any] | None = None
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        try:
            response = _strict_json_object(line)
        except ValueError:
            continue
        if response.get("id") == 2:
            tool_response = response
            break
    if not tool_response:
        raise RuntimeError("MiniMax MCP did not return a tool response")
    if "error" in tool_response:
        raise RuntimeError("MiniMax MCP returned a JSON-RPC error")
    text = _mcp_result_text(tool_response.get("result"))
    if "Failed to perform" in text or "API Error:" in text or "invalid api key" in text.lower():
        raise RuntimeError("MiniMax MCP provider returned an error")
    return text


def _minimal_minimax_env(api_key: str, host: str, private_root: Path) -> dict[str, str]:
    safe_path = os.pathsep.join(
        dict.fromkeys([str(Path(os.sys.executable).parent), "/usr/local/bin", "/usr/bin", "/bin"])
    )
    env = {
        "PATH": safe_path,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONIOENCODING": "utf-8",
        "HOME": str(private_root),
        "TMPDIR": str(private_root),
        "MINIMAX_API_KEY": api_key,
        "MINIMAX_API_HOST": host,
        "MINIMAX_MCP_BASE_PATH": str(private_root),
    }
    if os.name == "nt" and os.getenv("SystemRoot"):  # pragma: no cover
        env["SystemRoot"] = os.environ["SystemRoot"]
    return env


def _communicate_bounded(
    proc: subprocess.Popen[bytes],
    input_bytes: bytes,
    *,
    timeout: float,
    maximum: int,
    input_maximum: int = MAX_MCP_INPUT_BYTES,
) -> tuple[bytes, bytes]:
    process_group_id = proc.pid if os.name == "posix" else None
    if len(input_bytes) > input_maximum:
        reaped = _terminate_and_reap(proc, process_group_id=process_group_id)
        _close_process_streams(proc)
        if not reaped:
            raise RuntimeError("MiniMax MCP process did not terminate")
        raise RuntimeError(f"MiniMax MCP input exceeds the {input_maximum}-byte limit")
    if proc.stdin is None or proc.stdout is None or proc.stderr is None:
        reaped = _terminate_and_reap(proc, process_group_id=process_group_id)
        _close_process_streams(proc)
        if not reaped:
            raise RuntimeError("MiniMax MCP process did not terminate")
        raise RuntimeError("MiniMax MCP stdio pipes were not created")
    deadline = time.monotonic() + max(0.0, timeout)
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    total = 0
    lock = threading.Lock()
    overflow = threading.Event()
    input_errors: list[Exception] = []
    output_errors: list[Exception] = []

    def drain(name: str, stream: Any) -> None:
        nonlocal total
        try:
            while True:
                chunk = os.read(stream.fileno(), 65536)
                if not chunk:
                    return
                with lock:
                    remaining = maximum - total
                    if remaining > 0:
                        buffers[name].extend(chunk[:remaining])
                        total += min(len(chunk), remaining)
                    if len(chunk) > remaining:
                        overflow.set()
                        return
        except Exception as exc:
            output_errors.append(exc)

    def write_input() -> None:
        try:
            remaining = memoryview(input_bytes)
            descriptor = proc.stdin.fileno()
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError("stdin write made no progress")
                remaining = remaining[written:]
        except Exception as exc:
            input_errors.append(exc)
        finally:
            try:
                proc.stdin.close()
            except Exception as exc:
                if not input_errors:
                    input_errors.append(exc)

    drain_threads = [
        threading.Thread(target=drain, args=("stdout", proc.stdout), daemon=True),
        threading.Thread(target=drain, args=("stderr", proc.stderr), daemon=True),
    ]
    writer_thread = threading.Thread(target=write_input, daemon=True)
    threads = [writer_thread, *drain_threads]
    failure: RuntimeError | None = None
    try:
        for thread in drain_threads:
            thread.start()
        writer_thread.start()
        while True:
            if overflow.is_set():
                failure = RuntimeError(f"MiniMax MCP output exceeds the {maximum}-byte limit")
                break
            if input_errors:
                failure = RuntimeError("MiniMax MCP stdin communication failed")
                break
            if output_errors:
                failure = RuntimeError("MiniMax MCP output communication failed")
                break
            if proc.poll() is not None:
                break
            if time.monotonic() >= deadline:
                failure = RuntimeError(f"MiniMax MCP timed out after {timeout} seconds")
                break
            time.sleep(0.01)
    finally:
        # The leader may exit normally while a same-session descendant has
        # closed all inherited stdio. Always kill the original process group,
        # then reap the leader before accepting any result.
        reaped = _terminate_and_reap(proc, process_group_id=process_group_id)
        cleanup_deadline = time.monotonic() + MINIMAX_DRAIN_GRACE_SECONDS
        _join_process_threads(threads, deadline=cleanup_deadline)
        _close_process_streams(proc)
        _join_process_threads(threads, deadline=cleanup_deadline)
    if failure is None and input_errors:
        failure = RuntimeError("MiniMax MCP stdin communication failed")
    if failure is None and output_errors:
        failure = RuntimeError("MiniMax MCP output communication failed")
    if failure is None and writer_thread.is_alive():
        failure = RuntimeError("MiniMax MCP stdin did not stop")
    if any(thread.is_alive() for thread in drain_threads):
        raise RuntimeError("MiniMax MCP output pipes did not drain")
    if not reaped:
        raise RuntimeError("MiniMax MCP process did not terminate")
    if failure is not None:
        raise failure
    if overflow.is_set():
        raise RuntimeError(f"MiniMax MCP output exceeds the {maximum}-byte limit")
    if proc.returncode not in {0, None}:
        raise RuntimeError(f"MiniMax MCP exited with status {proc.returncode}")
    return bytes(buffers["stdout"]), bytes(buffers["stderr"])


def _join_process_threads(threads: list[threading.Thread], *, deadline: float) -> None:
    for thread in threads:
        if thread.ident is None:
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        thread.join(timeout=remaining)


def _close_process_streams(proc: subprocess.Popen[Any]) -> None:
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream is None:
            continue
        try:
            stream.close()
        except Exception:
            pass


def _terminate_and_reap(
    proc: subprocess.Popen[Any],
    *,
    process_group_id: int | None = None,
) -> bool:
    _terminate_process(proc, process_group_id=process_group_id)
    try:
        proc.wait(timeout=MINIMAX_DRAIN_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=MINIMAX_DRAIN_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            return False
    return proc.poll() is not None


def _terminate_process(
    proc: subprocess.Popen[Any],
    *,
    process_group_id: int | None = None,
) -> None:
    if os.name == "posix":
        try:
            os.killpg(process_group_id or proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            pass
        if proc.poll() is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        return
    if proc.poll() is None:  # pragma: no cover - POSIX project
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def _prepare_minimax_mcp() -> tuple[str, str]:
    requested = (os.getenv("MINIMAX_MCP_EXECUTABLE") or "").strip()
    if requested:
        candidate = Path(requested)
        if not candidate.is_absolute():
            raise RuntimeError("MINIMAX_MCP_EXECUTABLE must be an absolute path")
    else:
        located = shutil.which(MINIMAX_MCP_EXECUTABLE)
        if not located:
            raise RuntimeError(
                "MiniMax MCP is not installed. Install minimax-coding-plan-mcp==0.0.4; runtime uvx fetches are disabled."
            )
        candidate = Path(located)
    try:
        executable = candidate.resolve(strict=True)
        info = executable.stat()
    except OSError as exc:
        raise RuntimeError("MiniMax MCP executable cannot be resolved") from exc
    if not executable.is_absolute() or not stat.S_ISREG(info.st_mode) or not os.access(executable, os.X_OK):
        raise RuntimeError("MiniMax MCP executable must be an absolute executable regular file")
    try:
        proc = subprocess.Popen(
            [str(executable), "--version"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={"PATH": os.defpath, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
            start_new_session=os.name == "posix",
        )
        stdout, stderr = _communicate_bounded(proc, b"", timeout=10, maximum=16 * 1024)
    except (OSError, RuntimeError) as exc:
        raise RuntimeError("MiniMax MCP version verification failed") from exc
    rendered = f"{stdout.decode(errors='replace')}\n{stderr.decode(errors='replace')}"
    versions = re.findall(r"(?<![0-9.])([0-9]+\.[0-9]+\.[0-9]+)(?![0-9.])", rendered)
    if versions != [MINIMAX_MCP_VERSION]:
        raise RuntimeError(
            f"MiniMax MCP must report exactly version {MINIMAX_MCP_VERSION}; runtime package fetch is disabled"
        )
    return str(executable), MINIMAX_MCP_VERSION


def _minimax_mcp_command() -> list[str]:
    executable, _version = _prepare_minimax_mcp()
    return [executable]


def _mcp_result_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        if isinstance(result.get("data"), str):
            return result["data"]
        content = result.get("content")
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
            if parts:
                return "\n".join(parts)
        return json.dumps(result, ensure_ascii=False)
    return json.dumps(result, ensure_ascii=False)


def _parse_jsonish(text: str) -> dict[str, Any]:
    try:
        return _strict_json_object(text)
    except ValueError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return _strict_json_object(text[start : end + 1])
        raise


def _strict_json_object(text: str) -> dict[str, Any]:
    try:
        value = json.loads(
            text,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"Invalid constant: {value}")),
        )
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("Provider output is not valid JSON") from exc
    if type(value) is not dict:
        raise ValueError("Provider output must be a JSON object")
    return value


def validate_vision_payload(data: Any, profile: str = "research") -> dict[str, Any]:
    if type(data) is not dict:
        raise ValueError("Vision payload must be a JSON object")
    required = _required_fields(profile)
    expected = set(required)
    actual = set(data)
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    if missing:
        raise ValueError(f"Vision payload is missing required fields: {', '.join(missing)}")
    if unexpected:
        raise ValueError(f"Vision payload has unexpected fields: {', '.join(unexpected)}")
    normalized: dict[str, Any] = {}
    for field in required:
        value = data[field]
        if field == "confidence":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("Vision confidence must be a finite JSON number")
            numeric = float(value)
            if not math.isfinite(numeric) or not 0.0 <= numeric <= 1.0:
                raise ValueError("Vision confidence must be finite and between 0 and 1")
            normalized[field] = numeric
        else:
            if type(value) is not str or not value.strip():
                raise ValueError(f"Vision field {field} must be a non-empty string")
            normalized[field] = value.strip()
    return normalized


def _load_minimax_config_key() -> str | None:
    """Compatibility hook: implicit cross-application credential discovery is disabled."""
    return None


def apply_vision_data(
    shot: Shot,
    data: dict[str, Any],
    profile: str = "research",
    *,
    annotation_source: str = VisionProvider.openai.value,
) -> None:
    if _provider_exclusion_reason(shot):
        raise ValueError("Protected shot cannot be overwritten by provider annotation")
    normalized_profile = _normalize_profile(profile)
    validated = validate_vision_payload(data, profile=normalized_profile)
    fields = [*OBSERVATION_FIELDS, *ADS_INTERPRETATION_FIELDS] if normalized_profile == "ads" else OBSERVATION_FIELDS
    for field in fields:
        setattr(shot, field, validated[field])
    if normalized_profile != "ads":
        shot.remake_notes = ""
        shot.remake_notes_zh = ""
        shot.prompt_en = ""
        shot.prompt_zh = ""
        neutral_beats = {
            "opening_sequence",
            "early_sequence",
            "middle_sequence",
            "late_sequence",
            "closing_sequence",
        }
        current_beat = shot.story_beat.removeprefix("heuristic_unverified:")
        shot.story_beat = (
            f"heuristic_unverified:{current_beat}"
            if current_beat in neutral_beats
            else "heuristic_unverified:sequence_position_pending"
        )
        current_scene = shot.scene_type.removeprefix("heuristic_unverified:")
        shot.scene_type = (
            f"heuristic_unverified:{current_scene}" if current_scene in neutral_beats else shot.story_beat
        )
    shot.visual_description = shot.content_summary
    shot.confidence = validated["confidence"]
    shot.visual_confidence = shot.confidence
    shot.annotation_source = _normalize_annotation_source(annotation_source)
    shot.readiness_status = (
        "ready" if shot.visual_confidence >= 0.65 and shot.annotation_source != "codex" else "blocked"
    )
    profile_note = "ads interpretation included" if normalized_profile == "ads" else "neutral observations only"
    shot.review_notes = f"{shot.annotation_source} annotated ({profile_note}); verify against source before final evidence use"


def _project_profile(paths: ProjectPaths) -> str:
    try:
        media = CanonicalMediaPackage.model_validate(load_json(paths.data / "media_package.json"))
        value = media.analysis_profile.value if hasattr(media.analysis_profile, "value") else str(media.analysis_profile)
        return _normalize_profile(value)
    except Exception:
        return "research"


def _normalize_profile(profile: str) -> str:
    value = str(profile or "research").strip().lower()
    return value if value in {"ads", "research", "streaming", "shortform", "festival"} else "research"


def _required_fields(profile: str) -> list[str]:
    fields = list(OBSERVATION_FIELDS)
    if _normalize_profile(profile) == "ads":
        fields.extend(ADS_INTERPRETATION_FIELDS)
    fields.append("confidence")
    return fields


def _profile_notes(profile: str) -> str:
    if _normalize_profile(profile) == "ads":
        return (
            "This is an ads profile. Creative and narrative fields are unverified model interpretations; "
            "keep them separate from visible observations. If uncertain, use 'uncertain' and lower confidence."
        )
    return (
        "This is a non-ad evidence profile. Return neutral visible observations only. Do not use marketing roles "
        "such as hook, problem, demo, proof, payoff, or CTA, and do not return remake advice or generation prompts. "
        "If uncertain, use 'uncertain' and lower confidence."
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
