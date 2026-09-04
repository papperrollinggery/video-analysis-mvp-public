"""Explicit, project-local transaction boundary for professional client exports."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ._audio_intelligence_storage import strict_json_loads
from .artifacts import (
    clear_client_current_artifacts,
    load_artifact_registry,
    register_artifact,
    remove_saved_artifact,
    replace_client_current_artifacts,
)
from .client_export_dataset import _canonical_digest, build_client_export_dataset
from .export_templates import ExportTemplateError, preflight_client_layout
from .paths import ProjectPaths
from .readiness import evaluate_project_readiness
from .safe_io import (
    advisory_file_lock,
    atomic_write_bytes,
    atomic_write_text,
    ensure_output_directory,
    read_regular_bytes,
    remove_directory_tree,
    rename_directory_entry,
)

PACKAGE_SCHEMA = "client-export-package/v1"
STATE_SCHEMA = "client-export-state/v1"
MAX_EXPORT_FILE_BYTES = 512 * 1024 * 1024
MAX_RECEIPT_BYTES = 4 * 1024 * 1024
MAX_IDEMPOTENCY_ENTRIES = 64
IDEMPOTENCY_SCHEMA = "client-export-idempotency/v1"
JOURNAL_SCHEMA = "client-export-journal/v1"
SAVED_JOURNAL_SCHEMA = "client-export-saved-journal/v1"
IDEMPOTENCY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
VERSION_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
FORMAT_FILENAMES = {"xlsx": "client_breakdown.xlsx", "pdf": "client_breakdown.pdf"}
Renderer = Callable[..., dict[str, Any]]


@dataclass(frozen=True, slots=True)
class PdfRuntime:
    node_executable: Path
    node_modules_path: Path
    browser_executable: Path
    font_path: Path
    available_fonts: tuple[str, ...]


class ClientExportError(ValueError):
    pass


class ClientExportConflict(ClientExportError):
    pass


def generate_client_export(
    paths: ProjectPaths,
    *,
    formats: list[str] | tuple[str, ...],
    settings: Mapping[str, Any] | None,
    idempotency_key: str,
    cancelled: Callable[[], bool] | None = None,
    pdf_runtime: PdfRuntime | None = None,
    _renderers: Mapping[str, Renderer] | None = None,
) -> dict[str, Any]:
    selected_formats = _formats(formats)
    key = _identifier(idempotency_key, IDEMPOTENCY_PATTERN, "idempotency key")
    options = _settings(settings, selected_formats)
    renderers = dict(_renderers or _default_renderers(pdf_runtime))
    if any(name not in renderers for name in selected_formats):
        raise ClientExportError("A selected export renderer is unavailable")
    user_cancelled = cancelled or (lambda: False)
    paths.ensure()
    client = ensure_output_directory(paths.reports / "client", root=paths.root)
    lock_path = paths.data / ".client-export.lock"
    with (
        advisory_file_lock(paths.data / ".shots.lock", root=paths.root),
        advisory_file_lock(lock_path, root=paths.root),
    ):
        _recover_unlocked(paths, client)
        readiness = evaluate_project_readiness(
            paths.root,
            workspace_root=paths.root.parent,
            _shots_lock_held=True,
        )
        if readiness.get("professional_export_allowed") is not True:
            detail = "; ".join(str(item) for item in readiness.get("reasons", [])[:4])
            raise ClientExportConflict(
                "Professional client export is blocked by current readiness"
                + (f": {detail}" if detail else "")
            )
        dataset = build_client_export_dataset(paths, _shots_lock_held=True)
        try:
            plan = preflight_client_layout(
                dataset,
                options,
                project_root=paths.root,
                available_fonts=pdf_runtime.available_fonts if pdf_runtime else (),
            )
        except ExportTemplateError as exc:
            raise ClientExportError(str(exc)) from exc
        request_core = {
            "dataset_digest": dataset["dataset_digest"],
            "source_generation_id": dataset["source_bindings"]["report_generation_id"],
            "formats": selected_formats,
            "settings": plan["settings"],
        }
        request_digest = _canonical_digest(request_core)
        cancel_marker = client / f".cancel-{request_digest}"
        try:
            cancel_marker.unlink()
        except FileNotFoundError:
            pass

        def is_cancelled() -> bool:
            return user_cancelled() or cancel_marker.exists()

        current = _read_current_unlocked(client, allow_missing=True)
        idempotency = _bind_idempotency(
            client,
            key,
            request_digest,
            request_core["source_generation_id"],
        )
        if (
            current is not None
            and current["source_generation_id"] == request_core["source_generation_id"]
            and current["idempotency_key"] == key
        ):
            if current["request_digest"] != request_digest:
                raise ClientExportConflict("The idempotency key is already bound to a different export request")
            _complete_idempotency(client, key, current["export_id"])
            return current
        if idempotency["state"] == "current":
            raise ClientExportConflict(
                "The idempotent export result is no longer the current package; use a new key"
            )
        if is_cancelled():
            _fail_idempotency(client, key)
            _write_state(client, "cancelled", request_digest=request_digest, reason="cancelled before rendering")
            _remove_cancel_marker(cancel_marker)
            raise ClientExportError("Client export was cancelled")

        stage = client / f".client-export-stage-{secrets.token_hex(12)}"
        stage.mkdir(mode=0o700)
        renderer_options = dict(options)
        frozen_logo = _freeze_logo(paths, stage, plan["settings"].get("logo"))
        if frozen_logo is not None:
            renderer_options["logo_path"] = frozen_logo.relative_to(paths.root).as_posix()
        _write_state(client, "rendering", request_digest=request_digest)
        output_receipts: dict[str, dict[str, Any]] = {}
        try:
            for name in selected_formats:
                if is_cancelled():
                    raise _Cancelled
                output = stage / FORMAT_FILENAMES[name]
                try:
                    render_receipt = renderers[name](
                        dataset,
                        output,
                        settings=renderer_options,
                        project_root=paths.root,
                        cancelled=is_cancelled,
                    )
                except Exception as exc:
                    if is_cancelled() and getattr(
                        exc, "process_group_cleanup_verified", True
                    ) is not False:
                        raise _Cancelled from exc
                    raise ClientExportError(f"Client {name} rendering failed: {type(exc).__name__}") from exc
                _validate_renderer_receipt(name, render_receipt, dataset, plan)
                if frozen_logo is not None:
                    _verify_frozen_logo(frozen_logo, plan["settings"]["logo"], paths.root)
                payload = read_regular_bytes(output, root=paths.root, max_bytes=MAX_EXPORT_FILE_BYTES)
                output_receipts[name] = {
                    "filename": output.name,
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "size_bytes": len(payload),
                    "renderer_schema": render_receipt.get("schema_id"),
                    "renderer_receipt_digest": _canonical_digest(render_receipt),
                }
            if is_cancelled():
                raise _Cancelled
            if frozen_logo is not None:
                frozen_logo.unlink()
                frozen_logo = None
            current_dataset = build_client_export_dataset(paths, _shots_lock_held=True)
            if current_dataset["dataset_digest"] != dataset["dataset_digest"]:
                raise ClientExportConflict("Project evidence changed during client export")
            core = {
                "schema_id": PACKAGE_SCHEMA,
                "state": "current",
                "idempotency_key": key,
                "request_digest": request_digest,
                "dataset_digest": dataset["dataset_digest"],
                "source_generation_id": request_core["source_generation_id"],
                "formats": selected_formats,
                "settings": plan["settings"],
                "outputs": output_receipts,
                "created_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
            }
            export_id = _canonical_digest(core)
            receipt = {**core, "export_id": export_id}
            receipt["receipt_digest"] = _canonical_digest(receipt)
            atomic_write_text(
                stage / "export_receipt.json",
                json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                root=paths.root,
            )
            with advisory_file_lock(
                paths.data / ".client-export-cancel.lock",
                root=paths.root,
            ):
                if is_cancelled():
                    raise _Cancelled
                _write_state(
                    client,
                    "publishing",
                    request_digest=request_digest,
                )
            registry_before = load_artifact_registry(paths)
            journal = {
                "schema_id": JOURNAL_SCHEMA,
                "new_export_id": export_id,
                "old_export_id": current["export_id"] if current is not None else None,
                "registry_revision": registry_before["revision"],
            }
            _write_journal(client, journal)
            moved_previous = _publish_unlocked(client, stage)
            stage = None
            try:
                replace_client_current_artifacts(
                    paths,
                    _current_registry_records(receipt, client / "current"),
                )
            except Exception:
                _rollback_publication(client, moved_previous=moved_previous)
                _remove_journal(client)
                raise
            _finish_publication(client)
            _complete_idempotency(client, key, export_id)
            _remove_journal(client)
            _write_state(client, "current", request_digest=request_digest, export_id=export_id)
            _remove_cancel_marker(cancel_marker)
            return _read_current_unlocked(client, allow_missing=False)
        except _Cancelled as exc:
            _fail_idempotency(client, key)
            _write_state(client, "cancelled", request_digest=request_digest, reason="cancelled before publication")
            _remove_cancel_marker(cancel_marker)
            raise ClientExportError("Client export was cancelled") from exc
        except Exception as exc:
            _fail_idempotency(client, key)
            _write_state(client, "failed", request_digest=request_digest, reason=type(exc).__name__)
            _remove_cancel_marker(cancel_marker)
            if isinstance(exc, ClientExportError):
                raise
            raise ClientExportError(f"Client export transaction failed: {type(exc).__name__}") from exc
        finally:
            if stage is not None and stage.exists():
                _remove_tree(stage, client)


def read_current_export(paths: ProjectPaths) -> dict[str, Any]:
    client = ensure_output_directory(paths.reports / "client", root=paths.root)
    with advisory_file_lock(paths.data / ".client-export.lock", root=paths.root):
        _recover_unlocked(paths, client)
        receipt = _read_current_unlocked(client, allow_missing=False)
        _validate_current_registry(paths, receipt, client / "current")
        return receipt


def recover_client_exports(paths: ProjectPaths) -> dict[str, Any]:
    client = ensure_output_directory(paths.reports / "client", root=paths.root)
    with advisory_file_lock(paths.data / ".client-export.lock", root=paths.root):
        result = _recover_unlocked(paths, client)
        if result["status"] == "completed_publication":
            receipt = _read_current_unlocked(client, allow_missing=False)
            _write_state(
                client,
                "current",
                request_digest=receipt["request_digest"],
                export_id=receipt["export_id"],
            )
        else:
            _mark_interrupted_state(client)
        _cleanup_cancel_markers(client)
        return result


def read_export_state(paths: ProjectPaths) -> dict[str, Any]:
    client = paths.reports / "client"
    path = client / "export_state.json"
    try:
        raw = read_regular_bytes(path, root=paths.root, max_bytes=MAX_RECEIPT_BYTES)
    except FileNotFoundError:
        return {"schema_id": STATE_SCHEMA, "status": "absent"}
    try:
        value = strict_json_loads(raw)
    except (UnicodeError, ValueError) as exc:
        raise ClientExportError("Client export state is invalid") from exc
    required = {
        "schema_id",
        "status",
        "request_digest",
        "export_id",
        "reason",
        "updated_at_utc",
    }
    if (
        type(value) is not dict
        or set(value) != required
        or value.get("schema_id") != STATE_SCHEMA
        or value.get("status") not in {"rendering", "publishing", "current", "failed", "cancelled"}
        or type(value.get("request_digest")) is not str
        or re.fullmatch(r"[0-9a-f]{64}", value["request_digest"]) is None
    ):
        raise ClientExportError("Client export state schema is invalid")
    return value


def read_export_center(paths: ProjectPaths) -> dict[str, Any]:
    client = paths.reports / "client"
    if not client.exists() and not client.is_symlink():
        return {
            "schema_id": "client-export-center/v1",
            "state": {"schema_id": STATE_SCHEMA, "status": "absent"},
            "current": None,
            "saved": [],
        }
    ensure_output_directory(client, root=paths.root)
    with advisory_file_lock(paths.data / ".client-export.lock", root=paths.root):
        _recover_unlocked(paths, client)
        state = read_export_state(paths)
        current_receipt = _read_current_unlocked(client, allow_missing=True)
        current = None
        if current_receipt is not None:
            current = {
                "lifecycle_state": (
                    "current"
                    if _current_registry_matches(paths, current_receipt, client / "current")
                    else "stale"
                ),
                "receipt": current_receipt,
                "downloads": _package_downloads(
                    paths,
                    Path("reports/client/current"),
                    current_receipt,
                ),
            }
        registry = load_artifact_registry(paths)
        saved_rows = []
        for record in registry["artifacts"]:
            if record["scope"] != "client_export" or record["state"] != "saved":
                continue
            relative = Path(record["relative_path"])
            version_id = relative.name
            receipt = _read_package(paths.root / relative)
            if receipt["export_id"] != record["generation_id"]:
                raise ClientExportError("Saved export registry generation is invalid")
            saved_rows.append(
                {
                    "version_id": version_id,
                    "export_id": receipt["export_id"],
                    "formats": receipt["formats"],
                    "created_at_utc": receipt["created_at_utc"],
                    "size_bytes": sum(
                        item["size_bytes"] for item in receipt["outputs"].values()
                    ),
                    "downloads": _package_downloads(paths, relative, receipt),
                }
            )
        return {
            "schema_id": "client-export-center/v1",
            "state": state,
            "current": current,
            "saved": sorted(saved_rows, key=lambda item: item["version_id"]),
        }


def _package_downloads(
    paths: ProjectPaths,
    relative_root: Path,
    receipt: dict[str, Any],
) -> dict[str, str]:
    return {
        name: "/files/"
        + quote(
            (Path(paths.root.name) / relative_root / output["filename"]).as_posix()
        )
        for name, output in receipt["outputs"].items()
    }


def cancel_client_export(paths: ProjectPaths, request_digest: str) -> dict[str, Any]:
    digest = _identifier(request_digest, re.compile(r"[0-9a-f]{64}"), "request digest")
    with advisory_file_lock(
        paths.data / ".client-export-cancel.lock",
        root=paths.root,
    ):
        state = read_export_state(paths)
        if state["status"] == "absent":
            return {"status": "absent", "request_digest": digest}
        if state["request_digest"] != digest:
            raise ClientExportConflict("Another export request owns the current state")
        if state["status"] != "rendering":
            return {"status": state["status"], "request_digest": digest}
        client = ensure_output_directory(paths.reports / "client", root=paths.root)
        atomic_write_text(
            client / f".cancel-{digest}",
            "cancel\n",
            root=paths.root,
        )
        return {"status": "cancel_requested", "request_digest": digest}


def save_current_export(paths: ProjectPaths, version_id: str) -> dict[str, Any]:
    version = _identifier(version_id, VERSION_PATTERN, "version id")
    client = ensure_output_directory(paths.reports / "client", root=paths.root)
    with advisory_file_lock(paths.data / ".client-export.lock", root=paths.root):
        _recover_unlocked(paths, client)
        receipt = _read_current_unlocked(client, allow_missing=False)
        try:
            _validate_current_registry(paths, receipt, client / "current")
        except ClientExportConflict as exc:
            raise ClientExportConflict("A stale client export cannot be saved as a version") from exc
        saved = ensure_output_directory(client / "saved", root=paths.root)
        target = saved / version
        if target.exists() or target.is_symlink():
            existing = _read_package(target)
            if existing["export_id"] == receipt["export_id"]:
                return existing
            raise ClientExportConflict("The saved version id already belongs to a different export")
        stage = saved / f".client-export-save-{secrets.token_hex(12)}"
        artifact_id = _saved_artifact_id(receipt["export_id"], version)
        try:
            _copy_package(paths, client / "current", stage)
            _write_saved_journal(
                client,
                {
                    "schema_id": SAVED_JOURNAL_SCHEMA,
                    "operation": "save",
                    "version_id": version,
                    "export_id": receipt["export_id"],
                    "artifact_id": artifact_id,
                    "tombstone_name": None,
                },
            )
            rename_directory_entry(
                saved,
                stage.name,
                version,
                root=paths.root,
            )
            saved_record = _saved_registry_record(receipt, version)
            try:
                register_artifact(paths, saved_record)
            except Exception:
                _remove_tree(target, saved)
                _remove_saved_journal(client)
                raise
            _remove_saved_journal(client)
        finally:
            if stage.exists():
                _remove_tree(stage, saved)
        return _read_package(target)


def delete_saved_export(paths: ProjectPaths, version_id: str) -> dict[str, Any]:
    version = _identifier(version_id, VERSION_PATTERN, "version id")
    client = ensure_output_directory(paths.reports / "client", root=paths.root)
    with advisory_file_lock(paths.data / ".client-export.lock", root=paths.root):
        saved = ensure_output_directory(client / "saved", root=paths.root)
        target = saved / version
        if not target.exists() and not target.is_symlink():
            return {"status": "absent", "version_id": version}
        receipt = _read_package(target)
        tombstone_name = f".client-export-delete-{secrets.token_hex(12)}"
        artifact_id = _saved_artifact_id(receipt["export_id"], version)
        _write_saved_journal(
            client,
            {
                "schema_id": SAVED_JOURNAL_SCHEMA,
                "operation": "delete",
                "version_id": version,
                "export_id": receipt["export_id"],
                "artifact_id": artifact_id,
                "tombstone_name": tombstone_name,
            },
        )
        rename_directory_entry(
            saved,
            version,
            tombstone_name,
            root=paths.root,
        )
        try:
            remove_saved_artifact(
                paths,
                artifact_id,
            )
        except Exception:
            rename_directory_entry(
                saved,
                tombstone_name,
                version,
                root=paths.root,
            )
            _remove_saved_journal(client)
            raise
        _remove_tree(saved / tombstone_name, saved)
        _remove_saved_journal(client)
        return {"status": "deleted", "version_id": version}


def pdf_runtime_from_environment(
    environment: Mapping[str, str] | None = None,
) -> PdfRuntime | None:
    values = os.environ if environment is None else environment
    required = {
        "node_executable": values.get("VEW_PDF_NODE", ""),
        "node_modules_path": values.get("VEW_PDF_NODE_MODULES", ""),
        "browser_executable": values.get("VEW_PDF_BROWSER", ""),
        "font_path": values.get("VEW_PDF_FONT", ""),
    }
    if not any(required.values()):
        return None
    if not all(required.values()):
        raise ClientExportError("PDF runtime environment must define node, modules, browser, and font together")
    font_name = values.get("VEW_PDF_FONT_NAME", "Noto Sans CJK SC").strip()
    if not font_name or len(font_name.encode("utf-8")) > 256:
        raise ClientExportError("PDF runtime font name is invalid")
    return PdfRuntime(
        node_executable=Path(required["node_executable"]),
        node_modules_path=Path(required["node_modules_path"]),
        browser_executable=Path(required["browser_executable"]),
        font_path=Path(required["font_path"]),
        available_fonts=(font_name,),
    )


def _default_renderers(pdf_runtime: PdfRuntime | None) -> dict[str, Renderer]:
    from .export_pdf import render_client_pdf
    from .export_xlsx import render_client_xlsx

    def render_xlsx(dataset: dict[str, Any], output: Path, **kwargs: Any) -> dict[str, Any]:
        return render_client_xlsx(
            dataset,
            output,
            settings=kwargs["settings"],
            project_root=kwargs["project_root"],
        )

    renderers: dict[str, Renderer] = {"xlsx": render_xlsx}
    if pdf_runtime is not None:
        def render_pdf(dataset: dict[str, Any], output: Path, **kwargs: Any) -> dict[str, Any]:
            return render_client_pdf(
                dataset,
                output,
                settings=kwargs["settings"],
                project_root=kwargs["project_root"],
                available_fonts=pdf_runtime.available_fonts,
                node_executable=pdf_runtime.node_executable,
                node_modules_path=pdf_runtime.node_modules_path,
                browser_executable=pdf_runtime.browser_executable,
                font_path=pdf_runtime.font_path,
                cancelled=kwargs.get("cancelled"),
            )

        renderers["pdf"] = render_pdf
    return renderers


def _read_current_unlocked(client: Path, *, allow_missing: bool) -> dict[str, Any] | None:
    current = client / "current"
    if not current.exists() and not current.is_symlink():
        if allow_missing:
            return None
        raise ClientExportError("No current client export is available")
    return _read_package(current)


def _read_package(root: Path) -> dict[str, Any]:
    try:
        raw = read_regular_bytes(root / "export_receipt.json", root=root.parent.parent, max_bytes=MAX_RECEIPT_BYTES)
        receipt = strict_json_loads(raw)
    except (OSError, UnicodeError, ValueError) as exc:
        raise ClientExportError("Client export receipt is missing or invalid") from exc
    required = {
        "schema_id", "state", "idempotency_key", "request_digest", "dataset_digest",
        "source_generation_id", "formats", "settings", "outputs", "created_at_utc",
        "export_id", "receipt_digest",
    }
    if type(receipt) is not dict or set(receipt) != required or receipt.get("schema_id") != PACKAGE_SCHEMA:
        raise ClientExportError("Client export receipt schema is invalid")
    stored_receipt_digest = receipt["receipt_digest"]
    if _canonical_digest({key: value for key, value in receipt.items() if key != "receipt_digest"}) != stored_receipt_digest:
        raise ClientExportError("Client export receipt digest is invalid")
    if _canonical_digest({key: value for key, value in receipt.items() if key not in {"export_id", "receipt_digest"}}) != receipt["export_id"]:
        raise ClientExportError("Client export id is invalid")
    if receipt.get("formats") != sorted(receipt.get("outputs", {})):
        raise ClientExportError("Client export output coverage is invalid")
    for name, output in receipt["outputs"].items():
        if name not in FORMAT_FILENAMES or type(output) is not dict or output.get("filename") != FORMAT_FILENAMES[name]:
            raise ClientExportError("Client export output receipt is invalid")
        payload = read_regular_bytes(root / output["filename"], root=root.parent.parent, max_bytes=MAX_EXPORT_FILE_BYTES)
        if output.get("sha256") != hashlib.sha256(payload).hexdigest() or output.get("size_bytes") != len(payload):
            raise ClientExportError("Client export output digest is invalid")
    return receipt


def _copy_package(paths: ProjectPaths, source: Path, target: Path) -> None:
    receipt = _read_package(source)
    target.mkdir(mode=0o700)
    filenames = ["export_receipt.json"] + [
        receipt["outputs"][name]["filename"] for name in receipt["formats"]
    ]
    for filename in filenames:
        maximum = MAX_RECEIPT_BYTES if filename == "export_receipt.json" else MAX_EXPORT_FILE_BYTES
        payload = read_regular_bytes(source / filename, root=paths.root, max_bytes=maximum)
        atomic_write_bytes(target / filename, payload, root=paths.root)


def _freeze_logo(
    paths: ProjectPaths,
    stage: Path,
    logo: dict[str, Any] | None,
) -> Path | None:
    if logo is None:
        return None
    raw = read_regular_bytes(
        paths.root / logo["path"],
        root=paths.root,
        max_bytes=16 * 1024 * 1024,
    )
    if hashlib.sha256(raw).hexdigest() != logo["sha256"] or len(raw) != logo["size_bytes"]:
        raise ClientExportConflict("Client logo changed after layout preflight")
    suffix = ".png" if logo["media_type"] == "image/png" else ".jpg"
    frozen = stage / f".client-logo{suffix}"
    atomic_write_bytes(frozen, raw, root=paths.root)
    return frozen


def _verify_frozen_logo(
    path: Path,
    logo: dict[str, Any],
    project_root: Path,
) -> None:
    raw = read_regular_bytes(path, root=project_root, max_bytes=16 * 1024 * 1024)
    if hashlib.sha256(raw).hexdigest() != logo["sha256"] or len(raw) != logo["size_bytes"]:
        raise ClientExportConflict("Frozen client logo changed during rendering")


def _validate_renderer_receipt(
    name: str,
    receipt: Any,
    dataset: dict[str, Any],
    plan: dict[str, Any],
) -> None:
    if type(receipt) is not dict or type(receipt.get("schema_id")) is not str:
        raise ClientExportError("Client renderer returned an invalid receipt")
    if receipt["schema_id"] == "fixture-render/v1":
        if receipt.get("dataset_digest") != dataset["dataset_digest"]:
            raise ClientExportError("Fixture renderer receipt is bound to another dataset")
        return
    expected_schema = {"xlsx": "xlsx-render-receipt/v1", "pdf": "pdf-render-receipt/v1"}[name]
    if (
        receipt.get("schema_id") != expected_schema
        or receipt.get("dataset_digest") != dataset["dataset_digest"]
        or receipt.get("template_id") != plan["template"]["template_id"]
        or receipt.get("template_version") != plan["template"]["template_version"]
        or receipt.get("template_digest") != plan["template"]["template_digest"]
        or not _same_renderer_settings(plan["settings"], receipt.get("settings"))
    ):
        raise ClientExportError("Client renderer receipt does not match the frozen export plan")


def _same_renderer_settings(expected: dict[str, Any], actual: Any) -> bool:
    if type(actual) is not dict:
        return False
    left, right = dict(expected), dict(actual)
    left["formats"] = right.get("formats")
    for value in (left, right):
        logo = value.get("logo")
        if type(logo) is dict:
            value["logo"] = {key: item for key, item in logo.items() if key != "path"}
    return left == right


def _publish_unlocked(client: Path, stage: Path) -> bool:
    current = client / "current"
    previous = client / ".client-export-previous"
    if previous.exists() or previous.is_symlink():
        _remove_tree(previous, client)
    moved_previous = False
    try:
        if current.exists() or current.is_symlink():
            _require_directory(current, client)
            os.replace(current, previous)
            moved_previous = True
        os.replace(stage, current)
    except Exception:
        if moved_previous and not current.exists() and previous.exists():
            os.replace(previous, current)
        raise
    return moved_previous


def _rollback_publication(client: Path, *, moved_previous: bool) -> None:
    current = client / "current"
    previous = client / ".client-export-previous"
    if current.exists():
        _remove_tree(current, client)
    if moved_previous and previous.exists():
        os.replace(previous, current)


def _finish_publication(client: Path) -> None:
    previous = client / ".client-export-previous"
    if previous.exists():
        _remove_tree(previous, client)


def _recover_unlocked(paths: ProjectPaths, client: Path) -> dict[str, Any]:
    current = client / "current"
    previous = client / ".client-export-previous"
    restored = False
    _recover_saved_unlocked(paths, client)
    journal = _read_journal(client)
    if journal is not None:
        current_receipt = _optional_package(current)
        previous_receipt = _optional_package(previous)
        registry_export_id = _registry_current_export_id(paths)
        if (
            current_receipt is not None
            and current_receipt["export_id"] == journal["old_export_id"]
            and previous_receipt is None
            and _current_registry_matches(paths, current_receipt, current)
        ):
            _cleanup_export_stages(client)
            _remove_journal(client)
            return {"status": "aborted_before_publication"}
        if (
            current_receipt is not None
            and current_receipt["export_id"] == journal["new_export_id"]
            and _current_registry_matches(paths, current_receipt, current)
        ):
            _finish_publication(client)
            _complete_idempotency(
                client,
                current_receipt["idempotency_key"],
                current_receipt["export_id"],
            )
            _remove_journal(client)
            return {"status": "completed_publication"}
        if (previous.exists() or previous.is_symlink()) and previous_receipt is None:
            raise ClientExportError("Client export previous package is invalid during recovery")
        if previous_receipt is not None:
            if previous_receipt["export_id"] != journal["old_export_id"]:
                raise ClientExportError("Client export previous package does not match the journal")
            if current.exists() or current.is_symlink():
                _remove_tree(current, client)
            os.replace(previous, current)
            restored = True
            if registry_export_id == journal["new_export_id"]:
                replace_client_current_artifacts(
                    paths,
                    _current_registry_records(previous_receipt, current),
                )
        else:
            if registry_export_id == journal["new_export_id"]:
                raise ClientExportError(
                    "Client export recovery cannot reconcile committed registry metadata"
                )
            if registry_export_id is not None:
                clear_client_current_artifacts(paths)
            if current.exists() or current.is_symlink():
                _remove_tree(current, client)
        _remove_journal(client)
    if previous.exists() or previous.is_symlink():
        _require_directory(previous, client)
        if not current.exists() and not current.is_symlink():
            os.replace(previous, current)
            restored = True
        else:
            try:
                _read_package(current)
            except ClientExportError:
                _remove_tree(current, client)
                os.replace(previous, current)
                restored = True
            else:
                _remove_tree(previous, client)
    _cleanup_export_stages(client)
    return {"status": "restored_previous" if restored else "clean"}


def _cleanup_export_stages(client: Path) -> None:
    for candidate in list(client.glob(".client-export-stage-*")):
        _remove_tree(candidate, client)


def _optional_package(path: Path) -> dict[str, Any] | None:
    if not path.exists() and not path.is_symlink():
        return None
    try:
        return _read_package(path)
    except ClientExportError:
        return None


def _registry_current_export_id(paths: ProjectPaths) -> str | None:
    registry = load_artifact_registry(paths)
    matches = [
        record
        for record in registry["artifacts"]
        if record["artifact_id"] == "client_current_package"
        and record["state"] == "current"
    ]
    if not matches:
        return None
    if len(matches) != 1:
        raise ClientExportError("Client current registry metadata is ambiguous")
    return matches[0]["generation_id"]


def _validate_current_registry(
    paths: ProjectPaths,
    receipt: dict[str, Any],
    package_root: Path,
) -> None:
    if not _current_registry_matches(paths, receipt, package_root):
        raise ClientExportConflict(
            "The client export is stale or not fully committed in the artifact registry"
        )


def _current_registry_matches(
    paths: ProjectPaths,
    receipt: dict[str, Any],
    package_root: Path,
) -> bool:
    try:
        expected = sorted(
            _current_registry_records(receipt, package_root),
            key=lambda item: item["artifact_id"],
        )
        registry = load_artifact_registry(paths)
    except (OSError, ValueError):
        return False
    actual = sorted(
        (
            record
            for record in registry["artifacts"]
            if record["scope"] == "client_export"
            and record["retention"] == "current"
        ),
        key=lambda item: item["artifact_id"],
    )
    return actual == expected


def _write_journal(client: Path, value: dict[str, Any]) -> None:
    atomic_write_text(
        client / "export_journal.json",
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        root=client.parent.parent,
    )


def _read_journal(client: Path) -> dict[str, Any] | None:
    path = client / "export_journal.json"
    try:
        raw = read_regular_bytes(path, root=client.parent.parent, max_bytes=MAX_RECEIPT_BYTES)
    except FileNotFoundError:
        return None
    try:
        value = strict_json_loads(raw)
    except (UnicodeError, ValueError) as exc:
        raise ClientExportError("Client export journal is invalid") from exc
    if (
        type(value) is not dict
        or set(value) != {
            "schema_id",
            "new_export_id",
            "old_export_id",
            "registry_revision",
        }
        or value.get("schema_id") != JOURNAL_SCHEMA
        or type(value.get("new_export_id")) is not str
        or re.fullmatch(r"[0-9a-f]{64}", value["new_export_id"]) is None
        or (
            value.get("old_export_id") is not None
            and (
                type(value["old_export_id"]) is not str
                or re.fullmatch(r"[0-9a-f]{64}", value["old_export_id"]) is None
            )
        )
        or type(value.get("registry_revision")) is not int
        or value["registry_revision"] < 0
    ):
        raise ClientExportError("Client export journal schema is invalid")
    return value


def _remove_journal(client: Path) -> None:
    try:
        (client / "export_journal.json").unlink()
    except FileNotFoundError:
        pass


def _recover_saved_unlocked(paths: ProjectPaths, client: Path) -> None:
    journal = _read_saved_journal(client)
    if journal is None:
        saved = client / "saved"
        if saved.exists() and not saved.is_symlink():
            _cleanup_saved_stages(saved)
        return
    saved = ensure_output_directory(client / "saved", root=paths.root)
    target = saved / journal["version_id"]
    record_exists = _saved_record_exists(paths, journal["artifact_id"])
    if journal["operation"] == "save":
        if target.exists() or target.is_symlink():
            receipt = _read_package(target)
            if receipt["export_id"] != journal["export_id"]:
                raise ClientExportError("Saved export journal does not match the published package")
            if not record_exists:
                register_artifact(
                    paths,
                    _saved_registry_record(receipt, journal["version_id"]),
                )
        elif record_exists:
            raise ClientExportError("Saved export registry exists without its package")
    else:
        tombstone = saved / journal["tombstone_name"]
        if tombstone.exists() or tombstone.is_symlink():
            if record_exists:
                if target.exists() or target.is_symlink():
                    raise ClientExportError("Saved export recovery found duplicate package paths")
                rename_directory_entry(
                    saved,
                    journal["tombstone_name"],
                    journal["version_id"],
                    root=paths.root,
                )
            else:
                _remove_tree(tombstone, saved)
        elif record_exists and not target.exists():
            raise ClientExportError("Saved export delete journal cannot restore its package")
    _cleanup_saved_stages(saved)
    _remove_saved_journal(client)


def _cleanup_saved_stages(saved: Path) -> None:
    for candidate in list(saved.glob(".client-export-save-*")):
        _remove_tree(candidate, saved)


def _saved_record_exists(paths: ProjectPaths, artifact_id: str) -> bool:
    registry = load_artifact_registry(paths)
    matches = [
        record
        for record in registry["artifacts"]
        if record["artifact_id"] == artifact_id
    ]
    if len(matches) > 1:
        raise ClientExportError("Saved export registry metadata is ambiguous")
    return bool(matches)


def _write_saved_journal(client: Path, value: dict[str, Any]) -> None:
    atomic_write_text(
        client / "saved_journal.json",
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        root=client.parent.parent,
    )


def _read_saved_journal(client: Path) -> dict[str, Any] | None:
    path = client / "saved_journal.json"
    try:
        raw = read_regular_bytes(path, root=client.parent.parent, max_bytes=MAX_RECEIPT_BYTES)
    except FileNotFoundError:
        return None
    try:
        value = strict_json_loads(raw)
    except (UnicodeError, ValueError) as exc:
        raise ClientExportError("Saved export journal is invalid") from exc
    if (
        type(value) is not dict
        or set(value) != {
            "schema_id",
            "operation",
            "version_id",
            "export_id",
            "artifact_id",
            "tombstone_name",
        }
        or value.get("schema_id") != SAVED_JOURNAL_SCHEMA
        or value.get("operation") not in {"save", "delete"}
        or type(value.get("version_id")) is not str
        or VERSION_PATTERN.fullmatch(value["version_id"]) is None
        or type(value.get("export_id")) is not str
        or re.fullmatch(r"[0-9a-f]{64}", value["export_id"]) is None
        or type(value.get("artifact_id")) is not str
        or not value["artifact_id"].startswith("client-saved-")
        or (
            value["operation"] == "save"
            and value.get("tombstone_name") is not None
        )
        or (
            value["operation"] == "delete"
            and (
                type(value.get("tombstone_name")) is not str
                or re.fullmatch(r"\.client-export-delete-[0-9a-f]{24}", value["tombstone_name"]) is None
            )
        )
    ):
        raise ClientExportError("Saved export journal schema is invalid")
    return value


def _remove_saved_journal(client: Path) -> None:
    try:
        (client / "saved_journal.json").unlink()
    except FileNotFoundError:
        pass


def _current_registry_records(
    receipt: dict[str, Any], package_root: Path
) -> list[dict[str, Any]]:
    generation_id = receipt["export_id"]
    source_generation_id = receipt["source_generation_id"]
    receipt_payload = read_regular_bytes(
        package_root / "export_receipt.json",
        root=package_root.parent.parent,
        max_bytes=MAX_RECEIPT_BYTES,
    )
    records = [
        _registry_record(
            "client_current_package",
            "reports/client/current",
            "package",
            generation_id,
            source_generation_id,
            {
                "algorithm": "sha256",
                "sha256": _canonical_digest(receipt["outputs"]),
                "size_bytes": sum(item["size_bytes"] for item in receipt["outputs"].values()),
            },
            state="current",
            retention="current",
        ),
        _registry_record(
            "client_export_receipt",
            "reports/client/current/export_receipt.json",
            "file",
            generation_id,
            source_generation_id,
            {
                "algorithm": "sha256",
                "sha256": hashlib.sha256(receipt_payload).hexdigest(),
                "size_bytes": len(receipt_payload),
            },
            state="current",
            retention="current",
        ),
    ]
    for name, output in receipt["outputs"].items():
        records.append(
            _registry_record(
                f"client_breakdown_{name}",
                f"reports/client/current/{output['filename']}",
                "file",
                generation_id,
                source_generation_id,
                {"algorithm": "sha256", "sha256": output["sha256"], "size_bytes": output["size_bytes"]},
                state="current",
                retention="current",
            )
        )
    return records


def _saved_registry_record(receipt: dict[str, Any], version: str) -> dict[str, Any]:
    return _registry_record(
        _saved_artifact_id(receipt["export_id"], version),
        f"reports/client/saved/{version}",
        "package",
        receipt["export_id"],
        receipt["source_generation_id"],
        {
            "algorithm": "sha256",
            "sha256": _canonical_digest(receipt["outputs"]),
            "size_bytes": sum(item["size_bytes"] for item in receipt["outputs"].values()),
        },
        state="saved",
        retention="saved",
    )


def _saved_artifact_id(export_id: str, version: str) -> str:
    return f"client-saved-{hashlib.sha256(f'{export_id}:{version}'.encode()).hexdigest()[:24]}"


def _registry_record(
    artifact_id: str,
    relative_path: str,
    kind: str,
    generation_id: str,
    source_generation_id: str,
    digest: dict[str, Any],
    *,
    state: str,
    retention: str,
) -> dict[str, Any]:
    return {
        "artifact_id": artifact_id,
        "scope": "client_export",
        "kind": kind,
        "relative_path": relative_path,
        "state": state,
        "retention": retention,
        "generation_id": generation_id,
        "source_generation_id": source_generation_id,
        "digest": digest,
        "stale_reason": None,
    }


def _bind_idempotency(
    client: Path,
    key: str,
    request_digest: str,
    source_generation_id: str,
) -> dict[str, Any]:
    ledger = _load_idempotency(client)
    if ledger["source_generation_id"] != source_generation_id:
        ledger = {
            "schema_id": IDEMPOTENCY_SCHEMA,
            "source_generation_id": source_generation_id,
            "entries": [],
        }
    for entry in ledger["entries"]:
        if entry["key"] != key:
            continue
        if entry["request_digest"] != request_digest:
            raise ClientExportConflict(
                "The idempotency key is already bound to a different export request"
            )
        return entry
    entries = list(ledger["entries"])
    if len(entries) >= MAX_IDEMPOTENCY_ENTRIES:
        raise ClientExportConflict(
            "The bounded idempotency ledger is full; explicit maintenance is required before new keys"
        )
    entry = {
        "key": key,
        "request_digest": request_digest,
        "export_id": None,
        "state": "pending",
    }
    entries.append(entry)
    _write_idempotency(
        client,
        {
            "schema_id": IDEMPOTENCY_SCHEMA,
            "source_generation_id": source_generation_id,
            "entries": entries,
        },
    )
    return entry


def _complete_idempotency(client: Path, key: str, export_id: str) -> None:
    _update_idempotency(client, key, state="current", export_id=export_id)


def _fail_idempotency(client: Path, key: str) -> None:
    _update_idempotency(client, key, state="failed", export_id=None)


def _update_idempotency(
    client: Path,
    key: str,
    *,
    state: str,
    export_id: str | None,
) -> None:
    ledger = _load_idempotency(client)
    entries = []
    found = False
    for entry in ledger["entries"]:
        item = dict(entry)
        if item["key"] == key:
            item["state"] = state
            item["export_id"] = export_id
            found = True
        entries.append(item)
    if found:
        _write_idempotency(
            client,
            {
                "schema_id": IDEMPOTENCY_SCHEMA,
                "source_generation_id": ledger["source_generation_id"],
                "entries": entries,
            },
        )


def _load_idempotency(client: Path) -> dict[str, Any]:
    path = client / "idempotency.json"
    try:
        raw = read_regular_bytes(path, root=client.parent.parent, max_bytes=MAX_RECEIPT_BYTES)
    except FileNotFoundError:
        return {
            "schema_id": IDEMPOTENCY_SCHEMA,
            "source_generation_id": None,
            "entries": [],
        }
    try:
        value = strict_json_loads(raw)
    except (UnicodeError, ValueError) as exc:
        raise ClientExportError("Client export idempotency ledger is invalid") from exc
    if (
        type(value) is not dict
        or set(value) != {"schema_id", "source_generation_id", "entries"}
        or value.get("schema_id") != IDEMPOTENCY_SCHEMA
        or type(value.get("entries")) is not list
        or len(value["entries"]) > MAX_IDEMPOTENCY_ENTRIES
        or (
            value.get("source_generation_id") is not None
            and (
                type(value["source_generation_id"]) is not str
                or not value["source_generation_id"]
                or len(value["source_generation_id"].encode("utf-8")) > 256
            )
        )
    ):
        raise ClientExportError("Client export idempotency ledger schema is invalid")
    seen: set[str] = set()
    for entry in value["entries"]:
        if (
            type(entry) is not dict
            or set(entry) != {"key", "request_digest", "export_id", "state"}
            or type(entry.get("key")) is not str
            or IDEMPOTENCY_PATTERN.fullmatch(entry["key"]) is None
            or type(entry.get("request_digest")) is not str
            or re.fullmatch(r"[0-9a-f]{64}", entry["request_digest"]) is None
            or entry.get("state") not in {"pending", "current", "failed"}
            or (
                entry.get("export_id") is not None
                and (
                    type(entry["export_id"]) is not str
                    or re.fullmatch(r"[0-9a-f]{64}", entry["export_id"]) is None
                )
            )
            or entry["key"] in seen
        ):
            raise ClientExportError("Client export idempotency entry is invalid")
        seen.add(entry["key"])
    return value


def _write_idempotency(client: Path, ledger: dict[str, Any]) -> None:
    atomic_write_text(
        client / "idempotency.json",
        json.dumps(ledger, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        root=client.parent.parent,
    )


def _write_state(
    client: Path,
    status: str,
    *,
    request_digest: str,
    export_id: str | None = None,
    reason: str | None = None,
) -> None:
    payload = {
        "schema_id": STATE_SCHEMA,
        "status": status,
        "request_digest": request_digest,
        "export_id": export_id,
        "reason": reason,
        "updated_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
    }
    atomic_write_text(
        client / "export_state.json",
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        root=client.parent.parent,
    )


def _remove_cancel_marker(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _cleanup_cancel_markers(client: Path) -> None:
    for marker in client.glob(".cancel-*"):
        if re.fullmatch(r"\.cancel-[0-9a-f]{64}", marker.name) is not None:
            _remove_cancel_marker(marker)


def _mark_interrupted_state(client: Path) -> None:
    path = client / "export_state.json"
    try:
        raw = read_regular_bytes(
            path,
            root=client.parent.parent,
            max_bytes=MAX_RECEIPT_BYTES,
        )
    except FileNotFoundError:
        return
    try:
        value = strict_json_loads(raw)
    except (UnicodeError, ValueError):
        return
    if type(value) is dict and value.get("status") in {"rendering", "publishing"}:
        request_digest = value.get("request_digest")
        if type(request_digest) is str and re.fullmatch(r"[0-9a-f]{64}", request_digest):
            _write_state(
                client,
                "failed",
                request_digest=request_digest,
                reason="interrupted_before_completion",
            )


def _formats(value: list[str] | tuple[str, ...]) -> list[str]:
    if type(value) not in {list, tuple} or not value or any(type(item) is not str for item in value):
        raise ClientExportError("Export formats must be a non-empty list")
    normalized = sorted(set(value))
    if len(normalized) != len(value) or any(item not in FORMAT_FILENAMES for item in normalized):
        raise ClientExportError("Export formats must contain unique xlsx/pdf values")
    return normalized


def _settings(value: Mapping[str, Any] | None, formats: list[str]) -> dict[str, Any]:
    options = dict(value or {})
    options["formats"] = formats
    try:
        json.dumps(options, ensure_ascii=False, allow_nan=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ClientExportError("Export settings must be finite JSON data") from exc
    return options


def _identifier(value: str, pattern: re.Pattern[str], label: str) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise ValueError(f"Invalid {label}")
    return value


def _require_directory(path: Path, parent: Path) -> None:
    try:
        info = path.lstat()
        path.relative_to(parent)
    except (OSError, ValueError) as exc:
        raise ClientExportError("Unsafe client export directory") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ClientExportError("Unsafe client export directory")


def _remove_tree(path: Path, parent: Path) -> None:
    try:
        remove_directory_tree(path, root=parent)
    except (OSError, ValueError) as exc:
        raise ClientExportError("Unsafe client export directory removal") from exc


class _Cancelled(Exception):
    pass
