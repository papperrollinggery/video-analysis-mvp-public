"""Bounded, resumable analysis over one immutable native Codex request.

Checkpoints are proposals only. The final batch uses the existing apply path so
partial work never impersonates complete annotations or human review.
"""

from __future__ import annotations

from typing import Any

from ._audio_intelligence_storage import read_file_bytes_and_receipt, strict_json_loads
from .codex_analysis import (
    MAX_ASSEMBLED_RESPONSE_BYTES,
    RESPONSE_SCHEMA,
    CodexAnalysisConflict,
    _analysis_fields,
    _apply_codex_analysis_locked,
    _build_request,
    _current_submission_matches,
    _json_bytes,
    _normalized_response,
    _read_request,
    _validate_response,
    prepare_codex_analysis,
)
from .paths import ProjectPaths
from .safe_io import advisory_file_lock
from .schemas import dump_json

DEFAULT_BATCH_SIZE = 12
MAX_BATCH_SIZE = 32
PROGRESS_PATH = "data/codex_analysis_progress.json"


def next_codex_batch(paths: ProjectPaths, *, batch_size: int = DEFAULT_BATCH_SIZE) -> dict[str, Any]:
    if type(batch_size) is not int or not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError(f"Codex batch size must be 1 to {MAX_BATCH_SIZE}")
    prepared = prepare_codex_analysis(paths)
    request = prepared["request"]
    if prepared["status"] == "applied":
        return _progress_result(request, len(request["shots"]), status="applied")
    with advisory_file_lock(paths.data / ".shots.lock", root=paths.root):
        if _read_request(paths)["request_id"] != request["request_id"]:
            raise CodexAnalysisConflict("Codex request changed; request the next batch again")
        analyses = _read_progress(paths, request)
    selected = [item for item in request["shots"] if item["shot"]["shot_id"] not in analyses][:batch_size]
    selected_ids = {item["shot"]["shot_id"] for item in selected}
    indexes = [i for i, item in enumerate(request["shots"]) if item["shot"]["shot_id"] in selected_ids]
    neighbors = sorted({j for i in indexes for j in (i - 1, i + 1) if 0 <= j < len(request["shots"])})
    context = [request["shots"][i] for i in neighbors if request["shots"][i]["shot"]["shot_id"] not in selected_ids]
    fields = _analysis_fields(request["profile"])
    return {
        **_progress_result(request, len(analyses), status="batch_ready" if selected else "ready_to_apply"),
        "guide": request["guide"],
        "evidence_root": str(paths.root),
        "source_clip": "assets/review.mp4",
        "audio_evidence": request["audio_evidence"],
        "shots": [_evidence_item(item) for item in selected],
        "adjacent_context": [_evidence_item(item) for item in context],
        "response_schema": request["response_schema"],
        "response_template": {
            "schema_id": RESPONSE_SCHEMA,
            "project_id": request["project_id"],
            "request_id": request["request_id"],
            "analyses": [
                {"shot_id": item["shot"]["shot_id"], "analysis": {
                    field: 0.0 if field == "confidence" else "" for field in fields
                }} for item in selected
            ],
        },
        "next_action": "Inspect this batch, fill the response template, then codex submit --result FILE. Empty template fields are invalid. If all rows are checkpointed after a failed commit, codex submit --finish retries the commit.",
    }


def submit_codex_batch(
    paths: ProjectPaths, response: Any = None, *, replace: bool = False, finish: bool = False
) -> dict[str, Any]:
    from .workspace_api import project_write_lock

    with project_write_lock(paths.root), advisory_file_lock(paths.data / ".shots.lock", root=paths.root):
        request = _read_request(paths)
        if finish and response is not None:
            raise ValueError("Use either a batch response or --finish")
        incoming = {} if finish else _validate_response(response, request, require_complete=False)
        if len(incoming) > MAX_BATCH_SIZE:
            raise ValueError(f"Submit at most {MAX_BATCH_SIZE} shots per batch")
        analyses = _read_progress(paths, request)
        if _current_submission_matches(paths, request):
            # Retrying the final submitted batch is a readback, not a new run.
            if not finish and any(analyses.get(key) != value for key, value in incoming.items()):
                raise CodexAnalysisConflict("Analysis is already applied; prepare --refresh before changing it")
            return _progress_result(request, len(request["shots"]), status="applied")
        if _build_request(paths, guide_version=request["guide_version"])["request_id"] != request["request_id"]:
            raise CodexAnalysisConflict("Codex evidence changed; prepare current evidence again")
        for shot_id, analysis in incoming.items():
            if shot_id in analyses and analyses[shot_id] != analysis and not replace:
                raise CodexAnalysisConflict("A different response is checkpointed for this shot; use --replace for an intentional correction")
            analyses[shot_id] = analysis
        staged = _partial_response(request, analyses)
        if len(_json_bytes(staged)) > MAX_ASSEMBLED_RESPONSE_BYTES:
            raise ValueError("Combined Codex response exceeds 8 MiB; shorten repeated field text with --replace")
        # Persist before the final apply so a failed commit can be retried
        # without inspecting or writing every batch again.
        if incoming:
            dump_json(paths.root / PROGRESS_PATH, staged)
        if len(analyses) != len(request["shots"]):
            if finish:
                raise ValueError("Cannot finish while selected shots are still missing")
            return _progress_result(request, len(analyses), status="checkpointed")
        result = _apply_codex_analysis_locked(
            paths, _normalized_response(request, analyses), response_limit=MAX_ASSEMBLED_RESPONSE_BYTES
        )
        return {**_progress_result(request, len(analyses), status=result["status"]), **result}


def batch_progress(paths: ProjectPaths, request: dict[str, Any]) -> dict[str, Any]:
    analyses = _read_progress(paths, request)
    return {"checkpointed_shot_count": len(analyses), "remaining_shot_count": len(request["shots"]) - len(analyses)}


def _read_progress(paths: ProjectPaths, request: dict[str, Any]) -> dict[str, Any]:
    path = paths.root / PROGRESS_PATH
    if not path.exists() and not path.is_symlink():
        return {}
    raw, _receipt = read_file_bytes_and_receipt(path, MAX_ASSEMBLED_RESPONSE_BYTES * 2)
    response = strict_json_loads(raw)
    if type(response) is not dict:
        raise ValueError("Codex checkpoint is invalid")
    if response.get("request_id") != request["request_id"]:
        # A new evidence generation starts an empty proposal set. Old rows
        # cannot be silently adopted by another request.
        return {}
    return _validate_response(response, request, require_complete=False, maximum=MAX_ASSEMBLED_RESPONSE_BYTES)


def _partial_response(request: dict[str, Any], analyses: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_id": RESPONSE_SCHEMA,
        "project_id": request["project_id"],
        "request_id": request["request_id"],
        "analyses": [
            {"shot_id": item["shot"]["shot_id"], "analysis": analyses[item["shot"]["shot_id"]]}
            for item in request["shots"] if item["shot"]["shot_id"] in analyses
        ],
    }


def _evidence_item(item: dict[str, Any]) -> dict[str, Any]:
    shot = item["shot"]
    return {
        "shot_id": shot["shot_id"],
        "start_time": shot["start_time"],
        "end_time": shot["end_time"],
        "boundary_confidence": shot["boundary_confidence"],
        "frames": item.get("frames", [item["frame"]]),
        "frame_order": "visual generation frame_refs order; exact sample timestamps are not recorded",
    }


def _progress_result(request: dict[str, Any], completed: int, *, status: str) -> dict[str, Any]:
    return {
        "status": status,
        "request_id": request["request_id"],
        "selected_shot_count": len(request["shots"]),
        "completed_shot_count": completed,
        "remaining_shot_count": len(request["shots"]) - completed,
        "progress_path": PROGRESS_PATH,
        "review_required": True,
        "provider_called": False,
        "model_identity_verified": False,
    }
