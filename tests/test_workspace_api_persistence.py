from __future__ import annotations

import json
import os
import tempfile
import unittest
import uuid
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import Iterator
from unittest.mock import patch

from video_analysis_mvp.audio import _stage_and_commit_audio_generation
from video_analysis_mvp.media import _build_review_copy, _extract_audio
from video_analysis_mvp.paths import ProjectPaths
from video_analysis_mvp.readiness import evaluate_project_readiness
from video_analysis_mvp.safe_io import advisory_file_lock, atomic_output_path, atomic_write_text
from video_analysis_mvp.schemas import AnalysisProfile, CanonicalMediaPackage, SourceType, dump_json, load_json
from video_analysis_mvp.store import find_projects
from video_analysis_mvp.synthesis import _commit_report_generation, verify_report_generation_manifest
from video_analysis_mvp.visual import _build_visual_generation_receipt
from video_analysis_mvp.workspace_api import (
    ApiError,
    MAX_PROJECT_JSON_BYTES,
    dispatch_api,
    ensure_project_data,
)


class WorkspaceApiPersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="workspace-api-persistence-")
        self.base = Path(self.tempdir.name)
        self.workspace = self.base / "workspace"
        self.project = self.workspace / "safe-project"
        self.data = self.project / "data"
        self.data.mkdir(parents=True)
        self._write_json(
            self.project / "project_manifest.json",
            {
                "project_id": "safe-project",
                "profile": "research",
                "root_path": str(self.project),
                "source": "synthetic",
                "status": "reported",
                "artifacts": {
                    "storyboard_html": str(self.project / "reports" / "storyboard.html"),
                },
            },
        )
        self._write_json(
            self.data / "shots.json",
            [
                {
                    "shot_id": "shot_0001",
                    "shot_no": 1,
                    "start_time": 0.0,
                    "end_time": 1.0,
                    "duration": 1.0,
                    "story_beat": "observation",
                    "annotation_source": "machine",
                    "readiness_status": "blocked",
                    "prompt_zh": "campaign prompt must stay ads-only",
                    "remake_notes_zh": "campaign remake note must stay ads-only",
                }
            ],
        )

    def tearDown(self) -> None:
        for path in (self.project, self.data):
            try:
                path.chmod(0o700)
            except OSError:
                pass
        self.tempdir.cleanup()

    @staticmethod
    def _write_json(path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def _dispatch(self, method: str, suffix: str, body: object | bytes | None = None):
        encoded = body if isinstance(body, bytes) else json.dumps(body or {}).encode("utf-8")
        return dispatch_api(
            self.workspace,
            method,
            f"/api/projects/safe-project{suffix}",
            "",
            encoded,
        )

    def test_canvas_and_timeline_symlinks_fail_closed_without_touching_outside(self) -> None:
        for filename, suffix in (("canvas_graph.json", "/canvas"), ("media_timeline.json", "/media")):
            with self.subTest(filename=filename):
                outside = self.base / f"outside-{filename}"
                original = b'{"outside":"unchanged"}'
                outside.write_bytes(original)
                target = self.data / filename
                target.symlink_to(outside)

                with self.assertRaises(ApiError) as caught:
                    self._dispatch("GET", suffix)
                self.assertEqual(409, caught.exception.status)
                self.assertEqual(original, outside.read_bytes())

                with self.assertRaises(ApiError):
                    ensure_project_data(self.workspace, self.project)
                self.assertEqual(original, outside.read_bytes())
                target.unlink()

    def test_symlinked_data_parent_fails_before_initialization(self) -> None:
        for child in self.data.iterdir():
            child.unlink()
        self.data.rmdir()
        outside = self.base / "outside-data"
        outside.mkdir()
        marker = outside / "sentinel"
        marker.write_text("unchanged", encoding="utf-8")
        self.data.symlink_to(outside, target_is_directory=True)

        with self.assertRaises(ApiError) as caught:
            self._dispatch("GET", "/canvas")
        self.assertEqual(409, caught.exception.status)
        with self.assertRaises(ApiError):
            ensure_project_data(self.workspace, self.project)

        self.assertEqual("unchanged", marker.read_text(encoding="utf-8"))
        self.assertEqual([marker], list(outside.iterdir()))

    def test_gets_are_pure_stable_and_work_on_a_read_only_project(self) -> None:
        ensure_project_data(self.workspace, self.project)
        tracked = [self.data / "canvas_graph.json", self.data / "media_timeline.json"]
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in tracked}
        self.data.chmod(0o500)
        self.project.chmod(0o500)
        try:
            first = [
                self._dispatch("GET", "")[1],
                self._dispatch("GET", "/canvas")[1],
                self._dispatch("GET", "/media")[1],
            ]
            second = [
                self._dispatch("GET", "")[1],
                self._dispatch("GET", "/canvas")[1],
                self._dispatch("GET", "/media")[1],
            ]
        finally:
            self.project.chmod(0o700)
            self.data.chmod(0o700)

        self.assertEqual(first, second)
        self.assertEqual(before, {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in tracked})

    def test_workspace_snapshot_returns_one_stable_bundle_under_the_shots_lock(self) -> None:
        with patch(
            "video_analysis_mvp.workspace_api.advisory_file_lock",
            wraps=advisory_file_lock,
        ) as locker, patch(
            "video_analysis_mvp.workspace_api.evaluate_project_readiness",
            wraps=evaluate_project_readiness,
        ) as readiness_evaluator, patch(
            "video_analysis_mvp.workspace_api.verify_report_generation_manifest",
            wraps=verify_report_generation_manifest,
        ) as generation_verifier:
            status, first = self._dispatch("GET", "/workspace")
        second = self._dispatch("GET", "/workspace")[1]

        self.assertEqual(200, status)
        self.assertEqual(
            {"snapshot_id", "generation_id", "project", "canvas", "media", "deliverables"},
            set(first),
        )
        self.assertRegex(first["snapshot_id"], r"^sha256:[0-9a-f]{64}$")
        self.assertIsNone(first["generation_id"])
        self.assertEqual(first, second)
        resolved_project = self.project.resolve()
        locker.assert_called_once_with(resolved_project / "data" / ".shots.lock", root=resolved_project)
        self.assertEqual(1, readiness_evaluator.call_count)
        self.assertIs(readiness_evaluator.call_args.kwargs.get("_shots_lock_held"), True)
        self.assertEqual(1, generation_verifier.call_count)
        self.assertIs(generation_verifier.call_args.kwargs.get("_shots_lock_held"), True)

    def test_paths_needing_both_locks_always_take_project_before_shots(self) -> None:
        events: list[str] = []
        project_depth = 0

        @contextmanager
        def traced_project_lock(_project: Path) -> Iterator[None]:
            nonlocal project_depth
            events.append("project-enter")
            project_depth += 1
            try:
                yield
            finally:
                project_depth -= 1
                events.append("project-exit")

        @contextmanager
        def traced_shots_lock(_path: Path, *, root: Path | None = None) -> Iterator[None]:
            del root
            self.assertGreater(project_depth, 0, "shots lock was requested before the project lock")
            events.append("shots-enter")
            try:
                yield
            finally:
                events.append("shots-exit")

        with (
            patch("video_analysis_mvp.workspace_api.project_write_lock", side_effect=traced_project_lock),
            patch("video_analysis_mvp.workspace_api.advisory_file_lock", side_effect=traced_shots_lock),
        ):
            self._dispatch("GET", "/workspace")
        self.assertEqual(
            ["project-enter", "shots-enter", "shots-exit", "project-exit"],
            events,
        )

    def test_primary_workspace_shot_review_is_versioned_and_fail_closed(self) -> None:
        media = self._dispatch("GET", "/media")[1]
        edit_version = media["shot_boundaries"][0]["edit_version"]

        status, response = self._dispatch(
            "PATCH",
            "/shots/shot_0001",
            {
                "expected_shot_digest": edit_version,
                "content_summary": "A person opens a notebook.",
                "subject": "person",
                "action": "opens a notebook",
                "shot_scale": "medium",
                "camera_angle": "eye level",
                "camera_motion": "static",
                "composition": "centered",
                "visual_confidence": 0.9,
                "readiness_status": "ready",
            },
        )

        persisted = load_json(self.data / "shots.json")[0]
        self.assertEqual(200, status)
        self.assertIs(response["review_saved"], True)
        self.assertIs(response["report_regeneration_required"], True)
        self.assertRegex(response["saved_shot_digest"], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual("human", persisted["annotation_source"])
        self.assertEqual("ready", persisted["readiness_status"])
        self.assertEqual("A person opens a notebook.", persisted["content_summary"])
        self.assertEqual("operator reviewed in the primary workspace", persisted["review_notes"])
        self.assertFalse(response["readiness"]["professional_export_allowed"])
        self.assertIn("review_fields", response["shot"])
        invalidated_manifest = load_json(self.project / "project_manifest.json")
        self.assertEqual("review_pending", invalidated_manifest["status"])
        self.assertEqual({}, invalidated_manifest["artifacts"])
        self.assertNotIn("report_generation", invalidated_manifest)
        self.assertEqual(
            {
                "schema_version": 1,
                "reason": "shot_review_saved",
                "shot_id": "shot_0001",
                "requires_finalize": True,
            },
            invalidated_manifest["report_invalidation"],
        )

        before = (self.data / "shots.json").read_bytes()
        with self.assertRaises(ApiError) as caught:
            self._dispatch(
                "PATCH",
                "/shots/shot_0001",
                {
                    "expected_shot_digest": edit_version,
                    "readiness_status": "blocked",
                },
            )
        self.assertEqual(409, caught.exception.status)
        self.assertEqual(before, (self.data / "shots.json").read_bytes())

    def test_restoring_identical_review_content_cannot_revive_an_old_generation(self) -> None:
        initial = self._dispatch("GET", "/media")[1]["shot_boundaries"][0]
        ready = self._dispatch(
            "PATCH",
            "/shots/shot_0001",
            {
                "expected_shot_digest": initial["edit_version"],
                "readiness_status": "ready",
                "review_notes": "stable reviewed state",
            },
        )[1]

        manifest = load_json(self.project / "project_manifest.json")
        manifest["status"] = "reported"
        manifest["artifacts"] = {"storyboard_html": str(self.project / "reports" / "storyboard.html")}
        manifest["report_generation"] = {
            "schema_version": 3,
            "generation_id": str(uuid.uuid4()),
            "run_id": "stale-generation",
            "state": "committed",
            "digest_algorithm": "sha256",
            "source_receipts": {},
            "artifact_digests": {},
        }
        manifest.pop("report_invalidation", None)
        dump_json(self.project / "project_manifest.json", manifest)

        blocked = self._dispatch(
            "PATCH",
            "/shots/shot_0001",
            {
                "expected_shot_digest": ready["saved_shot_digest"],
                "readiness_status": "blocked",
                "review_notes": "temporary blocked state",
            },
        )[1]
        restored = self._dispatch(
            "PATCH",
            "/shots/shot_0001",
            {
                "expected_shot_digest": blocked["saved_shot_digest"],
                "readiness_status": "ready",
                "review_notes": "stable reviewed state",
            },
        )[1]

        final_manifest = load_json(self.project / "project_manifest.json")
        self.assertEqual("review_pending", final_manifest["status"])
        self.assertNotIn("report_generation", final_manifest)
        self.assertIs(restored["report_regeneration_required"], True)
        self.assertFalse(restored["readiness"]["professional_export_allowed"])

    def test_primary_workspace_shot_review_rejects_ambiguous_or_unsafe_fields(self) -> None:
        edit_version = self._dispatch("GET", "/media")[1]["shot_boundaries"][0]["edit_version"]
        cases = [
            {"expected_shot_digest": edit_version, "readiness_status": "ready", "annotation_source": "openai"},
            {"expected_shot_digest": edit_version, "readiness_status": "invented"},
            {"expected_shot_digest": edit_version, "readiness_status": "ready", "visual_confidence": True},
            {
                "expected_shot_digest": edit_version,
                "readiness_status": "ready",
                "content_summary": "x" * 8193,
            },
        ]
        before = (self.data / "shots.json").read_bytes()
        for body in cases:
            with self.subTest(body=body), self.assertRaises(ApiError) as caught:
                self._dispatch("PATCH", "/shots/shot_0001", body)
            self.assertEqual(400, caught.exception.status)
            self.assertEqual(before, (self.data / "shots.json").read_bytes())

    def test_concurrent_primary_workspace_reviews_allow_one_edit_version_winner(self) -> None:
        edit_version = self._dispatch("GET", "/media")[1]["shot_boundaries"][0]["edit_version"]

        def save(label: str) -> int:
            try:
                return self._dispatch(
                    "PATCH",
                    "/shots/shot_0001",
                    {
                        "expected_shot_digest": edit_version,
                        "readiness_status": "blocked",
                        "content_summary": label,
                    },
                )[0]
            except ApiError as exc:
                return exc.status

        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = sorted(pool.map(save, ["first", "second"]))

        self.assertEqual([200, 409], statuses)
        self.assertIn(load_json(self.data / "shots.json")[0]["content_summary"], {"first", "second"})

    def test_primary_workspace_exposes_and_saves_shot_twenty_five_without_a_display_cap(self) -> None:
        template = load_json(self.data / "shots.json")[0]
        shots = []
        for index in range(25):
            shot = dict(template)
            shot["shot_id"] = f"shot_{index + 1:04d}"
            shot["shot_no"] = index + 1
            shot["start_time"] = float(index)
            shot["end_time"] = float(index + 1)
            shots.append(shot)
        dump_json(self.data / "shots.json", shots)

        media = self._dispatch("GET", "/media")[1]
        self.assertEqual(25, len(media["shot_boundaries"]))
        last = media["shot_boundaries"][-1]
        status, response = self._dispatch(
            "PATCH",
            "/shots/shot_0025",
            {
                "expected_shot_digest": last["edit_version"],
                "readiness_status": "blocked",
                "content_summary": "The twenty-fifth shot is reviewable in the primary workspace.",
            },
        )
        self.assertEqual(200, status)
        self.assertEqual("shot_0025", response["shot_id"])
        self.assertEqual(
            "The twenty-fifth shot is reviewable in the primary workspace.",
            load_json(self.data / "shots.json")[-1]["content_summary"],
        )

    def test_concurrent_gets_never_initialize_project_files(self) -> None:
        targets = [self.data / "canvas_graph.json", self.data / "media_timeline.json"]
        with patch("video_analysis_mvp.workspace_api._write_json") as writer:
            with ThreadPoolExecutor(max_workers=12) as pool:
                results = list(
                    pool.map(
                        lambda suffix: self._dispatch("GET", suffix)[0],
                        ["", "/canvas", "/media", "/media/review-video"] * 8,
                    )
                )
        self.assertEqual([200] * len(results), results)
        writer.assert_not_called()
        self.assertTrue(all(not path.exists() for path in targets))

    def test_readiness_get_recomputes_without_rewriting_stored_receipt(self) -> None:
        receipt = self.data / "readiness.json"
        receipt.write_bytes(b'{"schema_version":1,"status":"ready","professional_export_allowed":true}')
        before = (receipt.read_bytes(), receipt.stat().st_mtime_ns)

        first = self._dispatch("GET", "/readiness")[1]
        second = self._dispatch("GET", "/readiness")[1]

        self.assertEqual(first, second)
        self.assertFalse(first["readiness"]["professional_export_allowed"])
        self.assertEqual(before, (receipt.read_bytes(), receipt.stat().st_mtime_ns))

    def test_partial_json_is_never_replaced_by_a_mutation(self) -> None:
        path = self.data / "canvas_graph.json"
        partial = b'{"version":"graph_001"'
        path.write_bytes(partial)

        with self.assertRaises(ApiError) as caught:
            self._dispatch("PATCH", "/canvas/viewport", {"zoom": 1.0})

        self.assertEqual(409, caught.exception.status)
        self.assertEqual(partial, path.read_bytes())

    def test_concurrent_marker_and_manual_node_mutations_preserve_every_update(self) -> None:
        ensure_project_data(self.workspace, self.project)

        def add_marker(index: int) -> None:
            self._dispatch(
                "POST",
                "/media/frame-markers",
                {"id": f"frame_marker_{index:03d}", "time": 0.5, "label": f"marker {index}"},
            )

        def add_node(index: int) -> None:
            self._dispatch(
                "POST",
                "/canvas/nodes",
                {"id": f"manual_{index:03d}", "type": "insight", "x": index, "y": index},
            )

        with ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(add_marker, range(20)))
            list(pool.map(add_node, range(20)))

        timeline = load_json(self.data / "media_timeline.json")
        graph = load_json(self.data / "canvas_graph.json")
        self.assertEqual({f"frame_marker_{index:03d}" for index in range(20)}, {item["id"] for item in timeline["markers"]})
        self.assertTrue({f"manual_{index:03d}" for index in range(20)}.issubset(set(graph["manual_nodes"])))
        node_ids = {item["id"] for item in graph["nodes"]}
        self.assertTrue({f"frame_marker_{index:03d}" for index in range(20)}.issubset(node_ids))
        self.assertTrue({f"manual_{index:03d}" for index in range(20)}.issubset(node_ids))

    def test_non_finite_negative_and_extreme_numbers_are_rejected(self) -> None:
        cases = [
            ("PATCH", "/canvas/viewport", b'{"zoom":NaN}'),
            ("PATCH", "/canvas/viewport", {"zoom": 100}),
            ("POST", "/canvas/nodes", {"x": 2_000_000}),
            ("POST", "/media/frame-markers", {"time": -1}),
            ("POST", "/media/segments", {"start_time": 0, "end_time": 100_000}),
        ]
        for method, suffix, body in cases:
            with self.subTest(method=method, suffix=suffix, body=body):
                with self.assertRaises(ApiError) as caught:
                    self._dispatch(method, suffix, body)
                self.assertEqual(400, caught.exception.status)

    def test_non_ads_media_and_canvas_omit_campaign_only_keys_and_nodes(self) -> None:
        media = self._dispatch("GET", "/media")[1]
        canvas = self._dispatch("GET", "/canvas")[1]

        self.assertNotIn("prompt", media["shot_boundaries"][0])
        self.assertNotIn("remake_tip", media["shot_boundaries"][0])
        self.assertFalse(
            {"prompt", "branch", "keeper_decision"}
            & {item.get("type") for item in canvas["nodes"] if isinstance(item, dict)}
        )
        for node in canvas["nodes"]:
            if isinstance(node, dict) and isinstance(node.get("data"), dict):
                self.assertNotIn("prompt", node["data"])
                self.assertNotIn("remake_tip", node["data"])

    def test_manifest_symlink_is_not_a_project_or_a_listing_entry(self) -> None:
        outside = self.base / "secret-manifest.json"
        outside.write_text(
            json.dumps(
                {
                    "project_id": "safe-project",
                    "profile": "research",
                    "root_path": str(self.project),
                    "source": "DO-NOT-LEAK",
                    "status": "reported",
                    "artifacts": {},
                }
            ),
            encoding="utf-8",
        )
        manifest = self.project / "project_manifest.json"
        manifest.unlink()
        manifest.symlink_to(outside)

        with self.assertRaises(ApiError) as caught:
            self._dispatch("GET", "")

        rendered = f"{caught.exception.message} {caught.exception.details}"
        self.assertEqual(404, caught.exception.status)
        self.assertNotIn("DO-NOT-LEAK", rendered)
        self.assertEqual([], find_projects(str(self.workspace)))

    def test_intake_canonicalizes_relative_source_and_enforces_validated_limit(self) -> None:
        source = self.base / "clip.mp4"
        source.write_bytes(b"real-source")
        other = self.base / "other-cwd"
        other.mkdir()
        (other / "clip.mp4").write_bytes(b"wrong-source")
        metadata = {
            "streams": [
                {
                    "codec_type": "video",
                    "duration": "1.0",
                    "width": 1920,
                    "height": 1080,
                    "avg_frame_rate": "24/1",
                }
            ],
            "format": {},
        }
        pipeline_result = unittest.mock.MagicMock()
        pipeline_result.model_dump.return_value = {"artifacts": {}}
        previous_cwd = Path.cwd()
        try:
            os.chdir(other)
            with (
                patch("video_analysis_mvp.media.ffprobe_metadata", return_value=metadata),
                patch("video_analysis_mvp.pipeline.run_full_pipeline", return_value=pipeline_result) as runner,
            ):
                status, _payload = dispatch_api(
                    self.workspace,
                    "POST",
                    "/api/projects",
                    "",
                    json.dumps({"source": "clip.mp4", "max_duration_seconds": 12}).encode(),
                )
        finally:
            os.chdir(previous_cwd)

        self.assertEqual(201, status)
        self.assertEqual(str(source.resolve()), runner.call_args.args[0])
        self.assertEqual(12.0, runner.call_args.kwargs["max_duration_seconds"])
        self.assertEqual("en", runner.call_args.kwargs["delivery_language"])

    def test_react_intake_rejects_http_sources_and_points_trusted_operators_to_cli(self) -> None:
        for route in ("/api/intake/validate", "/api/projects"):
            with self.subTest(route=route), self.assertRaises(ApiError) as caught:
                dispatch_api(
                    self.workspace,
                    "POST",
                    route,
                    "",
                    json.dumps({"source": "https://user:secret@example.com/video.mp4?token=secret"}).encode(),
                )
            rendered = f"{caught.exception.message} {caught.exception.details}"
            self.assertEqual(400, caught.exception.status)
            self.assertIn("CLI", rendered)
            self.assertIn("trusted operator", rendered.lower())
            self.assertNotIn("secret", rendered)

    def test_project_json_reads_and_deliverable_previews_are_bounded(self) -> None:
        oversized = self.data / "canvas_graph.json"
        oversized.write_bytes(b" " * (MAX_PROJECT_JSON_BYTES + 1))
        with self.assertRaises(ApiError) as caught:
            self._dispatch("GET", "/canvas")
        self.assertEqual(413, caught.exception.status)

        report = self.project / "reports" / "storyboard.html"
        report.parent.mkdir()
        report.write_text("x" * 100_000, encoding="utf-8")
        media = CanonicalMediaPackage(
            project_id=self.project.name,
            source_type=SourceType.file,
            source="synthetic",
            local_master_path=str(self.project / "ingest" / "master.mp4"),
            review_copy_path=str(self.project / "assets" / "review.mp4"),
            audio_path=str(self.project / "assets" / "audio.wav"),
            duration_seconds=1.0,
            frame_rate=24.0,
            resolution="320x180",
            aspect_ratio=16 / 9,
            status="analyzed",
            analysis_profile=AnalysisProfile.research,
        )
        paths = ProjectPaths(self.project)
        paths.ensure()
        dump_json(paths.data / "media_package.json", media)
        dump_json(paths.data / "shots.json", [])
        dump_json(paths.data / "scenes.json", [])
        (paths.assets / "contact_sheet.jpg").write_bytes(b"contact")
        dump_json(
            paths.data / "visual_generation.json",
            _build_visual_generation_receipt(paths, [], []),
        )
        _stage_and_commit_audio_generation(paths, [], [], [])
        _commit_report_generation(
            paths,
            media,
            str(uuid.uuid4()),
            {
                "storyboard_html": str(report),
                "project_manifest": str(self.project / "project_manifest.json"),
            },
        )
        status, preview = self._dispatch("GET", "/deliverables/storyboard_html/preview")
        self.assertEqual(200, status)
        self.assertEqual(60_000, len(preview["text"]))
        self.assertTrue(preview["truncated"])

        # A report publisher that follows the shots transaction must not be
        # able to replace the authorized file before the preview opens it.
        from video_analysis_mvp.workspace_api import read_regular_bytes as real_read_regular_bytes

        reader_reached = Event()
        writer_attempting = Event()
        writer_entered = Event()

        def replace_report() -> None:
            self.assertTrue(reader_reached.wait(2), "preview never reached its descriptor read")
            writer_attempting.set()
            with advisory_file_lock(self.data / ".shots.lock", root=self.project):
                report.write_text("NEW-UNCOMMITTED", encoding="utf-8")
                writer_entered.set()

        def controlled_read(*args: object, **kwargs: object) -> bytes:
            reader_reached.set()
            self.assertTrue(writer_attempting.wait(2), "writer never attempted the shots transaction")
            writer_entered.wait(0.2)
            return real_read_regular_bytes(*args, **kwargs)

        with ThreadPoolExecutor(max_workers=1) as pool:
            writer = pool.submit(replace_report)
            with patch(
                "video_analysis_mvp.workspace_api.read_regular_bytes",
                side_effect=controlled_read,
            ):
                raced_status, raced_preview = self._dispatch(
                    "GET",
                    "/deliverables/storyboard_html/preview",
                )
            writer.result(timeout=2)

        self.assertEqual(200, raced_status)
        self.assertEqual("x" * 60_000, raced_preview["text"])
        self.assertTrue(raced_preview["truncated"])
        self.assertTrue(writer_entered.is_set())


class AtomicSchemaJsonTest(unittest.TestCase):
    def test_dump_json_rejects_symlink_and_non_finite_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "outside.json"
            outside.write_text('{"unchanged":true}', encoding="utf-8")
            target = root / "target.json"
            target.symlink_to(outside)

            with self.assertRaises(ValueError):
                dump_json(target, {"changed": True})
            with self.assertRaises(ValueError):
                load_json(target)
            self.assertEqual('{"unchanged":true}', outside.read_text(encoding="utf-8"))

            target.unlink()
            target.write_text('{"stable":true}', encoding="utf-8")
            with self.assertRaises(ValueError):
                dump_json(target, {"value": float("nan")})
            self.assertEqual('{"stable":true}', target.read_text(encoding="utf-8"))
            self.assertEqual([], list(root.glob(".*.tmp")))

    def test_load_json_rejects_non_standard_constants(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text('{"value":Infinity}', encoding="utf-8")
            with self.assertRaises(ValueError):
                load_json(path)

    def test_project_paths_reject_symlinked_core_output_directories(self) -> None:
        for directory_name in ("ingest", "assets", "keyframes", "data", "reports"):
            with self.subTest(directory_name=directory_name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                project = root / "project"
                project.mkdir()
                outside = root / "outside"
                outside.mkdir()
                sentinel = outside / "sentinel"
                sentinel.write_text("unchanged", encoding="utf-8")
                target = project / "assets" / "keyframes" if directory_name == "keyframes" else project / directory_name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(outside, target_is_directory=True)

                with self.assertRaises(ValueError):
                    ProjectPaths(project).ensure()

                self.assertEqual("unchanged", sentinel.read_text(encoding="utf-8"))
                self.assertEqual([sentinel], list(outside.iterdir()))

    def test_core_text_json_and_subprocess_targets_reject_symlink_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = ProjectPaths(root / "project")
            paths.ensure()
            outside = root / "outside.txt"
            outside.write_text("unchanged", encoding="utf-8")

            targets = [
                paths.data / "receipt.json",
                paths.reports / "report.html",
                paths.assets / "frame.jpg",
                paths.ingest / "master.mp4",
                paths.keyframes / "shot.jpg",
            ]
            for target in targets:
                target.symlink_to(outside)

            with self.assertRaises(ValueError):
                dump_json(targets[0], {"changed": True})
            with self.assertRaises(ValueError):
                atomic_write_text(targets[1], "changed")
            with self.assertRaises(ValueError), atomic_output_path(targets[2]):
                pass
            with self.assertRaises(ValueError), atomic_output_path(targets[3]):
                pass
            with self.assertRaises(ValueError), atomic_output_path(targets[4]):
                pass

            self.assertEqual("unchanged", outside.read_text(encoding="utf-8"))

    def test_ffmpeg_outputs_use_atomic_temporary_targets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory) / "project")
            paths.ensure()
            master = paths.ingest / "master.mp4"
            master.write_bytes(b"input")

            def produce(args: list[str], timeout: int) -> None:
                output = Path(args[-1])
                self.assertNotIn(output, {paths.assets / "review.mp4", paths.assets / "audio.wav"})
                output.write_bytes(b"generated")

            with (
                patch(
                    "video_analysis_mvp.media.require_tool",
                    return_value="/verified/ffmpeg",
                ),
                patch(
                    "video_analysis_mvp.media.run_command",
                    side_effect=produce,
                ) as runner,
            ):
                _build_review_copy(master, paths.assets / "review.mp4", 360)
                _extract_audio(paths.assets / "review.mp4", paths.assets / "audio.wav")

            self.assertEqual(2, runner.call_count)
            self.assertTrue(
                all(call.args[0][0] == "/verified/ffmpeg" for call in runner.call_args_list)
            )
            self.assertEqual(b"generated", (paths.assets / "review.mp4").read_bytes())
            self.assertEqual(b"generated", (paths.assets / "audio.wav").read_bytes())
            self.assertEqual([], list(paths.assets.glob(".*.tmp*")))

            outside = Path(directory) / "outside.mp4"
            outside.write_bytes(b"unchanged")
            hostile_target = paths.assets / "hostile.mp4"
            hostile_target.symlink_to(outside)
            with (
                patch(
                    "video_analysis_mvp.media.require_tool",
                    return_value="/verified/ffmpeg",
                ),
                patch("video_analysis_mvp.media.run_command") as hostile_runner,
                self.assertRaises(ValueError),
            ):
                _build_review_copy(master, hostile_target, 360)
            hostile_runner.assert_not_called()
            self.assertEqual(b"unchanged", outside.read_bytes())


if __name__ == "__main__":
    unittest.main()
