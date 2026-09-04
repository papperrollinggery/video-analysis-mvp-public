"""Explicit, rollback-safe preparation of pre-readiness-v3 projects."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from .artifacts import load_artifact_registry
from .paths import ProjectPaths
from .readiness import READINESS_SCHEMA_VERSION
from .safe_io import advisory_file_lock, atomic_write_bytes, read_regular_bytes
from .schemas import load_json
from .synthesis import (
    REPORT_GENERATION_SCHEMA_VERSION,
    verify_report_generation_manifest,
)
from .workspace_api import _invalidate_report_for_review, project_write_lock

CURRENT_READINESS_SCHEMA_VERSION = READINESS_SCHEMA_VERSION
CURRENT_REPORT_GENERATION_SCHEMA_VERSION = REPORT_GENERATION_SCHEMA_VERSION
MAX_MIGRATION_METADATA_BYTES = 4 * 1024 * 1024
MIGRATION_REASON = "schema_migration_required"
MIGRATION_RECEIPT_NAME = "migration_receipt.json"
BACKUP_STATE_NAME = "backup_state.json"


class ProjectMigrationError(ValueError):
    pass


def inspect_project_migration(paths: ProjectPaths) -> dict[str, Any]:
    try:
        readiness = load_json(paths.data / "readiness.json")
        manifest = load_json(paths.manifest)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ProjectMigrationError(
            "Project readiness or manifest is missing or invalid"
        ) from exc
    if type(readiness) is not dict or type(manifest) is not dict:
        raise ProjectMigrationError("Project readiness and manifest must be objects")
    readiness_version = readiness.get("schema_version")
    generation_present = "report_generation" in manifest
    generation = manifest.get("report_generation")
    if generation_present and (
        type(generation) is not dict or "schema_version" not in generation
    ):
        raise ProjectMigrationError(
            "Project report-generation receipt is malformed"
        )
    report_version = generation.get("schema_version") if generation_present else None
    if (
        type(readiness_version) is not int
        or readiness_version < 1
        or readiness_version > CURRENT_READINESS_SCHEMA_VERSION
    ):
        raise ProjectMigrationError("Project readiness schema version is unsupported")
    if generation_present and (
        type(report_version) is not int
        or report_version < 1
        or report_version > CURRENT_REPORT_GENERATION_SCHEMA_VERSION
    ):
        raise ProjectMigrationError(
            "Project report-generation schema version is unsupported"
        )
    invalidation = manifest.get("report_invalidation")
    prepared = (
        type(invalidation) is dict
        and invalidation.get("reason") == MIGRATION_REASON
        and invalidation.get("requires_finalize") is True
        and manifest.get("status") == "review_pending"
    )
    if prepared:
        receipt = _migration_receipt(paths)
        if receipt is None:
            raise ProjectMigrationError(
                "Migration preparation receipt is missing or invalid"
            )
        status = "prepared"
        action = "Run the normal Finalize action to write current schema receipts."
    elif (
        readiness_version == CURRENT_READINESS_SCHEMA_VERSION
        and report_version == CURRENT_REPORT_GENERATION_SCHEMA_VERSION
    ):
        current, _reasons = verify_report_generation_manifest(paths)
        status = "current" if current else "finalize_required"
        action = "No schema migration is required." if current else "Resolve current evidence and Finalize again."
    elif (
        readiness_version == CURRENT_READINESS_SCHEMA_VERSION
        and report_version is None
    ):
        status = "finalize_required"
        action = "Resolve current evidence and Finalize again."
    elif (
        type(readiness_version) is int
        and readiness_version < CURRENT_READINESS_SCHEMA_VERSION
    ) or (
        type(report_version) is int
        and report_version < CURRENT_REPORT_GENERATION_SCHEMA_VERSION
    ):
        status = "migration_required"
        action = "Re-run with --apply to invalidate legacy publication before Finalize."
    else:
        raise ProjectMigrationError("Project schema version is unsupported")
    return {
        "schema_id": "project-migration-status/v1",
        "project_id": paths.root.name,
        "status": status,
        "readiness_schema_version": readiness_version,
        "report_generation_schema_version": report_version,
        "target_readiness_schema_version": CURRENT_READINESS_SCHEMA_VERSION,
        "target_report_generation_schema_version": CURRENT_REPORT_GENERATION_SCHEMA_VERSION,
        "next_action": action,
    }


def prepare_project_migration(
    paths: ProjectPaths, *, apply: bool = False
) -> dict[str, Any]:
    _require_existing_project_root(paths)
    if not apply:
        recovery = _incomplete_migration_backup(paths)
        if recovery is not None:
            return {
                "schema_id": "project-migration-status/v1",
                "project_id": paths.root.name,
                "status": "recovery_required",
                "readiness_schema_version": None,
                "report_generation_schema_version": None,
                "target_readiness_schema_version": CURRENT_READINESS_SCHEMA_VERSION,
                "target_report_generation_schema_version": CURRENT_REPORT_GENERATION_SCHEMA_VERSION,
                "next_action": "Re-run with --apply to restore the interrupted transaction before migration.",
                "changed": False,
                "recovered_incomplete_transaction": False,
            }
        return {
            **inspect_project_migration(paths),
            "changed": False,
            "recovered_incomplete_transaction": False,
        }

    with (
        project_write_lock(paths.root),
        advisory_file_lock(paths.data / ".shots.lock", root=paths.root),
        advisory_file_lock(paths.data / ".client-export.lock", root=paths.root),
    ):
        recovered = _recover_incomplete_migration(paths)
        status = inspect_project_migration(paths)
        if status["status"] != "migration_required":
            return {
                **status,
                "changed": False,
                "recovered_incomplete_transaction": recovered,
            }

        manifest_before = _optional_metadata(paths.manifest, root=paths.root)
        registry_path = paths.data / "artifact_registry.json"
        registry_before = _optional_metadata(registry_path, root=paths.root)
        receipt_path = paths.data / MIGRATION_RECEIPT_NAME
        receipt_before = _optional_metadata(receipt_path, root=paths.root)
        backup = Path(
            tempfile.mkdtemp(prefix=".vew-migration-backup-", dir=paths.root)
        )
        os.chmod(backup, 0o700)
        cleanup_backup = False
        try:
            _write_backup(backup, "project_manifest.json", manifest_before)
            _write_backup(backup, "artifact_registry.json", registry_before)
            _write_backup(backup, MIGRATION_RECEIPT_NAME, receipt_before)
            atomic_write_bytes(
                backup / BACKUP_STATE_NAME,
                json.dumps(
                    {
                        "schema_id": "project-migration-backup/v1",
                        "project_id": paths.root.name,
                        "present": sorted(
                            name
                            for name, payload in (
                                ("project_manifest.json", manifest_before),
                                ("artifact_registry.json", registry_before),
                                (MIGRATION_RECEIPT_NAME, receipt_before),
                            )
                            if payload is not None
                        ),
                    },
                    sort_keys=True,
                    allow_nan=False,
                ).encode("utf-8"),
                root=backup,
            )
            _invalidate_report_for_review(
                paths.root,
                "",
                reason=MIGRATION_REASON,
            )
            migration_receipt = {
                "schema_id": "project-migration-receipt/v1",
                "project_id": paths.root.name,
                "state": "prepared",
                "from_readiness_schema_version": status[
                    "readiness_schema_version"
                ],
                "from_report_generation_schema_version": status[
                    "report_generation_schema_version"
                ],
                "target_readiness_schema_version": CURRENT_READINESS_SCHEMA_VERSION,
                "target_report_generation_schema_version": CURRENT_REPORT_GENERATION_SCHEMA_VERSION,
                "requires_finalize": True,
            }
            atomic_write_bytes(
                receipt_path,
                json.dumps(
                    migration_receipt,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                    allow_nan=False,
                ).encode("utf-8"),
                root=paths.root,
            )
            prepared = inspect_project_migration(paths)
            if prepared["status"] != "prepared":
                raise ProjectMigrationError("Migration preparation validation failed")
            registry = load_artifact_registry(paths)
            if any(
                item["scope"] in {"report", "client_export"}
                and item["state"] == "current"
                for item in registry["artifacts"]
            ):
                raise ProjectMigrationError(
                    "Migration preparation left a current report or client export"
                )
            cleanup_backup = True
        except Exception as exc:
            try:
                _restore_metadata(paths.manifest, manifest_before, root=paths.root)
                _restore_metadata(registry_path, registry_before, root=paths.root)
                _restore_metadata(receipt_path, receipt_before, root=paths.root)
                cleanup_backup = True
            except Exception as rollback_exc:
                raise ProjectMigrationError(
                    "Migration failed and metadata rollback could not be verified"
                ) from rollback_exc
            if isinstance(exc, ProjectMigrationError):
                raise
            raise ProjectMigrationError(
                "Migration failed; original metadata was restored"
            ) from exc
        finally:
            if cleanup_backup:
                _remove_private_backup(backup)
        return {
            **prepared,
            "changed": True,
            "recovered_incomplete_transaction": recovered,
        }


def _migration_receipt(paths: ProjectPaths) -> dict[str, Any] | None:
    try:
        value = load_json(paths.data / MIGRATION_RECEIPT_NAME)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    expected = {
        "schema_id",
        "project_id",
        "state",
        "from_readiness_schema_version",
        "from_report_generation_schema_version",
        "target_readiness_schema_version",
        "target_report_generation_schema_version",
        "requires_finalize",
    }
    if (
        type(value) is not dict
        or set(value) != expected
        or value.get("schema_id") != "project-migration-receipt/v1"
        or value.get("project_id") != paths.root.name
        or value.get("state") != "prepared"
        or value.get("target_readiness_schema_version")
        != CURRENT_READINESS_SCHEMA_VERSION
        or value.get("target_report_generation_schema_version")
        != CURRENT_REPORT_GENERATION_SCHEMA_VERSION
        or value.get("requires_finalize") is not True
    ):
        return None
    from_readiness = value.get("from_readiness_schema_version")
    from_report = value.get("from_report_generation_schema_version")
    if (
        type(from_readiness) is not int
        or from_readiness < 1
        or from_readiness > CURRENT_READINESS_SCHEMA_VERSION
        or (
            from_report is not None
            and (
                type(from_report) is not int
                or from_report < 1
                or from_report > CURRENT_REPORT_GENERATION_SCHEMA_VERSION
            )
        )
    ):
        return None
    return value


def _require_existing_project_root(paths: ProjectPaths) -> None:
    try:
        info = paths.root.lstat()
    except FileNotFoundError as exc:
        raise ProjectMigrationError("Project directory is missing") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ProjectMigrationError("Project directory is unsafe")


def _incomplete_migration_backup(paths: ProjectPaths) -> Path | None:
    backups = sorted(paths.root.glob(".vew-migration-backup-*"))
    if not backups:
        return None
    if len(backups) != 1:
        raise ProjectMigrationError(
            "Multiple incomplete migration backups require operator inspection"
        )
    backup = backups[0]
    try:
        info = backup.lstat()
    except OSError as exc:
        raise ProjectMigrationError("Migration backup is unreadable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ProjectMigrationError("Migration backup is unsafe")
    return backup


def _recover_incomplete_migration(paths: ProjectPaths) -> bool:
    backup = _incomplete_migration_backup(paths)
    if backup is None:
        return False
    state_path = backup / BACKUP_STATE_NAME
    if not state_path.exists():
        # Mutation starts only after the backup state commit. An earlier crash
        # leaves the project untouched, so this incomplete backup can be removed.
        _remove_private_backup(backup)
        return False
    try:
        state = json.loads(
            read_regular_bytes(
                state_path,
                root=backup,
                max_bytes=MAX_MIGRATION_METADATA_BYTES,
            ).decode("utf-8")
        )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ProjectMigrationError("Migration backup state is invalid") from exc
    allowed = {
        "project_manifest.json",
        "artifact_registry.json",
        MIGRATION_RECEIPT_NAME,
    }
    if (
        type(state) is not dict
        or set(state) != {"schema_id", "project_id", "present"}
        or state.get("schema_id") != "project-migration-backup/v1"
        or state.get("project_id") != paths.root.name
        or type(state.get("present")) is not list
        or any(type(name) is not str or name not in allowed for name in state["present"])
        or len(state["present"]) != len(set(state["present"]))
    ):
        raise ProjectMigrationError("Migration backup state fields are invalid")
    targets = {
        "project_manifest.json": paths.manifest,
        "artifact_registry.json": paths.data / "artifact_registry.json",
        MIGRATION_RECEIPT_NAME: paths.data / MIGRATION_RECEIPT_NAME,
    }
    for name, target in targets.items():
        payload = (
            read_regular_bytes(
                backup / name,
                root=backup,
                max_bytes=MAX_MIGRATION_METADATA_BYTES,
            )
            if name in state["present"]
            else None
        )
        _restore_metadata(target, payload, root=paths.root)
    _remove_private_backup(backup)
    return True


def _optional_metadata(path: Path, *, root: Path) -> bytes | None:
    try:
        return read_regular_bytes(path, root=root, max_bytes=MAX_MIGRATION_METADATA_BYTES)
    except FileNotFoundError:
        return None


def _write_backup(root: Path, name: str, payload: bytes | None) -> None:
    if payload is None:
        return
    atomic_write_bytes(root / name, payload, root=root)


def _restore_metadata(path: Path, payload: bytes | None, *, root: Path) -> None:
    if payload is not None:
        atomic_write_bytes(path, payload, root=root)
        return
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ProjectMigrationError("Rollback target is unsafe")
    path.unlink()


def _remove_private_backup(root: Path) -> None:
    if not root.exists():
        return
    for path in root.iterdir():
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ProjectMigrationError("Migration backup contains an unsafe entry")
        path.unlink()
    root.rmdir()


__all__ = [
    "CURRENT_READINESS_SCHEMA_VERSION",
    "CURRENT_REPORT_GENERATION_SCHEMA_VERSION",
    "ProjectMigrationError",
    "inspect_project_migration",
    "prepare_project_migration",
]
