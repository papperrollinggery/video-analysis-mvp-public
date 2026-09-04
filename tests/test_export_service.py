from __future__ import annotations

import copy
import importlib.util
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from tests.test_audio_review import audio_review_fixture, ready_client_export_fixture
from video_analysis_mvp.artifacts import load_artifact_registry, mark_artifacts_stale
from video_analysis_mvp.audio_review import apply_audio_review, get_audio_event
from video_analysis_mvp.client_export_dataset import (
    _canonical_digest,
    build_client_export_dataset,
)
from video_analysis_mvp.export_service import (
    MAX_IDEMPOTENCY_ENTRIES,
    ClientExportConflict,
    ClientExportError,
    PdfRuntime,
    cancel_client_export,
    delete_saved_export,
    generate_client_export,
    read_current_export,
    read_export_center,
    read_export_state,
    recover_client_exports,
    save_current_export,
)
from video_analysis_mvp.paths import ProjectPaths
from video_analysis_mvp.readiness import evaluate_project_readiness
from video_analysis_mvp.schemas import dump_json, load_json
from video_analysis_mvp.workspace_api import (
    ApiError,
    dispatch_api,
    regenerate_project_report,
)

NODE = Path(os.environ.get("VEW_PDF_NODE", "__vew_pdf_node_unavailable__"))
NODE_MODULES = Path(
    os.environ.get("VEW_PDF_NODE_MODULES", "__vew_pdf_modules_unavailable__")
)
CHROME = Path(os.environ.get("VEW_PDF_BROWSER", "__vew_pdf_browser_unavailable__"))
FONT = Path(os.environ.get("VEW_PDF_FONT", "__vew_pdf_font_unavailable__"))
REAL_EXPORT_RUNTIME = (
    NODE.is_file()
    and (NODE_MODULES / "playwright").is_dir()
    and CHROME.is_file()
    and FONT.is_file()
    and importlib.util.find_spec("pypdf") is not None
    and importlib.util.find_spec("openpyxl") is not None
)


class ExportServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="export-service-")
        self.addCleanup(self.temp.cleanup)
        self.paths = ready_client_export_fixture(Path(self.temp.name))
        self.calls = 0

    def review_voice(self, paths: ProjectPaths, text: str = "Reviewed VO") -> None:
        page = get_audio_event(paths, "voice-1")
        event = page["events"][0]
        apply_audio_review(
            paths,
            "voice-1",
            {
                "expected_generation_id": page["generation_id"],
                "expected_proposal_sha256": event["proposal_sha256"],
                "status": "reviewed",
                "overrides": {"text": text},
                "review_notes": "test-only operator assertion",
                "confirm_operator_review": True,
            },
        )

    def renderer(self, marker: bytes = b"workbook"):
        def render(dataset, output, **_kwargs):
            self.calls += 1
            output.write_bytes(marker + dataset["dataset_digest"].encode("ascii"))
            return {
                "schema_id": "fixture-render/v1",
                "dataset_digest": dataset["dataset_digest"],
                "sha256": "ignored-by-service",
            }

        return render

    def generate(self, key: str, *, renderer=None, cancelled=None, settings=None):
        return generate_client_export(
            self.paths,
            formats=["xlsx"],
            settings=settings or {"language": "bilingual"},
            idempotency_key=key,
            cancelled=cancelled,
            _renderers={"xlsx": renderer or self.renderer()},
        )

    def test_success_publishes_one_current_package_and_same_key_is_idempotent(self) -> None:
        first = self.generate("double-click-1")
        second = self.generate("double-click-1")

        self.assertEqual(first, second)
        self.assertEqual(1, self.calls)
        current = self.paths.reports / "client" / "current"
        self.assertTrue((current / "client_breakdown.xlsx").is_file())
        self.assertEqual(first, read_current_export(self.paths))
        self.assertFalse((self.paths.reports / "client" / "saved").exists())
        self.assertFalse(list((self.paths.reports / "client").glob(".client-export-stage-*")))
        registry = load_artifact_registry(self.paths)
        self.assertEqual(
            {"client_current_package", "client_breakdown_xlsx", "client_export_receipt"},
            {item["artifact_id"] for item in registry["artifacts"] if item["scope"] == "client_export"},
        )

    def test_unreviewed_audio_blocks_finalize_and_client_export(self) -> None:
        with tempfile.TemporaryDirectory(prefix="export-audio-gate-") as directory:
            paths = audio_review_fixture(
                Path(directory), professionally_ready=True
            )
            blocked = evaluate_project_readiness(
                paths.root,
                workspace_root=paths.root.parent,
                require_persisted_receipt=False,
            )
            self.assertIs(blocked["audio_timeline_available"], True)
            self.assertIs(blocked["audio_review_complete"], False)
            self.assertEqual(1, blocked["audio_requires_review_count"])
            self.assertIsNotNone(blocked["audio_intelligence_binding"])
            with self.assertRaises(ApiError) as finalize_error:
                regenerate_project_report(paths.root.parent, paths.root)
            self.assertEqual(409, finalize_error.exception.status)
            self.assertIn("audio event", str(finalize_error.exception.details))

            with self.assertRaisesRegex(ClientExportConflict, "audio event"):
                generate_client_export(
                    paths,
                    formats=["xlsx"],
                    settings={"language": "bilingual"},
                    idempotency_key="unreviewed-audio",
                    _renderers={"xlsx": self.renderer(b"must-not-render")},
                )
            self.assertIsNone(read_export_center(paths)["current"])
            self.assertFalse(list(paths.root.rglob("*.xlsx")))

    def test_audio_review_finalize_export_mutation_and_refinalize_lifecycle(self) -> None:
        first = self.generate("lifecycle-first")
        save_current_export(self.paths, "approved-v1")

        self.review_voice(self.paths, "Revised reviewed VO")
        stale = read_export_center(self.paths)
        self.assertEqual("stale", stale["current"]["lifecycle_state"])
        self.assertEqual(["approved-v1"], [item["version_id"] for item in stale["saved"]])
        with self.assertRaises(ClientExportConflict):
            self.generate("lifecycle-before-refinalize")

        regenerated = regenerate_project_report(self.paths.root.parent, self.paths.root)
        self.assertTrue(regenerated["workspace"]["project"]["readiness"]["professional_export_allowed"])
        second = self.generate("lifecycle-second")
        self.assertNotEqual(first["export_id"], second["export_id"])
        self.assertEqual(
            ["approved-v1"],
            [item["version_id"] for item in read_export_center(self.paths)["saved"]],
        )

    def test_finalize_serializes_a_concurrent_audio_review(self) -> None:
        from video_analysis_mvp.pipeline import run_report as real_run_report

        entered = threading.Event()
        release = threading.Event()
        finalized = []
        mutated = []
        failures = []

        def slow_report(*args, **kwargs):
            entered.set()
            release.wait(timeout=5)
            return real_run_report(*args, **kwargs)

        def finalize() -> None:
            try:
                finalized.append(
                    regenerate_project_report(self.paths.root.parent, self.paths.root)
                )
            except (ApiError, OSError, ValueError, RuntimeError) as exc:
                failures.append(exc)

        def mutate() -> None:
            try:
                page = get_audio_event(self.paths, "voice-1")
                event = page["events"][0]
                mutated.append(
                    apply_audio_review(
                        self.paths,
                        "voice-1",
                        {
                            "expected_generation_id": page["generation_id"],
                            "expected_proposal_sha256": event["proposal_sha256"],
                            "status": "needs_work",
                            "overrides": {"text": "Needs another listen"},
                            "review_notes": "concurrent operator correction",
                            "confirm_operator_review": True,
                        },
                    )
                )
            except (ApiError, OSError, ValueError, RuntimeError) as exc:
                failures.append(exc)

        with patch("video_analysis_mvp.pipeline.run_report", side_effect=slow_report):
            finalize_thread = threading.Thread(target=finalize)
            finalize_thread.start()
            self.assertTrue(entered.wait(timeout=5))
            mutation_thread = threading.Thread(target=mutate)
            mutation_thread.start()
            time.sleep(0.1)
            self.assertTrue(mutation_thread.is_alive())
            release.set()
            finalize_thread.join(timeout=10)
            mutation_thread.join(timeout=10)

        self.assertEqual([], failures)
        self.assertTrue(finalized[0]["workspace"]["project"]["readiness"]["professional_export_allowed"])
        self.assertTrue(mutated[0]["report_regeneration_required"])
        self.assertEqual("review_pending", load_json(self.paths.manifest)["status"])

    def test_same_idempotency_key_with_different_request_is_a_conflict(self) -> None:
        self.generate("same-key")
        with self.assertRaises(ClientExportConflict):
            self.generate("same-key", settings={"language": "en"})
        self.assertEqual(1, self.calls)

    def test_historical_idempotency_key_cannot_be_rebound_after_current_changes(self) -> None:
        self.generate("key-a", settings={"language": "zh"})
        self.generate("key-b", settings={"language": "en"})
        with self.assertRaises(ClientExportConflict):
            self.generate("key-a", settings={"language": "bilingual"})
        self.assertEqual(2, self.calls)

    def test_same_key_is_a_new_namespace_after_a_new_finalized_generation(self) -> None:
        first = self.generate("generation-scoped-key")
        dataset = copy.deepcopy(build_client_export_dataset(self.paths))
        dataset["source_bindings"]["report_generation_id"] = "new-finalized-generation"
        base = {
            key: value
            for key, value in dataset.items()
            if key not in {"dataset_id", "dataset_digest"}
        }
        digest = _canonical_digest(base)
        dataset["dataset_id"] = dataset["dataset_digest"] = digest

        with patch(
            "video_analysis_mvp.export_service.build_client_export_dataset",
            return_value=dataset,
        ):
            second = self.generate("generation-scoped-key")

        self.assertNotEqual(first["export_id"], second["export_id"])
        self.assertEqual("new-finalized-generation", second["source_generation_id"])

    def test_full_bounded_idempotency_ledger_fails_closed_without_eviction(self) -> None:
        for index in range(MAX_IDEMPOTENCY_ENTRIES):
            self.generate(f"bounded-{index}")
        with self.assertRaisesRegex(ClientExportConflict, "ledger is full"):
            self.generate("bounded-overflow")
        with self.assertRaises(ClientExportConflict):
            self.generate("bounded-0", settings={"language": "en"})

    def test_renderer_failure_and_cancellation_preserve_previous_current_bytes(self) -> None:
        self.generate("initial", renderer=self.renderer(b"initial"))
        current = self.paths.reports / "client" / "current"
        before = {path.name: path.read_bytes() for path in current.iterdir()}

        def fail(*_args, **_kwargs):
            raise RuntimeError("fixture renderer crash")

        with self.assertRaisesRegex(ClientExportError, "rendering failed"):
            self.generate("failure", renderer=fail)
        self.assertEqual(before, {path.name: path.read_bytes() for path in current.iterdir()})

        checks = iter((False, True))
        with self.assertRaisesRegex(ClientExportError, "cancelled"):
            self.generate(
                "cancelled",
                renderer=self.renderer(b"cancelled"),
                cancelled=lambda: next(checks, True),
            )
        self.assertEqual(before, {path.name: path.read_bytes() for path in current.iterdir()})

    def test_cross_request_cancel_is_cooperative_and_never_publishes(self) -> None:
        started = threading.Event()
        release = threading.Event()
        failures = []

        def slow(dataset, output, **_kwargs):
            started.set()
            release.wait(timeout=5)
            output.write_bytes(dataset["dataset_digest"].encode("ascii"))
            return {"schema_id": "fixture-render/v1", "dataset_digest": dataset["dataset_digest"]}

        def run() -> None:
            try:
                self.generate("external-cancel", renderer=slow)
            except ClientExportError as exc:
                failures.append(exc)

        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(started.wait(timeout=5))
        state = read_export_state(self.paths)
        self.assertEqual("rendering", state["status"])
        self.assertEqual(
            "cancel_requested",
            cancel_client_export(self.paths, state["request_digest"])["status"],
        )
        release.set()
        thread.join(timeout=10)

        self.assertEqual(1, len(failures))
        self.assertIn("cancelled", str(failures[0]))
        self.assertEqual("cancelled", read_export_state(self.paths)["status"])
        self.assertFalse((self.paths.reports / "client" / "current").exists())

    def test_state_route_does_not_wait_for_the_long_export_lock(self) -> None:
        started = threading.Event()
        release = threading.Event()
        failures = []

        def slow(dataset, output, **_kwargs):
            started.set()
            release.wait(timeout=5)
            output.write_bytes(dataset["dataset_digest"].encode("ascii"))
            return {"schema_id": "fixture-render/v1", "dataset_digest": dataset["dataset_digest"]}

        def run() -> None:
            try:
                self.generate("state-route", renderer=slow)
            except (ClientExportError, OSError, ValueError) as exc:  # pragma: no cover - asserted below
                failures.append(exc)

        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(started.wait(timeout=5))
        began = time.monotonic()
        try:
            status, payload = dispatch_api(
                self.paths.root.parent,
                "GET",
                f"/api/projects/{self.paths.root.name}/exports/state",
                "",
                b"",
            )
            elapsed = time.monotonic() - began
        finally:
            release.set()
            thread.join(timeout=10)

        self.assertEqual(200, status)
        self.assertEqual("rendering", payload["status"])
        self.assertLess(elapsed, 1.0)
        self.assertEqual([], failures)

    def test_cancel_after_publishing_boundary_never_claims_cancel_requested(self) -> None:
        entered_publish = threading.Event()
        release_publish = threading.Event()
        real_publish = __import__("video_analysis_mvp.export_service", fromlist=["_publish_unlocked"])._publish_unlocked
        results = []

        def blocked_publish(client, stage):
            entered_publish.set()
            release_publish.wait(timeout=5)
            return real_publish(client, stage)

        def run() -> None:
            with patch(
                "video_analysis_mvp.export_service._publish_unlocked",
                side_effect=blocked_publish,
            ):
                results.append(self.generate("publishing-boundary"))

        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(entered_publish.wait(timeout=5))
        state = read_export_state(self.paths)
        self.assertEqual("publishing", state["status"])
        cancelled = cancel_client_export(self.paths, state["request_digest"])
        self.assertEqual("publishing", cancelled["status"])
        release_publish.set()
        thread.join(timeout=10)
        self.assertEqual(1, len(results))
        self.assertEqual("current", read_export_state(self.paths)["status"])

    def test_recover_marks_abandoned_rendering_state_failed(self) -> None:
        def crash(*_args, **_kwargs):
            raise SystemExit(96)

        with self.assertRaises(SystemExit):
            self.generate("interrupted-render", renderer=crash)
        self.assertEqual("rendering", read_export_state(self.paths)["status"])

        recover_client_exports(self.paths)

        state = read_export_state(self.paths)
        self.assertEqual("failed", state["status"])
        self.assertEqual("interrupted_before_completion", state["reason"])
        self.assertFalse(list((self.paths.reports / "client").glob(".cancel-*")))

    def test_concurrent_same_key_renders_once(self) -> None:
        gate = threading.Lock()

        def slow(dataset, output, **_kwargs):
            with gate:
                self.calls += 1
            time.sleep(0.1)
            output.write_bytes(dataset["dataset_digest"].encode("ascii"))
            return {"schema_id": "fixture-render/v1", "dataset_digest": dataset["dataset_digest"]}

        results = []
        failures = []

        def run() -> None:
            try:
                results.append(self.generate("concurrent", renderer=slow))
            except ClientExportError as exc:  # pragma: no cover - asserted below
                failures.append(exc)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual([], failures)
        self.assertEqual(1, self.calls)
        self.assertEqual(results[0]["export_id"], results[1]["export_id"])

    def test_recovery_restores_previous_and_removes_abandoned_staging(self) -> None:
        client = self.paths.reports / "client"
        previous = client / ".client-export-previous"
        previous.mkdir(parents=True)
        (previous / "export_receipt.json").write_text('{"previous":true}', encoding="utf-8")
        stage = client / ".client-export-stage-abandoned"
        stage.mkdir()
        (stage / "partial.xlsx").write_bytes(b"partial")

        result = recover_client_exports(self.paths)

        self.assertEqual("restored_previous", result["status"])
        self.assertTrue((client / "current" / "export_receipt.json").is_file())
        self.assertFalse(previous.exists())
        self.assertFalse(stage.exists())

    def test_crash_after_directory_publish_rolls_back_to_registry_generation(self) -> None:
        first = self.generate("crash-old", renderer=self.renderer(b"old"))
        with (
            patch(
                "video_analysis_mvp.export_service.replace_client_current_artifacts",
                side_effect=SystemExit(91),
            ),
            self.assertRaises(SystemExit),
        ):
            self.generate("crash-new", renderer=self.renderer(b"new"))

        result = recover_client_exports(self.paths)

        self.assertEqual("restored_previous", result["status"])
        self.assertEqual(first["export_id"], read_current_export(self.paths)["export_id"])
        registry = load_artifact_registry(self.paths)
        package = next(item for item in registry["artifacts"] if item["artifact_id"] == "client_current_package")
        self.assertEqual(first["export_id"], package["generation_id"])

    def test_crash_after_journal_before_publish_preserves_old_current(self) -> None:
        first = self.generate("prepublish-old", renderer=self.renderer(b"old"))
        with (
            patch(
                "video_analysis_mvp.export_service._publish_unlocked",
                side_effect=SystemExit(94),
            ),
            self.assertRaises(SystemExit),
        ):
            self.generate("prepublish-new", renderer=self.renderer(b"new"))

        result = recover_client_exports(self.paths)

        self.assertEqual("aborted_before_publication", result["status"])
        self.assertEqual(first["export_id"], read_current_export(self.paths)["export_id"])
        self.assertFalse(list((self.paths.reports / "client").glob(".client-export-stage-*")))
        self.assertFalse((self.paths.reports / "client" / "export_journal.json").exists())

    def test_missing_previous_during_crash_recovery_clears_orphan_registry(self) -> None:
        self.generate("orphan-old", renderer=self.renderer(b"old"))
        with (
            patch(
                "video_analysis_mvp.export_service.replace_client_current_artifacts",
                side_effect=SystemExit(91),
            ),
            self.assertRaises(SystemExit),
        ):
            self.generate("orphan-new", renderer=self.renderer(b"new"))
        previous = self.paths.reports / "client" / ".client-export-previous"
        __import__("shutil").rmtree(previous)

        recover_client_exports(self.paths)

        self.assertFalse((self.paths.reports / "client" / "current").exists())
        self.assertFalse((self.paths.reports / "client" / "export_journal.json").exists())
        self.assertFalse(
            any(
                item["scope"] == "client_export" and item["retention"] == "current"
                for item in load_artifact_registry(self.paths)["artifacts"]
            )
        )

    def test_save_and_delete_version_are_explicit_and_immutable(self) -> None:
        current = self.generate("save-source")
        saved = save_current_export(self.paths, "client-v1")
        saved_root = self.paths.reports / "client" / "saved" / "client-v1"
        self.assertEqual(current["export_id"], saved["export_id"])
        self.assertTrue((saved_root / "client_breakdown.xlsx").is_file())
        self.assertTrue(any(item["state"] == "saved" for item in load_artifact_registry(self.paths)["artifacts"]))
        center = read_export_center(self.paths)
        self.assertTrue(center["current"]["downloads"]["xlsx"].startswith(f"/files/{self.paths.root.name}/"))
        self.assertTrue(center["saved"][0]["downloads"]["xlsx"].startswith(f"/files/{self.paths.root.name}/"))
        self.assertNotIn(str(self.paths.root), json.dumps(center))

        again = save_current_export(self.paths, "client-v1")
        self.assertEqual(saved, again)
        with self.assertRaises(ValueError):
            save_current_export(self.paths, "../escape")

        deleted = delete_saved_export(self.paths, "client-v1")
        self.assertEqual("deleted", deleted["status"])
        self.assertFalse(saved_root.exists())
        self.assertFalse(any(item["state"] == "saved" for item in load_artifact_registry(self.paths)["artifacts"]))
        self.assertEqual("absent", delete_saved_export(self.paths, "client-v1")["status"])

    def test_receipt_contains_no_private_absolute_path(self) -> None:
        receipt = self.generate("privacy")
        self.assertNotIn(str(self.paths.root), json.dumps(receipt))

    def test_stale_registry_blocks_current_read_and_save_without_deleting_files(self) -> None:
        self.generate("stale-current")
        current = self.paths.reports / "client" / "current"
        before = {path.name: path.read_bytes() for path in current.iterdir()}
        mark_artifacts_stale(
            self.paths,
            scopes={"client_export"},
            reason="source_changed",
        )

        with self.assertRaisesRegex(ClientExportConflict, "stale"):
            read_current_export(self.paths)
        with self.assertRaisesRegex(ClientExportConflict, "stale"):
            save_current_export(self.paths, "stale-copy")
        self.assertEqual(before, {path.name: path.read_bytes() for path in current.iterdir()})

    def test_current_read_rejects_registry_digest_or_coverage_tampering(self) -> None:
        self.generate("registry-tamper")
        registry = load_artifact_registry(self.paths)
        receipt_record = next(
            item for item in registry["artifacts"] if item["artifact_id"] == "client_export_receipt"
        )
        receipt_record["digest"] = {
            "algorithm": "sha256",
            "sha256": "0" * 64,
            "size_bytes": 0,
        }
        dump_json(self.paths.data / "artifact_registry.json", registry)
        with self.assertRaisesRegex(ClientExportConflict, "not fully committed"):
            read_current_export(self.paths)

    def test_logo_is_frozen_once_for_every_selected_renderer(self) -> None:
        logo = self.paths.assets / "logo.png"
        Image.new("RGB", (2, 2), "red").save(logo)
        red = logo.read_bytes()
        seen = []

        def render(dataset, output, **kwargs):
            frozen = self.paths.root / kwargs["settings"]["logo_path"]
            seen.append(frozen.read_bytes())
            if len(seen) == 1:
                Image.new("RGB", (2, 2), "blue").save(logo)
            output.write_bytes(dataset["dataset_digest"].encode("ascii"))
            return {"schema_id": "fixture-render/v1", "dataset_digest": dataset["dataset_digest"]}

        receipt = generate_client_export(
            self.paths,
            formats=["pdf", "xlsx"],
            settings={"language": "bilingual", "logo_path": "assets/logo.png"},
            idempotency_key="frozen-logo",
            pdf_runtime=PdfRuntime(Path("node"), Path("modules"), Path("browser"), Path("font"), ("Noto Sans CJK SC",)),
            _renderers={"pdf": render, "xlsx": render},
        )

        self.assertEqual([red, red], seen)
        self.assertEqual(receipt["settings"]["logo"]["sha256"], __import__("hashlib").sha256(red).hexdigest())

    def test_saved_delete_registry_failure_restores_directory(self) -> None:
        self.generate("delete-rollback")
        save_current_export(self.paths, "client-v1")
        target = self.paths.reports / "client" / "saved" / "client-v1"
        with (
            patch(
                "video_analysis_mvp.export_service.remove_saved_artifact",
                side_effect=OSError("registry unavailable"),
            ),
            self.assertRaises(OSError),
        ):
            delete_saved_export(self.paths, "client-v1")
        self.assertTrue(target.is_dir())
        self.assertTrue(any(item["state"] == "saved" for item in load_artifact_registry(self.paths)["artifacts"]))

    def test_saved_save_and_delete_crashes_are_recovered(self) -> None:
        self.generate("saved-crash")
        with (
            patch(
                "video_analysis_mvp.export_service.register_artifact",
                side_effect=SystemExit(92),
            ),
            self.assertRaises(SystemExit),
        ):
            save_current_export(self.paths, "save-crash")
        self.assertFalse(any(item["state"] == "saved" for item in load_artifact_registry(self.paths)["artifacts"]))

        recover_client_exports(self.paths)

        self.assertTrue((self.paths.reports / "client" / "saved" / "save-crash").is_dir())
        self.assertTrue(any(item["state"] == "saved" for item in load_artifact_registry(self.paths)["artifacts"]))

        with (
            patch(
                "video_analysis_mvp.export_service.remove_saved_artifact",
                side_effect=SystemExit(93),
            ),
            self.assertRaises(SystemExit),
        ):
            delete_saved_export(self.paths, "save-crash")
        recover_client_exports(self.paths)
        self.assertTrue((self.paths.reports / "client" / "saved" / "save-crash").is_dir())
        self.assertFalse(list((self.paths.reports / "client" / "saved").glob(".client-export-delete-*")))

    def test_saved_crash_before_rename_cleans_abandoned_copy(self) -> None:
        self.generate("saved-pre-rename")
        real_rename = __import__("video_analysis_mvp.export_service", fromlist=["rename_directory_entry"]).rename_directory_entry

        def crash_on_save(parent, source_name, target_name, **kwargs):
            if source_name.startswith(".client-export-save-"):
                raise SystemExit(95)
            return real_rename(parent, source_name, target_name, **kwargs)

        with (
            patch(
                "video_analysis_mvp.export_service.rename_directory_entry",
                side_effect=crash_on_save,
            ),
            self.assertRaises(SystemExit),
        ):
            save_current_export(self.paths, "pre-rename")

        recover_client_exports(self.paths)

        saved = self.paths.reports / "client" / "saved"
        self.assertFalse(list(saved.glob(".client-export-save-*")))
        self.assertFalse((self.paths.reports / "client" / "saved_journal.json").exists())

    def test_saved_parent_symlink_swap_never_deletes_external_directory(self) -> None:
        self.generate("delete-symlink")
        save_current_export(self.paths, "client-v1")
        saved = self.paths.reports / "client" / "saved"
        external = Path(self.temp.name) / "external-saved"
        external_target = external / "client-v1"
        external_target.mkdir(parents=True)
        sentinel = external_target / "sentinel.txt"
        sentinel.write_text("keep", encoding="utf-8")
        real_read = __import__("video_analysis_mvp.export_service", fromlist=["_read_package"])._read_package

        def read_then_swap(path):
            receipt = real_read(path)
            saved.rename(saved.with_name("saved-original"))
            saved.symlink_to(external, target_is_directory=True)
            return receipt

        with (
            patch("video_analysis_mvp.export_service._read_package", side_effect=read_then_swap),
            self.assertRaises((ClientExportError, ValueError)),
        ):
            delete_saved_export(self.paths, "client-v1")
        self.assertEqual("keep", sentinel.read_text(encoding="utf-8"))

    @unittest.skipUnless(REAL_EXPORT_RUNTIME, "real XLSX/PDF export runtime is unavailable")
    def test_real_both_format_transaction_publishes_one_verified_package(self) -> None:
        receipt = generate_client_export(
            self.paths,
            formats=["xlsx", "pdf"],
            settings={"language": "bilingual"},
            idempotency_key="real-both",
            pdf_runtime=PdfRuntime(
                node_executable=NODE,
                node_modules_path=NODE_MODULES,
                browser_executable=CHROME,
                font_path=FONT,
                available_fonts=("Noto Sans CJK SC",),
            ),
        )

        self.assertEqual(["pdf", "xlsx"], receipt["formats"])
        current = self.paths.reports / "client" / "current"
        self.assertTrue((current / "client_breakdown.xlsx").is_file())
        self.assertTrue((current / "client_breakdown.pdf").is_file())
        registry = load_artifact_registry(self.paths)
        self.assertEqual(
            {
                "client_current_package",
                "client_breakdown_xlsx",
                "client_breakdown_pdf",
                "client_export_receipt",
            },
            {item["artifact_id"] for item in registry["artifacts"] if item["scope"] == "client_export"},
        )


if __name__ == "__main__":
    unittest.main()
