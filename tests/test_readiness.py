from __future__ import annotations

import base64
import os
import hashlib
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from video_analysis_mvp.audio import _stage_and_commit_audio_generation
from video_analysis_mvp.boundary_review import build_boundary_review_receipt
from video_analysis_mvp.config import save_runtime_config
from video_analysis_mvp.paths import ProjectPaths
from video_analysis_mvp.readiness import (
    canonical_readiness_payload,
    canonical_json_digest,
    evaluate_project_readiness,
    evaluate_readiness,
    has_vision_key,
    write_readiness,
)
from video_analysis_mvp.schemas import (
    AnalysisProfile,
    CanonicalMediaPackage,
    Shot,
    SourceType,
    dump_json,
    load_json,
)
from video_analysis_mvp.synthesis import _commit_report_generation
from video_analysis_mvp.visual import _build_visual_generation_receipt, visual_generation_binding
from video_analysis_mvp.workspace_api import (
    ApiError,
    deliverable_preview,
    derive_media_timeline,
    is_current_project_file,
    update_shot_review,
)


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class VisionProviderConfigurationTest(unittest.TestCase):
    def test_key_must_match_the_selected_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            with patch.dict(os.environ, {"MINIMAX_API_KEY": "minimax-test-key"}, clear=True):
                self.assertFalse(has_vision_key(workspace))
                save_runtime_config(workspace, {"vision_provider": "minimax_mcp"})
                self.assertTrue(has_vision_key(workspace))

            with patch.dict(os.environ, {"OPENAI_API_KEY": "openai-test-key"}, clear=True):
                self.assertFalse(has_vision_key(workspace))
                save_runtime_config(workspace, {"vision_provider": "openai"})
                self.assertTrue(has_vision_key(workspace))

    def test_custom_endpoint_rejects_ambient_key_but_accepts_exact_configured_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            save_runtime_config(workspace, {"openai_base_url": "https://gateway.example/v1"})
            with patch.dict(os.environ, {"OPENAI_API_KEY": "ambient-only"}, clear=True):
                self.assertFalse(has_vision_key(workspace))
            save_runtime_config(workspace, {"openai_api_key": "endpoint-bound"})
            with patch.dict(os.environ, {}, clear=True):
                self.assertTrue(has_vision_key(workspace))

    def test_unknown_provider_never_falls_back_to_an_ambient_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(
                os.environ,
                {"VIDEO_ANALYSIS_VISION_PROVIDER": "unknown", "OPENAI_API_KEY": "ambient"},
                clear=True,
            ):
                self.assertFalse(has_vision_key(Path(temp_dir)))

    @staticmethod
    def complete_shot(number: int, source: str, status: str = "ready") -> Shot:
        return Shot(
            shot_id=f"shot-{number}",
            shot_no=number,
            start_time=float(number - 1),
            end_time=float(number),
            duration=1.0,
            primary_frame_ref=f"frame-{number}.jpg",
            story_beat="observation",
            content_summary="A documented frame",
            subject="person",
            action="walking",
            shot_scale="medium",
            camera_angle="eye level",
            camera_motion="static",
            composition="centered",
            remake_notes="Record the setup",
            boundary_confidence="high",
            visual_confidence=0.9,
            confidence=0.9,
            annotation_source=source,
            readiness_status=status,
        )

    def test_configured_key_does_not_verify_machine_annotations(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            shot = self.complete_shot(1, "machine")
            with patch.dict(os.environ, {"OPENAI_API_KEY": "openai-test-key"}, clear=True):
                report = evaluate_readiness([shot], workspace_root=workspace)

            self.assertTrue(report["vision_key_configured"])
            self.assertEqual("blocked", report["status"])
            self.assertFalse(shot.professional_ready)

    def test_provider_source_self_assertion_cannot_pass_without_receipt(self) -> None:
        shots = [self.complete_shot(1, "vision"), self.complete_shot(2, "openai")]
        report = evaluate_readiness(shots)

        self.assertFalse(report["vision_annotation_complete"])
        self.assertEqual("blocked", report["status"])
        self.assertTrue(all(not item["provider_receipt_verified"] for item in report["shot_results"]))

    def test_partial_human_review_preserves_reviewed_shot_but_blocks_export(self) -> None:
        reviewed = self.complete_shot(1, "human")
        unreviewed = self.complete_shot(2, "machine")
        report = evaluate_readiness([reviewed, unreviewed])

        self.assertEqual("blocked", report["status"])
        self.assertTrue(report["shot_results"][0]["professional_ready"])
        self.assertFalse(report["shot_results"][1]["professional_ready"])
        self.assertFalse(reviewed.professional_ready, "readiness evaluation must not mutate source evidence")

        unreviewed.annotation_source = "human"
        unreviewed.readiness_status = "ready"
        report = evaluate_readiness([reviewed, unreviewed])
        self.assertTrue(report["human_review_override"])
        self.assertEqual("ready", report["status"])

    def test_one_incomplete_shot_cannot_pass_aggregate_export_gate(self) -> None:
        shots = [self.complete_shot(number, "human") for number in range(1, 11)]
        shots[-1].composition = ""

        report = evaluate_readiness(shots)

        self.assertEqual("blocked", report["status"])
        self.assertFalse(report["professional_export_allowed"])
        self.assertFalse(report["human_review_override"])
        self.assertFalse(report["shot_results"][-1]["professional_ready"])
        self.assertIn("missing composition", report["shot_results"][-1]["reasons"])
        self.assertIn("1 shot(s) fail professional readiness", report["reasons"])


class ProjectReadinessIntegrityTest(unittest.TestCase):
    metadata = {
        "streams": [
            {
                "codec_type": "video",
                "duration": "2.0",
                "width": 320,
                "height": 180,
                "avg_frame_rate": "24/1",
            }
        ],
        "format": {"duration": "2.0"},
    }

    def project(self, directory: str, *, profile: AnalysisProfile = AnalysisProfile.research, source: str = "human"):
        paths = ProjectPaths(Path(directory) / "audit-project")
        paths.ensure()
        master = paths.ingest / "master.mp4"
        review = paths.assets / "review.mp4"
        frame = paths.keyframes / "shot_0001.png"
        master.write_bytes(b"master-media-v1")
        review.write_bytes(b"review-media-v1")
        frame.write_bytes(PNG_1X1)
        media = CanonicalMediaPackage(
            project_id=paths.root.name,
            source_type=SourceType.file,
            source="source.mp4",
            local_master_path=str(master),
            review_copy_path=str(review),
            audio_path=str(paths.assets / "audio.wav"),
            duration_seconds=2.0,
            frame_rate=24.0,
            resolution="320x180",
            aspect_ratio=320 / 180,
            status="analyzed",
            analysis_profile=profile,
            metadata={
                "media_receipt": {
                    "schema_version": "1.0",
                    "master": self._media_receipt(master),
                    "review": self._media_receipt(review),
                }
            },
        )
        shot = VisionProviderConfigurationTest.complete_shot(1, source)
        shot.shot_id = "shot_0001"
        shot.start_time = 0.0
        shot.end_time = 2.0
        shot.duration = 2.0
        shot.frame_ref = frame.name
        shot.primary_frame_ref = frame.name
        shot.frame_refs = [frame.name]
        if profile == AnalysisProfile.ads:
            shot.story_beat = "setup"
        else:
            shot.story_beat = "heuristic_unverified:opening_sequence"
        dump_json(paths.data / "media_package.json", media)
        dump_json(paths.data / "shots.json", [shot])
        dump_json(paths.data / "scenes.json", [])
        (paths.assets / "contact_sheet.jpg").write_bytes(PNG_1X1)
        dump_json(
            paths.data / "visual_generation.json",
            _build_visual_generation_receipt(paths, [shot], []),
        )
        _stage_and_commit_audio_generation(paths, [], [], [])
        dump_json(
            paths.manifest,
            {
                "project_id": paths.root.name,
                "profile": profile.value,
                "root_path": str(paths.root),
                "source": "source.mp4",
                "status": "reported",
                "artifacts": {},
            },
        )
        return paths, media, shot

    def test_absent_audio_timeline_is_unknown_not_silence_and_not_a_new_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, _media, _shot = self.project(directory)
            report = evaluate_project_readiness(
                paths.root,
                workspace_root=paths.root.parent,
                require_persisted_receipt=False,
            )

        self.assertTrue(report["professional_export_allowed"], report["reasons"])
        self.assertIs(report["audio_timeline_available"], False)
        self.assertIsNone(report["audio_review_complete"])
        self.assertEqual(0, report["audio_event_count"])
        self.assertEqual(0, report["audio_requires_review_count"])
        self.assertFalse(any("silence" in reason.lower() for reason in report["reasons"]))

    @staticmethod
    def publish(paths: ProjectPaths, media: CanonicalMediaPackage, artifacts: dict[str, str]) -> None:
        _commit_report_generation(
            paths,
            media,
            str(uuid.uuid4()),
            {**artifacts, "project_manifest": str(paths.manifest)},
        )

    @staticmethod
    def _media_receipt(path: Path) -> dict[str, object]:
        return {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size_bytes": path.stat().st_size,
            "duration_seconds": 2.0,
            "frame_rate": 24.0,
            "resolution": "320x180",
            "aspect_ratio": 320 / 180,
        }

    def write_provider_receipt(self, paths: ProjectPaths, media: CanonicalMediaPackage, shot: Shot) -> None:
        frame = (paths.keyframes / shot.frame_ref).read_bytes()
        frame_digest = hashlib.sha256(frame).hexdigest()
        shot_digest = canonical_json_digest(shot.model_dump(mode="json"))
        receipt = media.metadata["media_receipt"]
        dump_json(
            paths.data / "vision_annotations.json",
            {
                "schema_version": "1.0",
                "run_id": "run-verified-001",
                "started_at": "2026-07-15T00:00:00Z",
                "completed_at": "2026-07-15T00:00:01Z",
                "provider": "openai",
                "provider_source": "openai_chat_completions",
                "model": "gpt-test",
                "endpoint_origin": "https://api.openai.com",
                "adapter": None,
                "selected_shot_ids": [shot.shot_id],
                "annotated_shot_ids": [shot.shot_id],
                "skipped_shot_ids": [],
                "diagnostics": [],
                "media_binding": {
                    "status": "bound",
                    "media_package_sha256": canonical_json_digest(media.model_dump(mode="json")),
                    "receipt_schema_version": "1.0",
                    "master_sha256": receipt["master"]["sha256"],
                    "review_sha256": receipt["review"]["sha256"],
                },
                "shot_receipts": [
                    {"shot_id": shot.shot_id, "shot_sha256": shot_digest, "frame_sha256": frame_digest}
                ],
                "input_frames": [
                    {
                        "shot_id": shot.shot_id,
                        "frame_ref": shot.frame_ref,
                        "sha256": frame_digest,
                        "size_bytes": len(frame),
                        "media_type": "image/png",
                        "width": 1,
                        "height": 1,
                    }
                ],
                "annotations": [shot.model_dump(mode="json")],
            },
        )

    def test_low_confidence_boundary_requires_a_current_explicit_human_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, _media, shot = self.project(directory)
            shot.boundary_confidence = "low"
            dump_json(paths.data / "shots.json", [shot])
            dump_json(
                paths.data / "visual_generation.json",
                _build_visual_generation_receipt(paths, [shot], []),
            )

            blocked = evaluate_project_readiness(
                paths.root,
                workspace_root=paths.root.parent,
                require_persisted_receipt=False,
            )
            self.assertEqual("blocked", blocked["status"])
            self.assertFalse(blocked["boundary_review_complete"])
            self.assertIn(
                "low boundary confidence requires explicit human boundary review",
                blocked["shot_results"][0]["reasons"],
            )

            binding = visual_generation_binding(paths, [shot])
            receipt = build_boundary_review_receipt(paths.root, [shot], binding, {shot.shot_id})
            dump_json(paths.data / "boundary_review.json", receipt)
            reviewed = evaluate_project_readiness(
                paths.root,
                workspace_root=paths.root.parent,
                require_persisted_receipt=False,
            )
            self.assertEqual("ready", reviewed["status"])
            self.assertTrue(reviewed["boundary_review_complete"])
            self.assertTrue(reviewed["shot_results"][0]["boundary_reviewed"])
            self.assertEqual(0.0, reviewed["low_boundary_confidence_rate"])
            self.assertEqual(1.0, reviewed["detected_low_boundary_confidence_rate"])

            forged = load_json(paths.data / "boundary_review.json")
            forged["receipt_digest"] = "sha256:" + "0" * 64
            dump_json(paths.data / "boundary_review.json", forged)
            invalid = evaluate_project_readiness(
                paths.root,
                workspace_root=paths.root.parent,
                require_persisted_receipt=False,
            )
            self.assertEqual("blocked", invalid["status"])
            self.assertTrue(any("boundary review receipt is invalid" in reason for reason in invalid["reasons"]))

    def test_primary_review_api_writes_the_boundary_receipt_without_changing_detector_confidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, _media, shot = self.project(directory)
            shot.boundary_confidence = "low"
            dump_json(paths.data / "shots.json", [shot])
            dump_json(
                paths.data / "visual_generation.json",
                _build_visual_generation_receipt(paths, [shot], []),
            )
            timeline = derive_media_timeline(paths.root.parent, paths.root)
            edit_version = timeline["shot_boundaries"][0]["edit_version"]

            response = update_shot_review(
                paths.root.parent,
                paths.root,
                shot.shot_id,
                {
                    "expected_shot_digest": edit_version,
                    "readiness_status": "ready",
                    "boundary_reviewed": True,
                },
            )

            persisted = Shot.model_validate(load_json(paths.data / "shots.json")[0])
            self.assertEqual("low", persisted.boundary_confidence)
            self.assertTrue(response["shot"]["review_fields"]["boundary_reviewed"])
            self.assertTrue((paths.data / "boundary_review.json").is_file())
            current = evaluate_project_readiness(
                paths.root,
                workspace_root=paths.root.parent,
                require_persisted_receipt=False,
            )
            self.assertEqual("ready", current["status"])

    def evaluate(self, paths: ProjectPaths):
        with patch("video_analysis_mvp.media.ffprobe_metadata", return_value=self.metadata):
            return evaluate_project_readiness(paths.root)

    def write_gate(self, paths: ProjectPaths, shot: Shot):
        with patch("video_analysis_mvp.media.ffprobe_metadata", return_value=self.metadata):
            return write_readiness(paths.data / "readiness.json", [shot])

    def test_all_non_ads_profiles_accept_complete_bound_provider_observations(self) -> None:
        for profile in (
            AnalysisProfile.research,
            AnalysisProfile.streaming,
            AnalysisProfile.shortform,
            AnalysisProfile.festival,
        ):
            with self.subTest(profile=profile.value), tempfile.TemporaryDirectory() as directory:
                paths, media, shot = self.project(directory, profile=profile, source="openai")
                self.write_provider_receipt(paths, media, shot)
                report = self.write_gate(paths, shot)
                self.assertEqual("ready", report["status"], report["reasons"])
                current = self.evaluate(paths)
                self.assertTrue(current["vision_annotation_complete"], current["reasons"])
                self.assertTrue(current["professional_export_allowed"], current["reasons"])

    def test_missing_visual_generation_receipt_blocks_professional_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, _media, shot = self.project(directory, source="human")
            (paths.data / "visual_generation.json").unlink()

            report = self.write_gate(paths, shot)

            self.assertFalse(report["professional_export_allowed"])
            self.assertIn("visual generation receipt is missing", "\n".join(report["reasons"]))
            self.assertFalse(report["shot_results"][0]["professional_ready"])

    def test_fake_frame_bytes_block_human_and_provider_paths(self) -> None:
        for source in ("human", "openai"):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as directory:
                paths, media, shot = self.project(directory, source=source)
                (paths.keyframes / shot.frame_ref).write_bytes(b"not-an-image")
                if source == "openai":
                    self.write_provider_receipt(paths, media, shot)
                report = self.write_gate(paths, shot)
                self.assertFalse(report["professional_export_allowed"])
                self.assertTrue(
                    any("frame reference is missing or unsafe" in reason for reason in report["shot_results"][0]["reasons"]),
                    report,
                )

    def test_media_timeline_overlays_current_shot_gate_and_hides_unbound_review_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, _media, shot = self.project(directory, source="human")
            self.write_gate(paths, shot)
            with patch("video_analysis_mvp.media.ffprobe_metadata", return_value=self.metadata):
                current = derive_media_timeline(paths.root.parent, paths.root)
            self.assertEqual("ready", current["shot_boundaries"][0]["readiness_status"])
            self.assertTrue(current["review_video"]["binding_valid"])
            self.assertIsNotNone(current["review_video"]["url"])

            raw = load_json(paths.data / "shots.json")
            raw[0]["composition"] = ""
            raw[0]["readiness_status"] = "ready"
            raw[0]["readiness_reasons"] = []
            dump_json(paths.data / "shots.json", raw)
            with patch("video_analysis_mvp.media.ffprobe_metadata", return_value=self.metadata):
                stale = derive_media_timeline(paths.root.parent, paths.root)
            self.assertEqual("blocked", stale["shot_boundaries"][0]["readiness_status"])
            self.assertIn("missing composition", stale["shot_boundaries"][0]["readiness_reasons"])

            (paths.assets / "review.mp4").write_bytes(b"replacement-media")
            with patch("video_analysis_mvp.media.ffprobe_metadata", return_value=self.metadata):
                unbound = derive_media_timeline(paths.root.parent, paths.root)
            self.assertFalse(unbound["review_video"]["binding_valid"])
            self.assertIsNone(unbound["review_video"]["url"])

    def test_provider_frame_receipt_requires_exact_type_dimensions_and_size(self) -> None:
        for field, forged in (
            ("media_type", "image/jpeg"),
            ("width", 2),
            ("height", 2),
            ("size_bytes", len(PNG_1X1) + 1),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                paths, media, shot = self.project(directory, source="openai")
                self.write_provider_receipt(paths, media, shot)
                receipt = load_json(paths.data / "vision_annotations.json")
                receipt["input_frames"][0][field] = forged
                dump_json(paths.data / "vision_annotations.json", receipt)
                report = self.write_gate(paths, shot)
                self.assertFalse(report["professional_export_allowed"])
                self.assertIn("vision receipt frame digest mismatch", "\n".join(report["reasons"]))

    def test_readiness_digest_binds_every_decision_field_and_unknown_fields(self) -> None:
        mutations = (
            ("analysis_profile", "streaming"),
            ("shot_count", 2),
            ("human_review_override", False),
            ("average_visual_confidence", 0.1),
            ("reasons", ["forged reason"]),
            ("unknown_extra", {"trusted": True}),
        )
        for field, forged in mutations:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                paths, _media, shot = self.project(directory, source="human")
                self.write_gate(paths, shot)
                stored = load_json(paths.data / "readiness.json")
                stored[field] = forged
                stored["report_digest"] = canonical_json_digest(canonical_readiness_payload(stored))
                dump_json(paths.data / "readiness.json", stored)
                current = self.evaluate(paths)
                self.assertFalse(current["professional_export_allowed"])
                self.assertFalse(current["stored_readiness_valid"])

    def test_ambient_key_change_does_not_stale_an_evidence_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, _media, shot = self.project(directory, source="human")
            with patch.dict(os.environ, {}, clear=True):
                self.write_gate(paths, shot)
            with patch.dict(os.environ, {"OPENAI_API_KEY": "later-capability"}, clear=True):
                current = self.evaluate(paths)
            self.assertTrue(current["stored_readiness_valid"], current["reasons"])
            self.assertTrue(current["professional_export_allowed"], current["reasons"])

    def test_shot_change_and_forged_or_stale_readiness_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, media, shot = self.project(directory, source="openai")
            self.write_provider_receipt(paths, media, shot)
            self.write_gate(paths, shot)
            shot.content_summary = "changed after provider receipt"
            dump_json(paths.data / "shots.json", [shot])
            current = self.evaluate(paths)
            self.assertFalse(current["professional_export_allowed"])
            self.assertTrue(any("stale" in reason for reason in current["reasons"]))

            dump_json(paths.data / "readiness.json", {"status": "ready", "professional_export_allowed": True})
            forged = self.evaluate(paths)
            self.assertFalse(forged["professional_export_allowed"])
            self.assertFalse(forged["stored_readiness_valid"])

    def test_duplicate_timeline_and_unsafe_or_missing_frames_block(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, _media, first = self.project(directory)
            second = Shot.model_validate(first.model_dump(mode="json"))
            second.start_time = 1.0
            second.end_time = 2.0
            second.duration = 1.0
            second.primary_frame_ref = "../escape.png"
            second.frame_ref = "missing.png"
            second.frame_refs = ["missing.png"]
            dump_json(paths.data / "shots.json", [first, second])
            report = self.evaluate(paths)
            joined = "\n".join(reason for item in report["shot_results"] for reason in item["reasons"])
            self.assertIn("shot_id must be unique", joined)
            self.assertIn("shot_no must be unique", joined)
            self.assertIn("overlapping shots are not allowed", joined)
            self.assertIn("frame reference is missing or unsafe", joined)
            self.assertFalse(report["professional_export_allowed"])

    def test_media_deletion_replacement_and_strict_json_numbers_block(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, _media, shot = self.project(directory)
            self.write_gate(paths, shot)
            (paths.assets / "review.mp4").write_bytes(b"replacement-with-same-name")
            replaced = self.evaluate(paths)
            self.assertTrue(any("review media sha256" in reason for reason in replaced["reasons"]))
            (paths.assets / "review.mp4").write_bytes(b"review-media-v1")
            with patch(
                "video_analysis_mvp.media.ffprobe_metadata",
                side_effect=AssertionError("readiness must trust metadata only after exact SHA binding"),
            ):
                drifted = evaluate_project_readiness(paths.root)
            self.assertFalse(any("media duration does not match receipt" in reason for reason in drifted["reasons"]))
            self.assertTrue(drifted["professional_export_allowed"], drifted["reasons"])
            (paths.ingest / "master.mp4").unlink()
            deleted = self.evaluate(paths)
            self.assertTrue(any("master media file is missing" in reason for reason in deleted["reasons"]))

            (paths.ingest / "master.mp4").write_bytes(b"master-media-v1")
            original = load_json(paths.data / "shots.json")
            for field, invalid in (
                ("start_time", True),
                ("start_time", "0.0"),
                ("visual_confidence", "0.9"),
                ("visual_confidence", 1.1),
            ):
                with self.subTest(field=field, invalid=invalid):
                    raw = [dict(original[0])]
                    raw[0][field] = invalid
                    dump_json(paths.data / "shots.json", raw)
                    strict = self.evaluate(paths)
                    self.assertFalse(strict["professional_export_allowed"])
            (paths.data / "shots.json").write_text(
                '[{"shot_id":"shot_0001","shot_no":1,"start_time":NaN,"end_time":2,"duration":2}]',
                encoding="utf-8",
            )
            non_finite = self.evaluate(paths)
            self.assertTrue(any("shots receipt is missing or invalid" in reason for reason in non_finite["reasons"]))

    def test_validation_cache_hits_and_managed_replacement_invalidates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, media, shot = self.project(directory, source="human")
            from video_analysis_mvp import readiness as readiness_module

            readiness_module._clear_validation_caches()
            real_read = os.read
            with patch("video_analysis_mvp.readiness.os.read", side_effect=real_read) as read:
                first = evaluate_readiness([shot], project_root=paths.root, media=media)
                self.assertTrue(first["professional_export_allowed"], first["reasons"])
                self.assertGreater(read.call_count, 0)
                read.reset_mock()
                second = evaluate_readiness([shot], project_root=paths.root, media=media)
                self.assertTrue(second["professional_export_allowed"], second["reasons"])
                self.assertEqual(0, read.call_count)

                (paths.keyframes / shot.frame_ref).write_bytes(b"replacement")
                replaced = evaluate_readiness([shot], project_root=paths.root, media=media)
                self.assertFalse(replaced["professional_export_allowed"])
                self.assertGreater(read.call_count, 0)

    def test_frame_directory_and_symlink_are_not_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, _media, shot = self.project(directory)
            outside = Path(directory) / "outside.png"
            outside.write_bytes(b"outside")
            (paths.keyframes / "linked.png").symlink_to(outside)
            (paths.keyframes / "directory.png").mkdir()
            for reference in ("linked.png", "directory.png"):
                with self.subTest(reference=reference):
                    shot.frame_ref = reference
                    shot.primary_frame_ref = reference
                    shot.frame_refs = [reference]
                    dump_json(paths.data / "shots.json", [shot])
                    report = self.evaluate(paths)
                    joined = "\n".join(reason for item in report["shot_results"] for reason in item["reasons"])
                    self.assertIn("frame reference is missing or unsafe", joined)
                    self.assertFalse(report["professional_export_allowed"])

    def test_current_file_allowlist_includes_review_frames_and_valid_receipts_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, media, shot = self.project(directory, source="openai")
            storyboard = paths.reports / "storyboard.html"
            storyboard.write_text("current", encoding="utf-8")
            stale_alias = paths.reports / "old-alias.txt"
            stale_alias.write_text("old", encoding="utf-8")
            self.write_provider_receipt(paths, media, shot)
            self.write_gate(paths, shot)
            self.publish(
                paths,
                media,
                {
                    "storyboard_html": str(storyboard),
                    "readiness_json": str(paths.data / "readiness.json"),
                },
            )
            with patch("video_analysis_mvp.media.ffprobe_metadata", return_value=self.metadata):
                self.assertTrue(is_current_project_file(paths.root, storyboard))
                self.assertTrue(is_current_project_file(paths.root, paths.assets / "review.mp4"))
                self.assertTrue(is_current_project_file(paths.root, paths.keyframes / shot.frame_ref))
                self.assertTrue(is_current_project_file(paths.root, paths.data / "readiness.json"))
                self.assertTrue(is_current_project_file(paths.root, paths.data / "vision_annotations.json"))
                self.assertFalse(is_current_project_file(paths.root, stale_alias))

            receipt = load_json(paths.data / "vision_annotations.json")
            receipt["input_frames"][0]["width"] = 2
            dump_json(paths.data / "vision_annotations.json", receipt)
            with patch("video_analysis_mvp.media.ffprobe_metadata", return_value=self.metadata):
                self.assertFalse(is_current_project_file(paths.root, paths.data / "vision_annotations.json"))

    def test_only_canonical_manifest_artifact_uses_current_path_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, media, shot = self.project(directory)
            report = paths.reports / "profile_analysis.html"
            report.write_text("professional", encoding="utf-8")
            self.write_gate(paths, shot)
            self.publish(paths, media, {"profile_analysis_html": str(report)})
            with (
                patch("video_analysis_mvp.media.ffprobe_metadata", return_value=self.metadata),
                self.assertRaises(ApiError) as alias,
            ):
                deliverable_preview(paths.root.parent, paths.root, "client_report_alias")
            self.assertEqual(404, alias.exception.status)
            with patch("video_analysis_mvp.media.ffprobe_metadata", return_value=self.metadata):
                preview = deliverable_preview(paths.root.parent, paths.root, "profile_analysis_html")
            self.assertEqual("professional", preview["text"])

            outside = Path(directory) / "outside.html"
            outside.write_text("outside", encoding="utf-8")
            report.unlink()
            report.symlink_to(outside)
            with (
                patch("video_analysis_mvp.media.ffprobe_metadata", return_value=self.metadata),
                self.assertRaises(ApiError) as unsafe,
            ):
                deliverable_preview(paths.root.parent, paths.root, "profile_analysis_html")
            self.assertEqual(404, unsafe.exception.status)
            report.unlink()
            report.write_text("professional", encoding="utf-8")

            shot.composition = ""
            dump_json(paths.data / "shots.json", [shot])
            with (
                patch("video_analysis_mvp.media.ffprobe_metadata", return_value=self.metadata),
                self.assertRaises(ApiError) as caught,
            ):
                deliverable_preview(paths.root.parent, paths.root, "profile_analysis_html")
            self.assertEqual(404, caught.exception.status)

if __name__ == "__main__":
    unittest.main()
