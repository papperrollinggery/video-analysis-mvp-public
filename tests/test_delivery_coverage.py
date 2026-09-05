from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from video_analysis_mvp.delivery import (
    build_prompt_adapter,
    write_model_prompt_pack,
    write_prompt_reverse_engineering,
    write_remake_brief,
)
from video_analysis_mvp.schemas import (
    AnalysisProfile,
    AnalysisReport,
    CanonicalMediaPackage,
    Shot,
    SourceType,
)


def _media() -> CanonicalMediaPackage:
    return CanonicalMediaPackage(
        project_id="delivery-coverage",
        source_type=SourceType.file,
        source="source.mp4",
        local_master_path="master.mp4",
        review_copy_path="review.mp4",
        audio_path="audio.wav",
        duration_seconds=57.0,
        frame_rate=24.0,
        resolution="1920x1080",
        aspect_ratio=16 / 9,
        status="analyzed",
        analysis_profile=AnalysisProfile.ads,
        metadata={"delivery_language": "en"},
    )


def _shot(number: int) -> Shot:
    start = (number - 1) * 3
    return Shot(
        shot_id=f"shot_{number:04d}",
        shot_no=number,
        start_time=float(start),
        end_time=float(start + 3),
        duration=3.0,
        timecode=f"00:{start:02d}-00:{start + 3:02d}",
        primary_frame_ref=f"shot_{number:04d}.jpg",
        content_summary=f"specific observed fact {number}",
        subject=f"subject {number}",
        action=f"specific action {number}",
        shot_scale="medium",
        camera_angle="eye-level",
        camera_motion="static",
        composition=f"composition {number}",
        style_notes=f"style fact {number}",
        remake_notes=f"control fact {number}",
    )


def _unknown_shot() -> Shot:
    return Shot(
        shot_id="shot_unknown",
        shot_no=1,
        start_time=0.0,
        end_time=3.0,
        duration=3.0,
        timecode="00:00-00:03",
        primary_frame_ref="unknown.jpg",
    )


class DeliveryCoverageTest(unittest.TestCase):
    def test_nineteen_shots_are_covered_without_repeating_shared_adapter_templates(self) -> None:
        media = _media()
        shots = [_shot(number) for number in range(1, 20)]
        report = AnalysisReport(
            project_id=media.project_id,
            profile=AnalysisProfile.ads,
            summary="",
            technical={},
            visual_observations=[],
            audio_observations=[],
            rhythm_observations=[],
            client_takeaways=[],
            artifacts={},
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remake_path = root / "remake.md"
            reverse_path = root / "reverse.md"
            pack_path = root / "pack.json"
            write_remake_brief(report, media, shots, [], [], remake_path)
            write_prompt_reverse_engineering(media, shots, reverse_path)
            write_model_prompt_pack(media, shots, pack_path)
            remake = remake_path.read_text(encoding="utf-8")
            reverse = reverse_path.read_text(encoding="utf-8")
            pack = json.loads(pack_path.read_text(encoding="utf-8"))

        for number in range(1, 20):
            self.assertIn(f"specific observed fact {number}", remake)
            self.assertIn(f"specific action {number}", remake)
            self.assertIn(f"specific observed fact {number}", reverse)
            self.assertIn(f"specific action {number}", reverse)
            self.assertIn(f"control fact {number}", reverse)
        self.assertEqual(19, reverse.count("#### Shot-Specific Prompt"))
        self.assertEqual(1, reverse.count("wrong logos"))
        self.assertNotIn("#### Universal Text-to-Video", reverse)
        self.assertNotIn("#### Strict JSON Template", reverse)
        self.assertEqual(19, len(pack["shots"]))

    def test_unknown_input_defers_hook_and_style_recommendations(self) -> None:
        media = _media()
        shot = _unknown_shot()
        report = AnalysisReport(
            project_id=media.project_id,
            profile=AnalysisProfile.ads,
            summary="",
            technical={},
            visual_observations=[],
            audio_observations=[],
            rhythm_observations=[],
            client_takeaways=[],
            artifacts={},
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "remake.md"
            write_remake_brief(report, media, [shot], [], [], path)
            text = path.read_text(encoding="utf-8")

        self.assertIn("Branch Decision Pending Evidence Review", text)
        self.assertNotIn("premium_style", text)
        self.assertNotIn("first 3 seconds", text)
        self.assertIn("does not infer a claim from missing annotations", text)

    def test_prompt_adapter_keeps_its_public_model_blocks(self) -> None:
        adapter = build_prompt_adapter(_shot(1), _media())
        self.assertEqual(
            {
                "universal_text_to_video",
                "image_to_video",
                "runway_gen_style",
                "kling_style_json",
                "veo_sora_narrative",
                "luma_pika_edit",
            },
            {key for key in adapter if key.endswith(("video", "style", "json", "narrative", "edit"))},
        )


if __name__ == "__main__":
    unittest.main()
