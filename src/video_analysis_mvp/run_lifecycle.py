from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .audio import analyze_audio, audio_generation_binding, verify_audio_analysis
from .audio_intelligence import audio_intelligence_binding
from .media import (
    create_project_id,
    ingest_source,
    normalized_source,
    verify_media_generation,
)
from .paths import (
    ProjectPaths,
    new_project_paths,
    project_paths,
    resolve_project_root,
    validate_project_id,
)
from .pipeline import set_delivery_language
from .safe_io import advisory_file_lock, atomic_write_text, read_regular_bytes
from .schemas import AnalysisProfile
from .store import load_media
from .synthesis import synthesize, verify_report_generation_manifest
from .utils import ProcessCancelledError, process_cancellation
from .visual import analyze_visual, verify_visual_generation, visual_generation_binding

JsonDict = dict[str, Any]
RUN_SCHEMA_VERSION = 1
RUN_STATES = frozenset({"queued", "running", "cancelling", "cancelled", "completed", "failed", "interrupted"})
RETRYABLE_RUN_STATES = frozenset({"failed", "interrupted", "cancelled"})
ACTIVE_RUN_STATES = frozenset({"queued", "running", "cancelling"})
MAX_RUN_RECORD_BYTES = 1024 * 1024
MAX_ACTIVE_ANALYSIS_RUNS = 1
MIN_WORKSPACE_FREE_BYTES = 256 * 1024 * 1024
STAGE_PROGRESS = {
    "queued": 0,
    "ingest": 8,
    "visual": 36,
    "audio": 62,
    "report": 84,
    "finalize": 96,
    "completed": 100,
}

_THREADS: dict[str, threading.Thread] = {}
_THREADS_LOCK = threading.Lock()


class RunCancelled(RuntimeError):
    pass


class RunAdmissionError(ValueError):
    pass


def start_analysis_run(workspace: Path, request: JsonDict) -> JsonDict:
    """Persist and launch one local analysis run.

    The caller must provide a canonical local source and a validated profile.
    Passwords and external-provider credentials are intentionally unsupported.
    """
    source = str(request.get("source") or "")
    if not source:
        raise ValueError("Analysis run requires a canonical local source")
    profile = AnalysisProfile(str(request.get("profile") or AnalysisProfile.research.value))
    project_id = validate_project_id(str(request.get("project_id") or create_project_id(source)))
    project_root = resolve_project_root(project_id, workspace)
    if os.path.lexists(project_root):
        raise FileExistsError(f"Project already exists: {project_id}")
    run_id = str(uuid.uuid4())
    now = _timestamp()
    safe_request = {
        "source": source,
        "profile": profile.value,
        "language": str(request.get("language") or "auto"),
        "delivery_language": "en" if request.get("delivery_language") == "en" else "zh",
        "skip_asr": bool(request.get("skip_asr", True)),
        "max_duration_seconds": float(request.get("max_duration_seconds", 60.0)),
    }
    record: JsonDict = {
        "schema_version": RUN_SCHEMA_VERSION,
        "run_id": run_id,
        "project_id": project_id,
        "kind": "full_analysis",
        "state": "queued",
        "stage": "queued",
        "progress": 0,
        "attempt": 0,
        "created_at": now,
        "started_at": None,
        "updated_at": now,
        "finished_at": None,
        "owner_pid": os.getpid(),
        "launching": True,
        "cancel_requested_at": None,
        "request": safe_request,
        "stages": [],
        "error": None,
        "result": None,
        "links": {
            "self": f"/api/runs/{run_id}",
            "project": f"/api/projects/{project_id}",
            "workspace": f"/projects/{project_id}",
        },
    }
    control, _runs = _control_paths(workspace)
    with advisory_file_lock(control / ".admission.lock", root=control):
        _assert_run_capacity(workspace)
        _assert_workspace_disk_budget(workspace, source)
        _write_new_record(workspace, record)
    _launch(workspace, run_id)
    return read_analysis_run(workspace, run_id)


def read_analysis_run(workspace: Path, run_id: str) -> JsonDict:
    run_id = validate_run_id(run_id)
    record = _read_record(workspace, run_id)
    if record.get("state") in ACTIVE_RUN_STATES and not _run_is_active(run_id, record):
        def interrupt(current: JsonDict) -> None:
            if current.get("state") not in ACTIVE_RUN_STATES:
                return
            now = _timestamp()
            current.update(
                {
                    "state": "interrupted",
                    "updated_at": now,
                    "finished_at": now,
                    "owner_pid": None,
                    "error": {
                        "type": "RunInterrupted",
                        "message": "The worker process stopped before the run reached a terminal state.",
                        "retriable": True,
                    },
                }
            )
            _finish_current_stage(current, "interrupted", now)

        record = _mutate_record(workspace, run_id, interrupt)
    return record


def retry_analysis_run(workspace: Path, run_id: str) -> JsonDict:
    run_id = validate_run_id(run_id)

    def queue_retry(record: JsonDict) -> None:
        if record.get("state") not in RETRYABLE_RUN_STATES:
            raise ValueError("Only failed or interrupted runs can be retried")
        error = record.get("error")
        if isinstance(error, dict) and error.get("retriable") is False:
            raise ValueError("This analysis run failed permanently and cannot be retried")
        now = _timestamp()
        record.update(
            {
                "state": "queued",
                "stage": "queued",
                "progress": 0,
                "updated_at": now,
                "finished_at": None,
                "owner_pid": os.getpid(),
                "launching": True,
                "cancel_requested_at": None,
                "error": None,
                "result": None,
            }
        )

    control, _runs = _control_paths(workspace)
    with advisory_file_lock(control / ".admission.lock", root=control):
        _assert_run_capacity(workspace)
        retry_record = _read_record(workspace, run_id)
        retry_request = retry_record.get("request")
        if not isinstance(retry_request, dict):
            raise RunAdmissionError("Analysis run request is missing or invalid")
        _assert_workspace_disk_budget(
            workspace, str(retry_request.get("source") or "")
        )
        _mutate_record(workspace, run_id, queue_retry)
    _launch(workspace, run_id)
    return read_analysis_run(workspace, run_id)


def cancel_analysis_run(workspace: Path, run_id: str) -> JsonDict:
    run_id = validate_run_id(run_id)

    def cancel(record: JsonDict) -> None:
        state = record.get("state")
        if state not in ACTIVE_RUN_STATES:
            raise ValueError("Only queued or running analysis runs can be cancelled")
        now = _timestamp()
        record["cancel_requested_at"] = now
        record["updated_at"] = now
        if state == "queued":
            record.update(
                {
                    "state": "cancelled",
                    "stage": "queued",
                    "finished_at": now,
                    "owner_pid": None,
                    "launching": False,
                }
            )
        else:
            record["state"] = "cancelling"

    return _mutate_record(workspace, run_id, cancel)


def list_analysis_runs(workspace: Path, *, project_id: str | None = None) -> list[JsonDict]:
    if project_id is not None:
        project_id = validate_project_id(project_id)
    _control_paths(workspace)
    records: list[JsonDict] = []
    for candidate in sorted(_runs_directory(workspace).glob("*.json"), reverse=True):
        try:
            record = read_analysis_run(workspace, candidate.stem)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if project_id is None or record.get("project_id") == project_id:
            records.append(record)
    return records


def _launch(workspace: Path, run_id: str) -> None:
    with _THREADS_LOCK:
        active = _THREADS.get(run_id)
        if active is not None and active.is_alive():
            raise ValueError("Analysis run is already active")
        thread = threading.Thread(
            target=_thread_entrypoint,
            args=(workspace.resolve(), run_id),
            name=f"vew-run-{run_id[:8]}",
            daemon=True,
        )
        _THREADS[run_id] = thread
        try:
            thread.start()
        except Exception:
            _THREADS.pop(run_id, None)
            _mutate_record(workspace, run_id, lambda record: record.update({"launching": False, "owner_pid": None}))
            raise
    _mutate_record(workspace, run_id, lambda record: record.update({"launching": False}))


def _thread_entrypoint(workspace: Path, run_id: str) -> None:
    try:
        _execute_run(workspace, run_id)
    finally:
        with _THREADS_LOCK:
            _THREADS.pop(run_id, None)


def _execute_run(workspace: Path, run_id: str) -> None:
    record = _read_record(workspace, run_id)
    project_id = validate_project_id(str(record.get("project_id") or ""))
    control, _runs = _control_paths(workspace)
    with advisory_file_lock(control / f".project-{project_id}.lock", root=control):
        _execute_run_locked(workspace, run_id)


def _execute_run_locked(workspace: Path, run_id: str) -> None:
    started_monotonic = time.monotonic()

    def begin(record: JsonDict) -> None:
        if record.get("state") == "cancelled":
            raise RunCancelled()
        now = _timestamp()
        record.update(
            {
                "state": "running",
                "stage": "ingest",
                "progress": STAGE_PROGRESS["ingest"],
                "attempt": int(record.get("attempt") or 0) + 1,
                "started_at": now,
                "updated_at": now,
                "finished_at": None,
                "owner_pid": os.getpid(),
                "launching": False,
                "error": None,
            }
        )
        _start_stage(record, "ingest", now)

    try:
        record = _mutate_record(workspace, run_id, begin)
    except RunCancelled:
        return
    request = record["request"]
    project_id = str(record["project_id"])
    current_stage = "ingest"
    cancellation_scope = process_cancellation(
        lambda: _run_cancel_requested(workspace, run_id)
    )
    cancellation_scope.__enter__()
    try:
        paths = _claimed_project_paths(workspace, project_id, run_id)
        media_ok, media_reasons = verify_media_generation(paths)
        if media_ok:
            media = load_media(paths)
            _assert_media_matches_request(media, request)
            _complete_stage(workspace, run_id, "ingest", skipped=True, detail="Verified existing media receipt")
        else:
            if (paths.data / "media_package.json").exists():
                raise ValueError("Existing media generation is invalid: " + "; ".join(media_reasons))
            media = ingest_source(
                str(request["source"]),
                paths,
                AnalysisProfile(str(request["profile"])),
                max_duration_seconds=float(request["max_duration_seconds"]),
            )
            _complete_stage(workspace, run_id, "ingest")
        set_delivery_language(paths, media, str(request["delivery_language"]))

        current_stage = "visual"
        _begin_stage(workspace, run_id, current_stage)
        visual_ok, _visual_reasons = verify_visual_generation(paths)
        if visual_ok:
            _complete_stage(workspace, run_id, current_stage, skipped=True, detail="Verified existing visual generation")
        else:
            analyze_visual(media, paths)
            verified, reasons = verify_visual_generation(paths)
            if not verified:
                raise RuntimeError("Visual generation verification failed: " + "; ".join(reasons))
            _complete_stage(workspace, run_id, current_stage)

        current_stage = "audio"
        _begin_stage(workspace, run_id, current_stage)
        audio_ok, _audio_reasons = verify_audio_analysis(paths)
        if audio_ok:
            _complete_stage(workspace, run_id, current_stage, skipped=True, detail="Verified existing input-bound audio analysis")
        else:
            analyze_audio(
                media,
                paths,
                language=str(request["language"]),
                skip_asr=bool(request["skip_asr"]),
            )
            verified, reasons = verify_audio_analysis(paths)
            if not verified:
                raise RuntimeError("Audio generation verification failed: " + "; ".join(reasons))
            _complete_stage(workspace, run_id, current_stage)

        current_stage = "report"
        _begin_stage(workspace, run_id, current_stage)
        report_ok, _report_reasons = verify_report_generation_manifest(paths)
        if report_ok:
            report_artifacts = _manifest_artifacts(paths)
            _complete_stage(workspace, run_id, current_stage, skipped=True, detail="Verified existing report generation")
        else:
            report = synthesize(paths)
            report_artifacts = report.artifacts
            verified, reasons = verify_report_generation_manifest(paths)
            if not verified:
                raise RuntimeError("Report generation verification failed: " + "; ".join(reasons))
            _complete_stage(workspace, run_id, current_stage)

        current_stage = "finalize"
        _begin_stage(workspace, run_id, current_stage)
        from .workspace_api import ensure_project_data

        ensure_project_data(workspace, paths.root)
        _complete_stage(workspace, run_id, current_stage)
        _raise_if_cancelled(workspace, run_id)
        generation_bindings = _final_generation_bindings(paths)
        elapsed = round(time.monotonic() - started_monotonic, 3)

        def complete(final: JsonDict) -> None:
            if final.get("state") == "cancelling":
                raise RunCancelled()
            now = _timestamp()
            final.update(
                {
                    "state": "completed",
                    "stage": "completed",
                    "progress": 100,
                    "updated_at": now,
                    "finished_at": now,
                    "owner_pid": None,
                    "launching": False,
                    "error": None,
                    "result": {
                        "status": "success",
                        "summary": f"Project {project_id} analyzed successfully.",
                        "elapsed_seconds": elapsed,
                        "artifacts": report_artifacts,
                        "generation_bindings": generation_bindings,
                    },
                }
            )

        _mutate_record(workspace, run_id, complete)
    except (RunCancelled, ProcessCancelledError) as exc:
        cleanup_verified = not isinstance(exc, ProcessCancelledError) or exc.cleanup_verified is not False
        if not cleanup_verified:
            def cleanup_failed(failed_record: JsonDict) -> None:
                now = _timestamp()
                failed_record.update(
                    {
                        "state": "failed",
                        "updated_at": now,
                        "finished_at": now,
                        "owner_pid": None,
                        "launching": False,
                        "error": {
                            "type": "ProcessCleanupUnverified",
                            "message": "Cancellation was requested but process-group cleanup could not be verified.",
                            "retriable": True,
                        },
                    }
                )
                _finish_current_stage(
                    failed_record,
                    "failed",
                    now,
                    detail="Cancellation cleanup could not be verified",
                )

            _mutate_record(workspace, run_id, cleanup_failed)
            return

        def cancelled(cancelled_record: JsonDict) -> None:
            now = _timestamp()
            cancelled_record.update(
                {
                    "state": "cancelled",
                    "updated_at": now,
                    "finished_at": now,
                    "owner_pid": None,
                    "launching": False,
                    "error": None,
                    "result": {
                        "status": "cancelled",
                        "summary": "The run stopped at a safe stage boundary after a local cancellation request.",
                    },
                }
            )
            _finish_current_stage(cancelled_record, "cancelled", now, detail="Cancelled by local operator")

        _mutate_record(workspace, run_id, cancelled)
    except Exception as exc:
        error_type = type(exc).__name__
        error_message = str(exc) or error_type
        retriable = not isinstance(exc, FileExistsError)

        def fail(failed: JsonDict) -> None:
            now = _timestamp()
            failed.update(
                {
                    "state": "failed",
                    "stage": current_stage,
                    "updated_at": now,
                    "finished_at": now,
                    "owner_pid": None,
                    "launching": False,
                    "error": {
                        "type": error_type,
                        "message": error_message,
                        "retriable": retriable,
                    },
                }
            )
            _finish_current_stage(failed, "failed", now, detail=error_message)

        _mutate_record(workspace, run_id, fail)
    finally:
        cancellation_scope.__exit__(None, None, None)


def _begin_stage(workspace: Path, run_id: str, stage: str) -> None:
    def begin(record: JsonDict) -> None:
        if record.get("state") == "cancelling":
            raise RunCancelled()
        now = _timestamp()
        record.update({"stage": stage, "progress": STAGE_PROGRESS[stage], "updated_at": now})
        _start_stage(record, stage, now)

    _mutate_record(workspace, run_id, begin)


def _raise_if_cancelled(workspace: Path, run_id: str) -> None:
    if _read_record(workspace, run_id).get("state") == "cancelling":
        raise RunCancelled()


def _run_cancel_requested(workspace: Path, run_id: str) -> bool:
    return _read_record(workspace, run_id).get("state") in {"cancelling", "cancelled"}


def _complete_stage(
    workspace: Path,
    run_id: str,
    stage: str,
    *,
    skipped: bool = False,
    detail: str | None = None,
) -> None:
    def complete(record: JsonDict) -> None:
        now = _timestamp()
        _finish_current_stage(record, "skipped" if skipped else "completed", now, detail=detail)
        record["updated_at"] = now

    _mutate_record(workspace, run_id, complete)


def _start_stage(record: JsonDict, stage: str, now: str) -> None:
    record.setdefault("stages", []).append(
        {
            "id": stage,
            "state": "running",
            "attempt": record.get("attempt"),
            "started_at": now,
            "finished_at": None,
            "elapsed_seconds": None,
            "detail": None,
        }
    )


def _finish_current_stage(record: JsonDict, state: str, now: str, *, detail: str | None = None) -> None:
    stages = record.get("stages")
    if not isinstance(stages, list):
        return
    current = next((item for item in reversed(stages) if isinstance(item, dict) and item.get("state") == "running"), None)
    if current is None:
        return
    current["state"] = state
    current["finished_at"] = now
    current["detail"] = detail
    started = current.get("started_at")
    if isinstance(started, str):
        try:
            current["elapsed_seconds"] = round(
                (datetime.fromisoformat(now) - datetime.fromisoformat(started)).total_seconds(),
                3,
            )
        except ValueError:
            current["elapsed_seconds"] = None


def _manifest_artifacts(paths: ProjectPaths) -> JsonDict:
    try:
        payload = json.loads(read_regular_bytes(paths.manifest, root=paths.root).decode("utf-8"))
    except Exception:
        return {}
    artifacts = payload.get("artifacts") if isinstance(payload, dict) else None
    return dict(artifacts) if isinstance(artifacts, dict) else {}


def _claimed_project_paths(workspace: Path, project_id: str, run_id: str) -> ProjectPaths:
    """Bind one project ID to one durable run before creating or reusing it.

    The caller holds the project lease. A retry may reuse only the project
    claimed by the same run; a new run can never adopt an existing project.
    """
    control, _runs = _control_paths(workspace)
    claim_path = control / f".project-{project_id}.json"
    root = resolve_project_root(project_id, workspace)
    expected = {"schema_version": 1, "project_id": project_id, "run_id": run_id}
    if os.path.lexists(claim_path):
        claim = json.loads(read_regular_bytes(claim_path, root=control, max_bytes=4096).decode("utf-8"))
        if claim != expected:
            raise FileExistsError(f"Project is owned by another analysis run: {project_id}")
    else:
        if os.path.lexists(root):
            raise FileExistsError(f"Project already exists: {project_id}")
        atomic_write_text(claim_path, json.dumps(expected, sort_keys=True), root=control)

    owner_path = root / ".vew-run-owner.json"
    if os.path.lexists(root):
        if not os.path.lexists(owner_path):
            if not _is_blank_managed_project_root(root):
                raise FileExistsError(f"Existing project is not owned by analysis run {run_id}: {project_id}")
            atomic_write_text(owner_path, json.dumps(expected, sort_keys=True), root=root)
        paths = project_paths(project_id, workspace)
        try:
            owner = json.loads(read_regular_bytes(owner_path, root=root, max_bytes=4096).decode("utf-8"))
        except FileNotFoundError:
            raise FileExistsError(f"Existing project is not owned by analysis run {run_id}: {project_id}") from None
        if owner != expected:
            raise FileExistsError(f"Project is owned by another analysis run: {project_id}")
        return paths

    paths = new_project_paths(project_id, workspace)
    atomic_write_text(owner_path, json.dumps(expected, sort_keys=True), root=paths.root)
    return paths


def _is_blank_managed_project_root(root: Path) -> bool:
    """Recognize only the exact empty tree created before an owner commit.

    This narrow recovery rule cannot adopt a synchronous or user-created
    project because any extra, missing, non-directory, symlinked, or non-empty
    entry fails closed.
    """
    try:
        root_info = root.lstat()
        if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
            return False
        if {item.name for item in root.iterdir()} != {"ingest", "assets", "data", "reports"}:
            return False
        expected_empty = (root / "ingest", root / "data", root / "reports", root / "assets" / "keyframes")
        for directory in expected_empty:
            info = directory.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or any(directory.iterdir()):
                return False
        assets = root / "assets"
        assets_info = assets.lstat()
        return (
            not stat.S_ISLNK(assets_info.st_mode)
            and stat.S_ISDIR(assets_info.st_mode)
            and {item.name for item in assets.iterdir()} == {"keyframes"}
        )
    except OSError:
        return False


def _assert_media_matches_request(media: Any, request: JsonDict) -> None:
    _source_type, expected_source = normalized_source(str(request["source"]))
    actual_profile = getattr(media.analysis_profile, "value", str(media.analysis_profile))
    if media.source != expected_source:
        raise ValueError("Existing media source does not match the durable run request")
    if actual_profile != str(request["profile"]):
        raise ValueError("Existing media profile does not match the durable run request")


def _final_generation_bindings(paths: ProjectPaths) -> JsonDict:
    media_payload = read_regular_bytes(paths.data / "media_package.json", root=paths.root)
    manifest_payload = read_regular_bytes(paths.manifest, root=paths.root)
    manifest = json.loads(manifest_payload.decode("utf-8"))
    report_generation = manifest.get("report_generation") if isinstance(manifest, dict) else None
    return {
        "media_package_sha256": hashlib.sha256(media_payload).hexdigest(),
        "visual_generation": visual_generation_binding(paths),
        "audio_generation": audio_generation_binding(paths),
        "audio_intelligence": audio_intelligence_binding(paths),
        "report_manifest_sha256": hashlib.sha256(manifest_payload).hexdigest(),
        "report_generation_id": (
            report_generation.get("generation_id") if isinstance(report_generation, dict) else None
        ),
    }


def _run_is_active(run_id: str, record: JsonDict) -> bool:
    with _THREADS_LOCK:
        thread = _THREADS.get(run_id)
        if thread is not None and thread.is_alive():
            return True
    owner_pid = record.get("owner_pid")
    if type(owner_pid) is not int or owner_pid <= 0:
        return False
    if owner_pid == os.getpid():
        if record.get("launching") is True:
            return True
        # In this process the thread registry is authoritative.  Treating the
        # process itself as the worker would leave a crashed daemon run stuck.
        return False
    try:
        os.kill(owner_pid, 0)
    except (OSError, ValueError):
        return False
    return True


def _control_paths(workspace: Path) -> tuple[Path, Path]:
    workspace = workspace.expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    _require_directory(workspace)
    control = workspace / ".vew"
    runs = control / "runs"
    for directory in (control, runs):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
        _require_directory(directory)
    return control, runs


def _assert_run_capacity(workspace: Path) -> None:
    active = 0
    for candidate in _runs_directory(workspace).glob("*.json"):
        try:
            record = _read_record(workspace, candidate.stem)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if record.get("state") in ACTIVE_RUN_STATES and _run_is_active(candidate.stem, record):
            active += 1
    if active >= MAX_ACTIVE_ANALYSIS_RUNS:
        raise RunAdmissionError(
            f"Workspace already has {active} active analysis run(s); wait or cancel before starting another"
        )


def _assert_workspace_disk_budget(workspace: Path, source: str) -> None:
    try:
        source_info = Path(source).stat()
        free = shutil.disk_usage(workspace).free
    except OSError as exc:
        raise RunAdmissionError("Workspace disk budget could not be verified") from exc
    if not stat.S_ISREG(source_info.st_mode):
        raise RunAdmissionError("Analysis source must be a regular file")
    required = source_info.st_size + MIN_WORKSPACE_FREE_BYTES
    if free < required:
        raise RunAdmissionError(
            f"Workspace has insufficient free space for this run: {free} available, {required} required"
        )


def _runs_directory(workspace: Path) -> Path:
    return _control_paths(workspace)[1]


def _require_directory(path: Path) -> None:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"Unsafe run state directory: {path}")


def _run_path(workspace: Path, run_id: str) -> tuple[Path, Path]:
    control, runs = _control_paths(workspace)
    return control, runs / f"{validate_run_id(run_id)}.json"


def _write_new_record(workspace: Path, record: JsonDict) -> None:
    control, path = _run_path(workspace, str(record["run_id"]))
    lock = control / ".runs.lock"
    with advisory_file_lock(lock, root=control):
        if path.exists() or path.is_symlink():
            raise FileExistsError("Analysis run already exists")
        _write_record(control, path, record)


def _read_record(workspace: Path, run_id: str) -> JsonDict:
    control, path = _run_path(workspace, run_id)
    payload = json.loads(read_regular_bytes(path, root=control, max_bytes=MAX_RUN_RECORD_BYTES).decode("utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != RUN_SCHEMA_VERSION:
        raise ValueError("Analysis run record is invalid or unsupported")
    if payload.get("run_id") != run_id or payload.get("state") not in RUN_STATES:
        raise ValueError("Analysis run record identity or state is invalid")
    return payload


def _mutate_record(workspace: Path, run_id: str, mutation: Callable[[JsonDict], None]) -> JsonDict:
    control, path = _run_path(workspace, run_id)
    lock = control / ".runs.lock"
    with advisory_file_lock(lock, root=control):
        record = _read_record(workspace, run_id)
        mutation(record)
        _write_record(control, path, record)
        return record


def _write_record(control: Path, path: Path, record: JsonDict) -> None:
    payload = json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False)
    if len(payload.encode("utf-8")) > MAX_RUN_RECORD_BYTES:
        raise ValueError("Analysis run record exceeds its storage limit")
    atomic_write_text(path, payload, root=control)


def validate_run_id(run_id: str) -> str:
    try:
        parsed = uuid.UUID(run_id)
    except (ValueError, AttributeError, TypeError):
        raise ValueError("Invalid analysis run id") from None
    if str(parsed) != run_id:
        raise ValueError("Invalid analysis run id")
    return run_id


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")
