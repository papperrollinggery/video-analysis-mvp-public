from __future__ import annotations

import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from video_analysis_mvp.delivery import _camera_text, enforce_profile_output_boundary
from video_analysis_mvp.audio import _stage_and_commit_audio_generation
from video_analysis_mvp.paths import ProjectPaths
from video_analysis_mvp.safe_io import advisory_file_lock
from video_analysis_mvp.schemas import (
    AnalysisProfile,
    CanonicalMediaPackage,
    Shot,
    SourceType,
    TranscriptSegment,
    dump_json,
    load_json,
)
from video_analysis_mvp.synthesis import (
    _begin_report_generation,
    _attach_audio_to_shots,
    _clear_artifact_digest_cache,
    _commit_report_generation,
    synthesize,
    verify_report_generation_manifest,
)
from video_analysis_mvp.visual import _build_visual_generation_receipt
from video_analysis_mvp.web import save_keeper_decision


def _media(paths: ProjectPaths, profile: AnalysisProfile = AnalysisProfile.research) -> CanonicalMediaPackage:
    return CanonicalMediaPackage(
        project_id=paths.root.name,
        source_type=SourceType.file,
        source="source.mp4",
        local_master_path=str(paths.ingest / "master.mp4"),
        review_copy_path=str(paths.assets / "review.mp4"),
        audio_path=str(paths.assets / "audio.wav"),
        duration_seconds=2.0,
        frame_rate=24.0,
        resolution="1920x1080",
        aspect_ratio=16 / 9,
        status="analyzed",
        analysis_profile=profile,
        metadata={"delivery_language": "zh"},
    )


def _shot() -> Shot:
    return Shot(
        shot_id="shot_0001",
        shot_no=1,
        start_time=0.0,
        end_time=2.0,
        duration=2.0,
        timecode="00:00-00:02",
        frame_ref="frame-0001.jpg",
        primary_frame_ref="frame-0001.jpg",
        frame_refs=["frame-0001.jpg"],
        boundary_confidence="high",
        story_beat="opening_sequence",
        scene_type="opening_sequence",
        annotation_source="machine",
        readiness_status="blocked",
    )


def _install_source_generation_receipts(
    paths: ProjectPaths,
    shot: Shot | None = None,
    profile: AnalysisProfile = AnalysisProfile.research,
) -> None:
    current_shot = shot or _shot()
    dump_json(paths.data / "media_package.json", _media(paths, profile))
    frame = paths.keyframes / current_shot.frame_ref
    if not frame.exists():
        frame.write_bytes(b"frame-v1")
    contact = paths.assets / "contact_sheet.jpg"
    if not contact.exists():
        contact.write_bytes(b"contact-v1")
    dump_json(paths.data / "shots.json", [current_shot])
    dump_json(paths.data / "scenes.json", [])
    dump_json(
        paths.data / "visual_generation.json",
        _build_visual_generation_receipt(paths, [current_shot], []),
    )
    _stage_and_commit_audio_generation(paths, [], [], [])


class ReportGenerationReceiptTest(unittest.TestCase):
    def test_audio_attachment_preserves_explicit_human_dialogue_decisions(self) -> None:
        transcript = [
            TranscriptSegment(
                segment_id="segment_0001",
                start_time=0.0,
                end_time=1.0,
                text="machine transcript",
            )
        ]
        reviewed = _shot().model_copy(
            update={
                "annotation_source": "human",
                "readiness_status": "ready",
                "dialogue": "Human-reviewed dialogue",
            }
        )
        reviewed_silence = _shot().model_copy(
            update={
                "shot_id": "shot_0002",
                "annotation_source": "human",
                "readiness_status": "ready",
                "dialogue": "",
            }
        )
        machine = _shot().model_copy(update={"shot_id": "shot_0003", "dialogue": "stale machine text"})

        _attach_audio_to_shots([reviewed, reviewed_silence, machine], transcript, [], [])

        self.assertEqual("Human-reviewed dialogue", reviewed.dialogue)
        self.assertEqual("", reviewed_silence.dialogue)
        self.assertEqual("machine transcript", machine.dialogue)
        for shot in (reviewed, reviewed_silence, machine):
            self.assertEqual("machine transcript", shot.speech_summary)

    def test_committed_manifest_binds_every_file_directory_and_itself(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory) / "receipt-project")
            paths.ensure()
            report_path = paths.reports / "report.html"
            report_path.write_bytes(b"report-v1")
            frame_path = paths.keyframes / "frame-0001.jpg"
            frame_path.write_bytes(b"frame-v1")
            _install_source_generation_receipts(paths)
            artifacts = {
                "report_html": str(report_path),
                "keyframes": str(paths.keyframes),
                "project_manifest": str(paths.manifest),
            }

            _commit_report_generation(paths, _media(paths), str(uuid.uuid4()), artifacts)

            manifest = load_json(paths.manifest)
            generation = manifest["report_generation"]
            receipts = generation["artifact_digests"]
            self.assertEqual("committed", generation["state"])
            self.assertEqual(generation["generation_id"], generation["run_id"])
            self.assertEqual(set(artifacts), set(receipts))
            self.assertEqual("file", receipts["report_html"]["kind"])
            self.assertEqual("directory", receipts["keyframes"]["kind"])
            self.assertEqual(1, receipts["keyframes"]["file_count"])
            self.assertEqual("manifest", receipts["project_manifest"]["kind"])
            self.assertEqual(
                {"audio_generation", "audio_intelligence", "readiness", "visual_generation"},
                set(generation["source_receipts"]),
            )
            readiness_binding = generation["source_receipts"]["readiness"]
            self.assertEqual(4, generation["schema_version"])
            self.assertIsNone(generation["source_receipts"]["audio_intelligence"])
            self.assertEqual("canonical-readiness-v1", readiness_binding["digest_mode"])
            self.assertIsNone(readiness_binding["vision_receipt_binding"])
            self.assertEqual((True, []), verify_report_generation_manifest(paths))

            report_path.write_bytes(b"report-v2")
            valid, reasons = verify_report_generation_manifest(paths)
            self.assertFalse(valid)
            self.assertIn("artifact digest mismatch: report_html", reasons)

    def test_legacy_manifest_without_generation_receipt_is_not_current(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory) / "legacy-project")
            paths.ensure()
            dump_json(
                paths.manifest,
                {
                    "project_id": paths.root.name,
                    "profile": "research",
                    "root_path": str(paths.root),
                    "source": "source.mp4",
                    "status": "reported",
                    "artifacts": {},
                },
            )

            valid, reasons = verify_report_generation_manifest(paths)

            self.assertFalse(valid)
            self.assertEqual(["report generation receipt is missing"], reasons)

    def test_unknown_alias_and_noncanonical_paths_fail_before_artifact_reads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory) / "contract-project")
            paths.ensure()
            report_path = paths.reports / "report.html"
            report_path.write_bytes(b"report")
            _install_source_generation_receipts(paths)
            with self.assertRaisesRegex(ValueError, "unsupported report artifact id"):
                _commit_report_generation(
                    paths,
                    _media(paths),
                    str(uuid.uuid4()),
                    {
                        "project_manifest": str(paths.manifest),
                        "unbounded_alias": str(paths.root),
                    },
                )
            with self.assertRaisesRegex(ValueError, "non-canonical"):
                _commit_report_generation(
                    paths,
                    _media(paths),
                    str(uuid.uuid4()),
                    {
                        "project_manifest": str(paths.manifest),
                        "report_html": str(paths.reports / "storyboard.html"),
                    },
                )

    def test_failed_generation_invalidates_old_manifest_before_output_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory) / "failed-project")
            paths.ensure()
            media = _media(paths)
            dump_json(paths.data / "media_package.json", media)
            dump_json(paths.data / "shots.json", [_shot()])
            dump_json(paths.data / "scenes.json", [])
            _install_source_generation_receipts(paths, _shot())
            old_report = paths.reports / "storyboard.html"
            old_report.write_bytes(b"old-generation")
            dump_json(
                paths.manifest,
                {
                    "project_id": media.project_id,
                    "profile": "research",
                    "root_path": str(paths.root),
                    "source": media.source,
                    "status": "reported",
                    "artifacts": {"storyboard_html": str(old_report)},
                },
            )

            def fail_after_mutation(*_args: object, **_kwargs: object) -> dict[str, str]:
                old_report.write_bytes(b"partial-new-generation")
                raise RuntimeError("injected delivery failure")

            with patch(
                "video_analysis_mvp.synthesis.write_profile_delivery_package",
                side_effect=fail_after_mutation,
            ):
                with self.assertRaisesRegex(RuntimeError, "injected delivery failure"):
                    synthesize(paths)

            marker = load_json(paths.manifest)
            self.assertEqual("publishing", marker["status"])
            self.assertEqual({}, marker["artifacts"])
            self.assertEqual("publishing", marker["report_generation"]["state"])
            valid, reasons = verify_report_generation_manifest(paths)
            self.assertFalse(valid)
            self.assertIn("report generation is not committed", reasons)

    def test_synthesis_rejects_missing_source_generation_receipts_before_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory) / "legacy-source-project")
            paths.ensure()
            media = _media(paths)
            shot = _shot()
            dump_json(paths.data / "media_package.json", media)
            dump_json(paths.data / "shots.json", [shot])
            dump_json(paths.data / "scenes.json", [])
            shots_before = (paths.data / "shots.json").read_bytes()

            with self.assertRaisesRegex(ValueError, "visual generation is invalid"):
                synthesize(paths)

            self.assertFalse(paths.manifest.exists())
            self.assertEqual(shots_before, (paths.data / "shots.json").read_bytes())

    def test_verifier_and_report_publisher_are_linearized_by_the_shots_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory) / "concurrent-project")
            paths.ensure()
            media = _media(paths)
            report_path = paths.reports / "report.html"
            report_path.write_bytes(b"report-v1")
            _install_source_generation_receipts(paths)
            artifacts = {
                "report_html": str(report_path),
                "project_manifest": str(paths.manifest),
            }
            _commit_report_generation(paths, media, str(uuid.uuid4()), artifacts)

            verifier_entered = threading.Event()
            release_verifier = threading.Event()
            publisher_done = threading.Event()
            result: list[tuple[bool, list[str]]] = []

            from video_analysis_mvp import synthesis as synthesis_module

            real_receipts = synthesis_module._artifact_receipts

            def paused_receipts(*args: object, **kwargs: object):
                if threading.current_thread().name == "verifier":
                    verifier_entered.set()
                    self.assertTrue(release_verifier.wait(5))
                return real_receipts(*args, **kwargs)

            def publish_next_generation() -> None:
                with advisory_file_lock(paths.data / ".shots.lock", root=paths.root):
                    _begin_report_generation(paths, media, str(uuid.uuid4()))
                    report_path.write_bytes(b"report-v2")
                publisher_done.set()

            with patch("video_analysis_mvp.synthesis._artifact_receipts", side_effect=paused_receipts):
                verifier = threading.Thread(
                    target=lambda: result.append(verify_report_generation_manifest(paths)),
                    name="verifier",
                )
                verifier.start()
                self.assertTrue(verifier_entered.wait(5))
                publisher = threading.Thread(target=publish_next_generation, name="publisher")
                publisher.start()
                self.assertFalse(publisher_done.wait(0.2))
                release_verifier.set()
                verifier.join(5)
                publisher.join(5)

            self.assertFalse(verifier.is_alive())
            self.assertFalse(publisher.is_alive())
            self.assertEqual([(True, [])], result)
            self.assertTrue(publisher_done.is_set())
            valid, reasons = verify_report_generation_manifest(paths)
            self.assertFalse(valid)
            self.assertIn("report generation is not committed", reasons)

    def test_artifact_digest_cache_hits_and_replacement_invalidates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory) / "cache-project")
            paths.ensure()
            media = _media(paths)
            report_path = paths.reports / "report.html"
            report_path.write_bytes(b"report-v1")
            _install_source_generation_receipts(paths)
            _commit_report_generation(
                paths,
                media,
                str(uuid.uuid4()),
                {
                    "report_html": str(report_path),
                    "project_manifest": str(paths.manifest),
                },
            )

            _clear_artifact_digest_cache()
            from video_analysis_mvp import synthesis as synthesis_module

            real_cache_get = synthesis_module._artifact_digest_cache_get
            misses: list[tuple[object, ...]] = []

            def tracked_cache_get(key: tuple[object, ...]):
                value = real_cache_get(key)
                if value is None:
                    misses.append(key)
                return value

            with patch(
                "video_analysis_mvp.synthesis._artifact_digest_cache_get",
                side_effect=tracked_cache_get,
            ):
                self.assertEqual((True, []), verify_report_generation_manifest(paths))
                self.assertTrue(misses)
                misses.clear()
                self.assertEqual((True, []), verify_report_generation_manifest(paths))
                self.assertEqual([], misses)

                report_path.write_bytes(b"report-v2")
                valid, reasons = verify_report_generation_manifest(paths)
                self.assertFalse(valid)
                self.assertIn("artifact digest mismatch: report_html", reasons)
                self.assertTrue(misses)

    def test_report_generation_is_bound_to_current_visual_and_audio_receipts(self) -> None:
        for source_path in ("data/visual_generation.json", "data/audio_generation.json"):
            with self.subTest(source_path=source_path), tempfile.TemporaryDirectory() as directory:
                paths = ProjectPaths(Path(directory) / "source-binding-project")
                paths.ensure()
                report_path = paths.reports / "report.html"
                report_path.write_bytes(b"report-v1")
                _install_source_generation_receipts(paths)
                _commit_report_generation(
                    paths,
                    _media(paths),
                    str(uuid.uuid4()),
                    {
                        "report_html": str(report_path),
                        "project_manifest": str(paths.manifest),
                    },
                )

                receipt_path = paths.root / source_path
                receipt = load_json(receipt_path)
                receipt["generation_id"] = "0" * 64
                dump_json(receipt_path, receipt)

                valid, reasons = verify_report_generation_manifest(paths)
                self.assertFalse(valid)
                self.assertTrue(
                    any("report source generation verification failed" in reason for reason in reasons),
                    reasons,
                )

    def test_report_generation_is_bound_to_full_shot_semantics_and_media(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory) / "semantic-binding-project")
            paths.ensure()
            media = _media(paths)
            shot = _shot()
            report_path = paths.reports / "storyboard.html"
            report_path.write_text("OLD REPORT: old dialogue", encoding="utf-8")
            _install_source_generation_receipts(paths, shot)
            artifacts = {
                "storyboard_html": str(report_path),
                "project_manifest": str(paths.manifest),
            }
            _commit_report_generation(paths, media, str(uuid.uuid4()), artifacts)

            changed = shot.model_copy(deep=True)
            changed.dialogue = "new dialogue"
            changed.subject = "new subject"
            dump_json(paths.data / "shots.json", [changed])

            valid, reasons = verify_report_generation_manifest(paths)
            self.assertFalse(valid)
            self.assertIn("report source generation receipts are stale or forged", reasons)

            report_path.write_text("NEW REPORT: new dialogue", encoding="utf-8")
            _commit_report_generation(paths, media, str(uuid.uuid4()), artifacts)
            self.assertEqual((True, []), verify_report_generation_manifest(paths))

            changed_media = media.model_copy(update={"resolution": "1280x720"})
            dump_json(paths.data / "media_package.json", changed_media)
            valid, reasons = verify_report_generation_manifest(paths)
            self.assertFalse(valid)
            self.assertIn("report source generation receipts are stale or forged", reasons)

    def test_keeper_decision_is_independent_from_committed_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = ProjectPaths(root / "keeper-project")
            paths.ensure()
            media = _media(paths, AnalysisProfile.ads)
            _install_source_generation_receipts(paths, profile=AnalysisProfile.ads)
            lineage = paths.data / "lineage.json"
            lineage.write_text('{"schema_version":1,"nodes":[],"commits":[],"branches":[]}', encoding="utf-8")
            artifacts = {
                "lineage_json": str(lineage),
                "project_manifest": str(paths.manifest),
            }
            _commit_report_generation(paths, media, str(uuid.uuid4()), artifacts)
            before = lineage.read_bytes()

            save_keeper_decision(root, paths.root.name, {"keeper_branch": "safer"})

            self.assertEqual(before, lineage.read_bytes())
            self.assertEqual("safer", load_json(paths.data / "keeper_decision.json")["keeper_branch"])
            self.assertEqual((True, []), verify_report_generation_manifest(paths))


class NeutralCameraEvidenceTest(unittest.TestCase):
    def test_research_camera_labels_are_preserved_without_vehicle_or_brand_invention(self) -> None:
        paths = ProjectPaths(Path("/tmp") / "neutral-camera")
        media = _media(paths)
        shot = _shot()
        shot.shot_scale = "wide side profile"
        shot.camera_angle = "front passenger-side angle"
        shot.camera_motion = "vehicle-mounted vibration"
        shot.composition = "Toyota logo on left, Super Bowl mark on right"

        rendered = _camera_text(shot, "zh", include_composition=True)

        self.assertEqual(
            "wide side profile，front passenger-side angle，vehicle-mounted vibration，Toyota logo on left, Super Bowl mark on right",
            rendered,
        )
        for invented in ("全车", "副驾", "车载", "丰田", "超级碗"):
            self.assertNotIn(invented, rendered)
        self.assertEqual("", _camera_text(_shot(), "zh"))

        shot.story_beat = "product_title"
        shot.scene_type = "brand_payoff"
        enforce_profile_output_boundary(media, [shot])
        self.assertEqual("heuristic_unverified:opening_sequence", shot.story_beat)
        self.assertEqual(shot.story_beat, shot.scene_type)


if __name__ == "__main__":
    unittest.main()
