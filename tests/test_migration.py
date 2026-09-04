from __future__ import annotations

import contextlib
import copy
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_audio_review import ready_client_export_fixture
from video_analysis_mvp import migration as migration_module
from video_analysis_mvp.cli import main
from video_analysis_mvp.export_service import (
    ClientExportConflict,
    generate_client_export,
    read_export_center,
    save_current_export,
)
from video_analysis_mvp.migration import (
    ProjectMigrationError,
    inspect_project_migration,
    prepare_project_migration,
)
from video_analysis_mvp.paths import ProjectPaths
from video_analysis_mvp.schemas import dump_json, load_json
from video_analysis_mvp.workspace_api import regenerate_project_report


class ProjectMigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="vew-migration-")
        self.addCleanup(self.temp.cleanup)
        self.paths = ready_client_export_fixture(Path(self.temp.name))

    def make_legacy(self) -> None:
        readiness = load_json(self.paths.data / "readiness.json")
        readiness["schema_version"] = 2
        dump_json(self.paths.data / "readiness.json", readiness)
        manifest = load_json(self.paths.manifest)
        manifest["report_generation"]["schema_version"] = 3
        manifest["report_generation"]["source_receipts"].pop(
            "audio_intelligence", None
        )
        dump_json(self.paths.manifest, manifest)

    def snapshot(self) -> dict[str, bytes | None]:
        result = {}
        for path in (
            self.paths.manifest,
            self.paths.data / "artifact_registry.json",
            self.paths.data / "migration_receipt.json",
        ):
            result[path.name] = path.read_bytes() if path.exists() else None
        return result

    def generate_and_save_client_version(self) -> None:
        def render(dataset, output, **_kwargs):
            output.write_bytes(b"migration-fixture:" + dataset["dataset_digest"].encode())
            return {
                "schema_id": "fixture-render/v1",
                "dataset_digest": dataset["dataset_digest"],
            }

        generate_client_export(
            self.paths,
            formats=["xlsx"],
            settings={"language": "bilingual"},
            idempotency_key="migration-current-v1",
            _renderers={"xlsx": render},
        )
        save_current_export(self.paths, "approved-v1")

    def test_current_project_is_idempotent_noop(self) -> None:
        before = self.snapshot()
        first = prepare_project_migration(self.paths, apply=False)
        second = prepare_project_migration(self.paths, apply=True)
        self.assertEqual("current", first["status"])
        self.assertFalse(first["changed"])
        self.assertEqual(first, second)
        self.assertEqual(before, self.snapshot())

    def test_current_readiness_without_a_publication_requires_finalize(self) -> None:
        manifest = load_json(self.paths.manifest)
        manifest.pop("report_generation")
        manifest["status"] = "review_pending"
        dump_json(self.paths.manifest, manifest)

        inspected = prepare_project_migration(self.paths, apply=False)
        self.assertEqual("finalize_required", inspected["status"])
        self.assertFalse(inspected["changed"])

    def test_dry_run_apply_and_refinalize_are_explicit_and_idempotent(self) -> None:
        self.generate_and_save_client_version()
        self.make_legacy()
        before = self.snapshot()
        planned = prepare_project_migration(self.paths, apply=False)
        self.assertEqual("migration_required", planned["status"])
        self.assertFalse(planned["changed"])
        self.assertEqual(before, self.snapshot())

        prepared = prepare_project_migration(self.paths, apply=True)
        self.assertEqual("prepared", prepared["status"])
        self.assertTrue(prepared["changed"])
        self.assertFalse(prepared["recovered_incomplete_transaction"])
        again = prepare_project_migration(self.paths, apply=True)
        self.assertEqual("prepared", again["status"])
        self.assertFalse(again["changed"])
        self.assertFalse(list(self.paths.root.glob(".vew-migration-backup-*")))
        receipt = load_json(self.paths.data / "migration_receipt.json")
        self.assertEqual("project-migration-receipt/v1", receipt["schema_id"])
        self.assertTrue(receipt["requires_finalize"])
        center = read_export_center(self.paths)
        self.assertEqual("stale", center["current"]["lifecycle_state"])
        self.assertEqual(
            ["approved-v1"],
            [item["version_id"] for item in center["saved"]],
        )

        finalized = regenerate_project_report(
            self.paths.root.parent,
            self.paths.root,
        )
        self.assertTrue(
            finalized["workspace"]["project"]["readiness"][
                "professional_export_allowed"
            ]
        )
        self.assertEqual("current", inspect_project_migration(self.paths)["status"])

    def test_failure_restores_manifest_and_registry_bytes(self) -> None:
        self.generate_and_save_client_version()
        self.make_legacy()
        before = self.snapshot()
        with (
            patch(
                "video_analysis_mvp.workspace_api.mark_artifacts_stale",
                side_effect=OSError("injected registry failure"),
            ),
            self.assertRaisesRegex(ProjectMigrationError, "restored"),
        ):
            prepare_project_migration(self.paths, apply=True)

        self.assertEqual(before, self.snapshot())
        self.assertFalse(list(self.paths.root.glob(".vew-migration-backup-*")))

    def test_process_interruption_is_recovered_before_retry(self) -> None:
        self.generate_and_save_client_version()
        self.make_legacy()
        before = self.snapshot()
        with (
            patch(
                "video_analysis_mvp.workspace_api.mark_artifacts_stale",
                side_effect=SystemExit(96),
            ),
            self.assertRaises(SystemExit),
        ):
            prepare_project_migration(self.paths, apply=True)
        self.assertTrue(list(self.paths.root.glob(".vew-migration-backup-*")))
        self.assertNotEqual(before, self.snapshot())

        inspected = prepare_project_migration(self.paths, apply=False)
        self.assertEqual("recovery_required", inspected["status"])
        self.assertFalse(inspected["changed"])
        self.assertFalse(inspected["recovered_incomplete_transaction"])
        self.assertNotEqual(before, self.snapshot())
        self.assertTrue(list(self.paths.root.glob(".vew-migration-backup-*")))

        recovered = prepare_project_migration(self.paths, apply=True)
        self.assertTrue(recovered["recovered_incomplete_transaction"])
        self.assertEqual("prepared", recovered["status"])
        self.assertTrue(recovered["changed"])
        self.assertFalse(list(self.paths.root.glob(".vew-migration-backup-*")))
        self.assertEqual(
            ["approved-v1"],
            [item["version_id"] for item in read_export_center(self.paths)["saved"]],
        )

    def test_installed_cli_surface_is_dry_by_default_and_applies_explicitly(self) -> None:
        self.make_legacy()
        before = self.snapshot()
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            status = main(
                [
                    "--workspace",
                    str(self.paths.root.parent),
                    "migrate",
                    self.paths.root.name,
                ]
            )
        self.assertEqual(0, status)
        self.assertEqual("migration_required", json.loads(stdout.getvalue())["status"])
        self.assertEqual(before, self.snapshot())

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            status = main(
                [
                    "--workspace",
                    str(self.paths.root.parent),
                    "migrate",
                    self.paths.root.name,
                    "--apply",
                ]
            )
        self.assertEqual(0, status)
        self.assertEqual("prepared", json.loads(stdout.getvalue())["status"])

    def test_dry_run_does_not_create_a_missing_project(self) -> None:
        missing = self.paths.root.parent / "missing-project"
        with self.assertRaisesRegex(ProjectMigrationError, "directory is missing"):
            prepare_project_migration(ProjectPaths(missing), apply=False)
        with self.assertRaisesRegex(ProjectMigrationError, "directory is missing"):
            prepare_project_migration(ProjectPaths(missing), apply=True)
        self.assertFalse(missing.exists())

    def test_unsupported_schema_fails_before_writes(self) -> None:
        manifest_before = self.paths.manifest.read_bytes()
        readiness = copy.deepcopy(load_json(self.paths.data / "readiness.json"))
        readiness["schema_version"] = 999
        dump_json(self.paths.data / "readiness.json", readiness)
        with self.assertRaisesRegex(ProjectMigrationError, "unsupported"):
            prepare_project_migration(self.paths, apply=True)
        self.assertEqual(manifest_before, self.paths.manifest.read_bytes())

    def test_mixed_legacy_and_future_schema_versions_fail_before_writes(self) -> None:
        combinations = ((2, 999), (999, 3))
        for readiness_version, report_version in combinations:
            with self.subTest(
                readiness_version=readiness_version,
                report_version=report_version,
            ):
                readiness = load_json(self.paths.data / "readiness.json")
                readiness["schema_version"] = readiness_version
                dump_json(self.paths.data / "readiness.json", readiness)
                manifest = load_json(self.paths.manifest)
                manifest["report_generation"]["schema_version"] = report_version
                dump_json(self.paths.manifest, manifest)
                before = self.snapshot()

                with self.assertRaisesRegex(ProjectMigrationError, "unsupported"):
                    prepare_project_migration(self.paths, apply=True)
                self.assertEqual(before, self.snapshot())

                readiness["schema_version"] = 3
                dump_json(self.paths.data / "readiness.json", readiness)
                manifest["report_generation"]["schema_version"] = 4
                dump_json(self.paths.manifest, manifest)

    def test_present_malformed_report_generation_fails_before_writes(self) -> None:
        readiness = load_json(self.paths.data / "readiness.json")
        readiness["schema_version"] = 2
        dump_json(self.paths.data / "readiness.json", readiness)
        original = load_json(self.paths.manifest)
        for malformed in ("invalid", {}, None, {"schema_version": None}):
            with self.subTest(malformed=malformed):
                manifest = copy.deepcopy(original)
                manifest["report_generation"] = malformed
                dump_json(self.paths.manifest, manifest)
                before = self.snapshot()
                with self.assertRaises(ProjectMigrationError):
                    prepare_project_migration(self.paths, apply=True)
                self.assertEqual(before, self.snapshot())

    def test_migration_serializes_save_version_before_registry_rollback(self) -> None:
        self.generate_and_save_client_version()
        self.make_legacy()
        entered = threading.Event()
        release = threading.Event()
        migration_failures: list[Exception] = []
        save_failures: list[Exception] = []
        real_invalidate = migration_module._invalidate_report_for_review

        def slow_invalidate(*args, **kwargs):
            entered.set()
            release.wait(timeout=5)
            return real_invalidate(*args, **kwargs)

        def migrate() -> None:
            try:
                prepare_project_migration(self.paths, apply=True)
            except (OSError, RuntimeError, ValueError) as exc:  # pragma: no cover
                migration_failures.append(exc)

        def save() -> None:
            try:
                save_current_export(self.paths, "race-save")
            except (OSError, RuntimeError, ValueError) as exc:  # pragma: no cover
                save_failures.append(exc)

        with patch(
            "video_analysis_mvp.migration._invalidate_report_for_review",
            side_effect=slow_invalidate,
        ):
            migration_thread = threading.Thread(target=migrate)
            migration_thread.start()
            self.assertTrue(entered.wait(timeout=5))
            save_thread = threading.Thread(target=save)
            save_thread.start()
            time.sleep(0.1)
            self.assertTrue(save_thread.is_alive())
            release.set()
            migration_thread.join(timeout=10)
            save_thread.join(timeout=10)

        self.assertEqual([], migration_failures)
        self.assertEqual(1, len(save_failures))
        self.assertIsInstance(save_failures[0], ClientExportConflict)
        self.assertFalse(
            (self.paths.reports / "client" / "saved" / "race-save").exists()
        )
        self.assertEqual(
            ["approved-v1"],
            [item["version_id"] for item in read_export_center(self.paths)["saved"]],
        )


if __name__ == "__main__":
    unittest.main()
