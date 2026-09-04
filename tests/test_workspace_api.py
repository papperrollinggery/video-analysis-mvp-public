from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from video_analysis_mvp.audio import _stage_and_commit_audio_generation
from video_analysis_mvp.paths import ProjectPaths
from video_analysis_mvp.schemas import AnalysisProfile, CanonicalMediaPackage, Shot, SourceType, dump_json
from video_analysis_mvp.synthesis import _commit_report_generation
from video_analysis_mvp.visual import _build_visual_generation_receipt
from video_analysis_mvp.workspace_api import (
    _api_readiness,
    _shot_boundary_payload,
    deliverables_payload,
    workspace_snapshot_payload,
)


class ShotBoundaryProvenanceTest(unittest.TestCase):
    def payload(self, **overrides: object) -> dict[str, object]:
        readiness_result = overrides.pop("readiness_result", None)
        values: dict[str, object] = {
            "shot_id": "shot_0001",
            "shot_no": 1,
            "start_time": 0.0,
            "end_time": 1.0,
            "duration": 1.0,
            "story_beat": "observation",
            "annotation_source": "machine",
            "readiness_status": "blocked",
            "readiness_reasons": ["verified annotation provenance required"],
        }
        values.update(overrides)
        shot = Shot.model_validate(values)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            return _shot_boundary_payload(
                root,
                project,
                shot,
                readiness_result=readiness_result if isinstance(readiness_result, dict) else None,
            )

    def test_payload_exposes_per_shot_annotation_receipt(self) -> None:
        payload = self.payload(story_beat="heuristic_unverified:opening_sequence")

        self.assertEqual("machine", payload["annotation_source"])
        self.assertEqual("unverified", payload["annotation_verification"])
        self.assertEqual(
            ["verified annotation provenance required"],
            payload["readiness_reasons"],
        )

    def test_current_readiness_distinguishes_provider_receipt_human_review_and_unverified(self) -> None:
        reviewed = self.payload(
            annotation_source="human",
            readiness_status="ready",
            readiness_reasons=[],
            readiness_result={"annotation_state": "human_assertion", "human_assertion": True},
        )
        heuristic = self.payload(
            annotation_source="human",
            readiness_status="ready",
            readiness_reasons=[],
            story_beat="heuristic_unverified:opening_sequence",
            readiness_result={"annotation_state": "unverified", "human_assertion": False},
        )
        provider = self.payload(
            annotation_source="openai",
            readiness_status="ready",
            readiness_reasons=[],
            readiness_result={
                "annotation_state": "provider_receipt_verified",
                "provider_receipt_verified": True,
            },
        )

        self.assertEqual("human_reviewed", reviewed["annotation_verification"])
        self.assertEqual("unverified", heuristic["annotation_verification"])
        self.assertEqual("provider_receipt_verified", provider["annotation_verification"])

    def test_english_evidence_is_preferred_while_original_zh_evidence_remains_a_fallback(self) -> None:
        english = self.payload(
            story_beat="product_reveal",
            content_summary="English visual observation",
            content_summary_zh="中文画面观察",
            direction_notes="English interpretation",
            direction_notes_zh="中文解读",
            shot_scale="wide shot",
        )
        zh_fallback = self.payload(
            content_summary="",
            content_summary_zh="原始中文证据",
            direction_notes="",
            direction_notes_zh="原始中文解读",
        )

        self.assertEqual("Product reveal", english["story_beat"])
        self.assertEqual("English visual observation", english["visual_content"])
        self.assertEqual("English interpretation", english["meaning"])
        self.assertEqual("Wide", english["shot_size"])
        self.assertEqual("原始中文证据", zh_fallback["visual_content"])
        self.assertEqual("原始中文解读", zh_fallback["meaning"])

    def test_workspace_api_has_no_fixed_chinese_ui_copy(self) -> None:
        source = (Path(__file__).parents[1] / "src" / "video_analysis_mvp" / "workspace_api.py").read_text(
            encoding="utf-8"
        )
        self.assertNotRegex(source, r"[\u4e00-\u9fff]")

    def test_generic_payload_preserves_observations_without_ad_or_vehicle_inference(self) -> None:
        payload = self.payload(
            camera_angle="side view",
            sound_design="music",
            audio_notes="No transcript available",
            direction_notes="",
            direction_notes_zh="",
            rhythm_notes="pending audio sync",
            content_summary="",
            content_summary_zh="",
            visual_description="",
            action="",
            action_zh="",
        )

        self.assertEqual("side view", payload["angle"])
        self.assertEqual("music", payload["sound"])
        self.assertIsNone(payload["visual_content"])
        self.assertIsNone(payload["meaning"])
        self.assertIsNone(payload["rhythm"])
        combined = str(payload).lower()
        for invented in ("engine", "vehicle", "car", "前三秒", "产品能力", "引擎"):
            self.assertNotIn(invented, combined)

    def test_media_readiness_requires_bound_media_and_current_frame_results(self) -> None:
        base = {
            "status": "blocked",
            "score": 0.99,
            "shot_count": 1,
            "shot_results": [{"shot_id": "shot_0001", "reasons": []}],
        }
        unbound = _api_readiness({**base, "media_binding": {"status": "unbound"}})
        bound = _api_readiness({**base, "media_binding": {"status": "bound"}})
        invalid_frame = _api_readiness(
            {
                **base,
                "media_binding": {"status": "bound"},
                "shot_results": [
                    {"shot_id": "shot_0001", "reasons": ["frame reference is missing or unsafe: frame.jpg"]}
                ],
            }
        )

        self.assertEqual("blocked", unbound["checks"][0]["status"])
        self.assertEqual("ready", bound["checks"][0]["status"])
        self.assertEqual("blocked", invalid_frame["checks"][0]["status"])

    def test_blocked_sensitive_draft_wins_and_present_files_are_only_available(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "ads-project"
            reports = project / "reports"
            data = project / "data"
            reports.mkdir(parents=True)
            data.mkdir()
            remake = reports / "remake_brief.md"
            storyboard = reports / "storyboard.html"
            remake.write_text("draft", encoding="utf-8")
            storyboard.write_text("evidence", encoding="utf-8")
            media = CanonicalMediaPackage(
                project_id=project.name,
                source_type=SourceType.file,
                source="source.mp4",
                local_master_path=str(project / "ingest" / "master.mp4"),
                review_copy_path=str(project / "assets" / "review.mp4"),
                audio_path=str(project / "assets" / "audio.wav"),
                duration_seconds=1.0,
                frame_rate=24.0,
                resolution="1920x1080",
                aspect_ratio=16 / 9,
                status="analyzed",
                analysis_profile=AnalysisProfile.ads,
            )
            paths = ProjectPaths(project)
            paths.ensure()
            dump_json(paths.data / "media_package.json", media)
            shots: list[Shot] = []
            dump_json(paths.data / "shots.json", shots)
            dump_json(paths.data / "scenes.json", [])
            (paths.assets / "contact_sheet.jpg").write_bytes(b"contact")
            dump_json(
                paths.data / "visual_generation.json",
                _build_visual_generation_receipt(paths, shots, []),
            )
            _stage_and_commit_audio_generation(paths, [], [], [])
            generation_id = str(uuid.uuid4())
            _commit_report_generation(
                paths,
                media,
                generation_id,
                {
                    "remake_brief": str(remake),
                    "storyboard_html": str(storyboard),
                    "project_manifest": str(project / "project_manifest.json"),
                },
            )
            with patch(
                "video_analysis_mvp.workspace_api.readiness_payload",
                return_value={"professional_export_allowed": False, "reasons": ["blocked"]},
            ):
                artifacts = {
                    item["id"]: item for item in deliverables_payload(root, project)["artifacts"]
                }

            self.assertEqual("blocked", artifacts["remake_brief"]["readiness_status"])
            self.assertIsNone(artifacts["remake_brief"]["url"])
            self.assertEqual("available", artifacts["storyboard_html"]["readiness_status"])
            self.assertIsNotNone(artifacts["storyboard_html"]["url"])

            snapshot = workspace_snapshot_payload(root, project)
            self.assertEqual(generation_id, snapshot["generation_id"])
            self.assertRegex(str(snapshot["snapshot_id"]), r"^sha256:[0-9a-f]{64}$")

            changed_media = media.model_copy(update={"resolution": "1280x720"})
            dump_json(paths.data / "media_package.json", changed_media)
            stale = workspace_snapshot_payload(root, project)
            self.assertIsNone(stale["generation_id"])
            self.assertNotIn(
                "storyboard_html",
                {item["id"] for item in stale["deliverables"]["artifacts"]},
            )


if __name__ == "__main__":
    unittest.main()
