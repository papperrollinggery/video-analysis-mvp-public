from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from video_analysis_mvp.run_lifecycle import (
    MIN_WORKSPACE_FREE_BYTES,
    RunAdmissionError,
    _assert_media_matches_request,
    _claimed_project_paths,
    cancel_analysis_run,
    list_analysis_runs,
    read_analysis_run,
    retry_analysis_run,
    start_analysis_run,
)
from video_analysis_mvp.schemas import (
    AnalysisProfile,
    CanonicalMediaPackage,
    SourceType,
)
from video_analysis_mvp.utils import ProcessCancelledError
from video_analysis_mvp.workspace_api import ApiError, dispatch_api


class RunLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="run-lifecycle-")
        self.workspace = Path(self.tempdir.name) / "workspace"
        self.source = Path(self.tempdir.name) / "source.mp4"
        self.source.write_bytes(b"synthetic source")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _media(self, project_id: str) -> CanonicalMediaPackage:
        project = self.workspace / project_id
        return CanonicalMediaPackage(
            project_id=project_id,
            source_type=SourceType.file,
            source=str(self.source),
            local_master_path=str(project / "ingest" / "master.mp4"),
            review_copy_path=str(project / "assets" / "review.mp4"),
            audio_path=str(project / "assets" / "audio.wav"),
            duration_seconds=1.0,
            frame_rate=24.0,
            resolution="320x180",
            aspect_ratio=1.778,
            status="created",
            analysis_profile=AnalysisProfile.research,
        )

    @staticmethod
    def _wait_for_terminal(workspace: Path, run_id: str) -> dict[str, object]:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            record = read_analysis_run(workspace, run_id)
            if record["state"] in {"completed", "failed", "interrupted", "cancelled"}:
                return record
            time.sleep(0.01)
        raise AssertionError("analysis run did not reach a terminal state")

    def test_successful_run_persists_stage_history_and_terminal_receipt(self) -> None:
        project_id = "lifecycle-success"
        media = self._media(project_id)
        with (
            patch("video_analysis_mvp.run_lifecycle.verify_media_generation", return_value=(False, ["missing"])),
            patch("video_analysis_mvp.run_lifecycle.ingest_source", return_value=media),
            patch("video_analysis_mvp.run_lifecycle.set_delivery_language"),
            patch("video_analysis_mvp.run_lifecycle.verify_visual_generation", side_effect=[(False, []), (True, [])]),
            patch("video_analysis_mvp.run_lifecycle.analyze_visual"),
            patch("video_analysis_mvp.run_lifecycle.verify_audio_analysis", side_effect=[(False, []), (True, [])]),
            patch("video_analysis_mvp.run_lifecycle.analyze_audio"),
            patch("video_analysis_mvp.run_lifecycle.verify_report_generation_manifest", side_effect=[(False, []), (True, [])]),
            patch(
                "video_analysis_mvp.run_lifecycle.synthesize",
                return_value=SimpleNamespace(artifacts={"project_manifest": "manifest.json"}),
            ),
            patch(
                "video_analysis_mvp.run_lifecycle._final_generation_bindings",
                return_value={"report_generation_id": "synthetic-generation"},
            ),
            patch("video_analysis_mvp.workspace_api.ensure_project_data"),
        ):
            started = start_analysis_run(
                self.workspace,
                {
                    "source": str(self.source),
                    "project_id": project_id,
                    "profile": "research",
                    "skip_asr": True,
                    "delivery_language": "en",
                    "max_duration_seconds": 60,
                },
            )
            record = self._wait_for_terminal(self.workspace, str(started["run_id"]))

        self.assertEqual("completed", record["state"])
        self.assertEqual(100, record["progress"])
        self.assertEqual(1, record["attempt"])
        self.assertEqual(["ingest", "visual", "audio", "report", "finalize"], [item["id"] for item in record["stages"]])
        self.assertTrue(all(item["state"] == "completed" for item in record["stages"]))
        self.assertNotIn("password", record["request"])
        self.assertEqual("synthetic-generation", record["result"]["generation_bindings"]["report_generation_id"])
        self.assertEqual([record], list_analysis_runs(self.workspace, project_id=project_id))

    def test_new_run_rejects_an_existing_project_before_persisting(self) -> None:
        project_id = "existing-project"
        (self.workspace / project_id).mkdir(parents=True)
        with self.assertRaisesRegex(FileExistsError, "Project already exists"):
            start_analysis_run(
                self.workspace,
                {"source": str(self.source), "project_id": project_id, "profile": "research"},
            )
        self.assertEqual([], list_analysis_runs(self.workspace))

    def test_workspace_admission_allows_only_one_active_analysis_run(self) -> None:
        with patch("video_analysis_mvp.run_lifecycle._launch"):
            first = start_analysis_run(
                self.workspace,
                {"source": str(self.source), "project_id": "admission-first", "profile": "research"},
            )
            with self.assertRaises(RunAdmissionError):
                start_analysis_run(
                    self.workspace,
                    {"source": str(self.source), "project_id": "admission-second", "profile": "research"},
                )

        self.assertEqual("queued", first["state"])
        self.assertEqual(1, len(list_analysis_runs(self.workspace)))
        self.assertFalse((self.workspace / "admission-second").exists())

    def test_insufficient_workspace_disk_budget_fails_before_run_record(self) -> None:
        with (
            patch(
                "video_analysis_mvp.run_lifecycle.shutil.disk_usage",
                return_value=SimpleNamespace(free=MIN_WORKSPACE_FREE_BYTES - 1),
            ),
            patch("video_analysis_mvp.run_lifecycle._launch") as launch,
            self.assertRaises(RunAdmissionError),
        ):
            start_analysis_run(
                self.workspace,
                {"source": str(self.source), "project_id": "disk-budget", "profile": "research"},
            )

        launch.assert_not_called()
        self.assertEqual([], list_analysis_runs(self.workspace))

    def test_retry_rechecks_workspace_disk_budget_before_requeue(self) -> None:
        with (
            patch("video_analysis_mvp.run_lifecycle._launch"),
            patch("video_analysis_mvp.run_lifecycle._run_is_active", return_value=True),
        ):
            queued = start_analysis_run(
                self.workspace,
                {"source": str(self.source), "project_id": "retry-disk", "profile": "research"},
            )
            cancelled = cancel_analysis_run(self.workspace, str(queued["run_id"]))
        self.assertEqual("cancelled", cancelled["state"])

        with (
            patch(
                "video_analysis_mvp.run_lifecycle.shutil.disk_usage",
                return_value=SimpleNamespace(free=MIN_WORKSPACE_FREE_BYTES - 1),
            ),
            patch("video_analysis_mvp.run_lifecycle._launch") as launch,
            self.assertRaises(RunAdmissionError),
        ):
            retry_analysis_run(self.workspace, str(queued["run_id"]))

        launch.assert_not_called()
        self.assertEqual(
            "cancelled",
            read_analysis_run(self.workspace, str(queued["run_id"]))["state"],
        )

    def test_project_claim_allows_only_the_owning_run(self) -> None:
        first = _claimed_project_paths(self.workspace, "claimed-project", "00000000-0000-4000-8000-000000000001")
        same = _claimed_project_paths(self.workspace, "claimed-project", "00000000-0000-4000-8000-000000000001")
        self.assertEqual(first.root, same.root)
        with self.assertRaisesRegex(FileExistsError, "another analysis run"):
            _claimed_project_paths(self.workspace, "claimed-project", "00000000-0000-4000-8000-000000000002")

    def test_owner_receipt_failure_recovers_only_the_exact_blank_managed_root(self) -> None:
        project_id = "owner-recovery"
        run_id = "00000000-0000-4000-8000-000000000003"
        real_atomic_write = __import__(
            "video_analysis_mvp.run_lifecycle", fromlist=["atomic_write_text"]
        ).atomic_write_text
        writes = 0

        def fail_owner_write(*args: object, **kwargs: object) -> None:
            nonlocal writes
            writes += 1
            if writes == 2:
                raise OSError("injected owner commit failure")
            real_atomic_write(*args, **kwargs)

        with patch("video_analysis_mvp.run_lifecycle.atomic_write_text", side_effect=fail_owner_write):
            with self.assertRaisesRegex(OSError, "injected owner commit failure"):
                _claimed_project_paths(self.workspace, project_id, run_id)
        recovered = _claimed_project_paths(self.workspace, project_id, run_id)
        self.assertTrue((recovered.root / ".vew-run-owner.json").is_file())

        unsafe_project = "owner-recovery-unsafe"
        unsafe_run = "00000000-0000-4000-8000-000000000004"
        with patch("video_analysis_mvp.run_lifecycle.atomic_write_text", side_effect=fail_owner_write):
            writes = 0
            with self.assertRaises(OSError):
                _claimed_project_paths(self.workspace, unsafe_project, unsafe_run)
        (self.workspace / unsafe_project / "unexpected.txt").write_text("do not adopt", encoding="utf-8")
        with self.assertRaisesRegex(FileExistsError, "not owned"):
            _claimed_project_paths(self.workspace, unsafe_project, unsafe_run)

    def test_reused_media_must_match_source_and_profile(self) -> None:
        media = self._media("binding-project")
        media.source = str(self.source.resolve())
        _assert_media_matches_request(media, {"source": str(self.source), "profile": "research"})
        with self.assertRaisesRegex(ValueError, "source"):
            _assert_media_matches_request(media, {"source": str(self.source.with_name("other.mp4")), "profile": "research"})
        with self.assertRaisesRegex(ValueError, "profile"):
            _assert_media_matches_request(media, {"source": str(self.source), "profile": "ads"})

    def test_failed_run_is_retryable_and_preserves_attempt_history(self) -> None:
        project_id = "lifecycle-retry"
        media = self._media(project_id)
        with (
            patch("video_analysis_mvp.run_lifecycle.verify_media_generation", return_value=(False, ["missing"])),
            patch("video_analysis_mvp.run_lifecycle.ingest_source", return_value=media),
            patch("video_analysis_mvp.run_lifecycle.set_delivery_language"),
            patch("video_analysis_mvp.run_lifecycle.verify_visual_generation", return_value=(False, ["missing"])),
            patch("video_analysis_mvp.run_lifecycle.analyze_visual", side_effect=RuntimeError("synthetic visual failure")),
        ):
            started = start_analysis_run(
                self.workspace,
                {"source": str(self.source), "project_id": project_id, "profile": "research"},
            )
            failed = self._wait_for_terminal(self.workspace, str(started["run_id"]))

        self.assertEqual("failed", failed["state"])
        self.assertEqual("visual", failed["stage"])
        self.assertEqual("synthetic visual failure", failed["error"]["message"])
        self.assertTrue(failed["error"]["retriable"])
        self.assertEqual("failed", failed["stages"][-1]["state"])

        with patch("video_analysis_mvp.run_lifecycle._launch"):
            queued = retry_analysis_run(self.workspace, str(failed["run_id"]))
        self.assertEqual("queued", queued["state"])
        self.assertEqual(1, queued["attempt"])
        self.assertEqual(2, len(queued["stages"]))
        self.assertIs(queued["launching"], True)

    def test_launch_window_is_not_misclassified_as_interrupted(self) -> None:
        with patch("video_analysis_mvp.run_lifecycle._launch"):
            queued = start_analysis_run(
                self.workspace,
                {"source": str(self.source), "project_id": "launch-window", "profile": "research"},
            )
        reread = read_analysis_run(self.workspace, str(queued["run_id"]))
        self.assertEqual("queued", reread["state"])
        self.assertIs(reread["launching"], True)

    def test_orphaned_active_record_is_marked_interrupted(self) -> None:
        with patch("video_analysis_mvp.run_lifecycle._launch"), patch(
            "video_analysis_mvp.run_lifecycle._run_is_active", return_value=True
        ):
            queued = start_analysis_run(
                self.workspace,
                {"source": str(self.source), "project_id": "orphaned-run", "profile": "research"},
            )
        with patch("video_analysis_mvp.run_lifecycle._run_is_active", return_value=False):
            interrupted = read_analysis_run(self.workspace, str(queued["run_id"]))
        self.assertEqual("interrupted", interrupted["state"])
        self.assertTrue(interrupted["error"]["retriable"])

    def test_invalid_run_identifier_cannot_escape_state_directory(self) -> None:
        with self.assertRaisesRegex(ValueError, "Invalid analysis run id"):
            read_analysis_run(self.workspace, "../outside")

    def test_queued_run_can_be_cancelled_and_then_retried(self) -> None:
        with patch("video_analysis_mvp.run_lifecycle._launch"), patch(
            "video_analysis_mvp.run_lifecycle._run_is_active", return_value=True
        ):
            queued = start_analysis_run(
                self.workspace,
                {"source": str(self.source), "project_id": "cancelled-run", "profile": "research"},
            )
        cancelled = cancel_analysis_run(self.workspace, str(queued["run_id"]))
        self.assertEqual("cancelled", cancelled["state"])
        self.assertIsNotNone(cancelled["cancel_requested_at"])
        with patch("video_analysis_mvp.run_lifecycle._launch"):
            retried = retry_analysis_run(self.workspace, str(queued["run_id"]))
        self.assertEqual("queued", retried["state"])

    def test_running_subprocess_is_cancelled_before_its_stage_timeout(self) -> None:
        from video_analysis_mvp.utils import run_command

        project_id = "cancel-running-process"
        media = self._media(project_id)
        visual_started = threading.Event()

        def blocking_visual(*_args: object, **_kwargs: object) -> None:
            visual_started.set()
            run_command(
                [sys.executable, "-c", "import time; time.sleep(5)"],
                timeout=5,
            )

        with (
            patch("video_analysis_mvp.run_lifecycle.verify_media_generation", return_value=(False, ["missing"])),
            patch("video_analysis_mvp.run_lifecycle.ingest_source", return_value=media),
            patch("video_analysis_mvp.run_lifecycle.set_delivery_language"),
            patch("video_analysis_mvp.run_lifecycle.verify_visual_generation", return_value=(False, ["missing"])),
            patch("video_analysis_mvp.run_lifecycle.analyze_visual", side_effect=blocking_visual),
        ):
            started = start_analysis_run(
                self.workspace,
                {"source": str(self.source), "project_id": project_id, "profile": "research"},
            )
            self.assertTrue(visual_started.wait(timeout=5))
            cancelled_at = time.monotonic()
            cancel_analysis_run(self.workspace, str(started["run_id"]))
            terminal = self._wait_for_terminal(self.workspace, str(started["run_id"]))

        self.assertEqual("cancelled", terminal["state"])
        self.assertLess(time.monotonic() - cancelled_at, 2.0)

    def test_unverified_process_cleanup_is_failed_not_cancelled(self) -> None:
        project_id = "cancel-cleanup-unverified"
        media = self._media(project_id)
        visual_started = threading.Event()
        release_visual = threading.Event()

        def unverified_cleanup(*_args: object, **_kwargs: object) -> None:
            visual_started.set()
            release_visual.wait(timeout=5)
            raise ProcessCancelledError("cancelled", cleanup_verified=False)

        with (
            patch("video_analysis_mvp.run_lifecycle.verify_media_generation", return_value=(False, ["missing"])),
            patch("video_analysis_mvp.run_lifecycle.ingest_source", return_value=media),
            patch("video_analysis_mvp.run_lifecycle.set_delivery_language"),
            patch("video_analysis_mvp.run_lifecycle.verify_visual_generation", return_value=(False, ["missing"])),
            patch("video_analysis_mvp.run_lifecycle.analyze_visual", side_effect=unverified_cleanup),
        ):
            started = start_analysis_run(
                self.workspace,
                {"source": str(self.source), "project_id": project_id, "profile": "research"},
            )
            self.assertTrue(visual_started.wait(timeout=5))
            cancel_analysis_run(self.workspace, str(started["run_id"]))
            release_visual.set()
            terminal = self._wait_for_terminal(self.workspace, str(started["run_id"]))

        self.assertEqual("failed", terminal["state"])
        self.assertEqual("ProcessCleanupUnverified", terminal["error"]["type"])
        self.assertTrue(terminal["error"]["retriable"])

    def test_workspace_api_exposes_async_run_without_allowing_background_vision(self) -> None:
        response = {
            "schema_version": 1,
            "run_id": "4b1a44ed-67e1-45ac-8570-fc9af3dc24bd",
            "project_id": "api-run",
            "state": "queued",
        }
        body = {
            "source": str(self.source),
            "project_id": "api-run",
            "profile": "research",
            "with_vision": False,
        }
        with patch("video_analysis_mvp.run_lifecycle.start_analysis_run", return_value=response) as starter:
            status, payload = dispatch_api(
                self.workspace,
                "POST",
                "/api/runs",
                "",
                json.dumps(body).encode("utf-8"),
            )
        self.assertEqual(202, status)
        self.assertEqual(response, payload)
        self.assertNotIn("password", starter.call_args.args[1])

        body["with_vision"] = True
        with patch("video_analysis_mvp.run_lifecycle.start_analysis_run") as starter:
            with self.assertRaises(ApiError) as caught:
                dispatch_api(
                    self.workspace,
                    "POST",
                    "/api/runs",
                    "",
                    json.dumps(body).encode("utf-8"),
                )
        self.assertEqual(400, caught.exception.status)
        starter.assert_not_called()

    def test_workspace_api_maps_admission_pressure_to_429(self) -> None:
        body = {
            "source": str(self.source),
            "project_id": "api-admission",
            "profile": "research",
        }
        with patch(
            "video_analysis_mvp.run_lifecycle.start_analysis_run",
            side_effect=RunAdmissionError("workspace busy"),
        ):
            with self.assertRaises(ApiError) as caught:
                dispatch_api(
                    self.workspace,
                    "POST",
                    "/api/runs",
                    "",
                    json.dumps(body).encode("utf-8"),
                )

        self.assertEqual(429, caught.exception.status)

        retry_id = "4b1a44ed-67e1-45ac-8570-fc9af3dc24bd"
        with (
            patch(
                "video_analysis_mvp.run_lifecycle.retry_analysis_run",
                side_effect=RunAdmissionError("disk budget unavailable"),
            ),
            self.assertRaises(ApiError) as retry_caught,
        ):
            dispatch_api(
                self.workspace,
                "POST",
                f"/api/runs/{retry_id}/retry",
                "",
                b"{}",
            )

        self.assertEqual(429, retry_caught.exception.status)

    def test_retry_and_cancel_reject_malformed_run_ids_as_bad_requests(self) -> None:
        for action in ("retry", "cancel"):
            with self.subTest(action=action), self.assertRaises(ApiError) as caught:
                dispatch_api(self.workspace, "POST", f"/api/runs/not-a-uuid/{action}", "", b"{}")
            self.assertEqual(400, caught.exception.status)


if __name__ == "__main__":
    unittest.main()
