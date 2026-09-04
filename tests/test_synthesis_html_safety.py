from __future__ import annotations

import html
import tempfile
import unittest
from pathlib import Path

from video_analysis_mvp.schemas import (
    AnalysisProfile,
    AnalysisReport,
    CanonicalMediaPackage,
    Shot,
    SourceType,
)
from video_analysis_mvp.synthesis import _storyboard_row, render_html_report


STORED_XSS = '<img src=x onerror="window.__stored_xss__=1">'
TAG_BREAKOUT = "</td><script>window.__stored_xss__=1</script><td>"


def _shot(**updates: object) -> Shot:
    values: dict[str, object] = {
        "scene_no": "001",
        "shot_id": "shot-001",
        "shot_no": 1,
        "setup_id": "A",
        "start_time": 0.0,
        "end_time": 1.0,
        "duration": 1.0,
        "timecode": "00:00-00:01",
        "frame_ref": "frame-0001.jpg",
        "content_summary": "A safe frame",
        "scene_type": "hook",
        "shot_scale": "wide",
        "camera_angle": "eye-level",
        "camera_motion": "static",
        "composition": "center-weighted",
        "dialogue": "Safe dialogue",
        "sound_sync": "sync",
        "audio_notes": "Safe audio notes",
        "music_state": "medium",
        "beat_density": 0.5,
        "rhythm_notes": "moderate rhythm activity",
        "prompt_en": "Safe prompt",
        "prompt_zh": "安全提示词",
        "review_notes": "Safe review notes",
        "style_notes": "Safe style notes",
    }
    values.update(updates)
    return Shot(**values)


class SynthesisHtmlSafetyTest(unittest.TestCase):
    def test_storyboard_row_escapes_each_dynamic_text_field(self) -> None:
        cases = (
            ("scene_no", {}, False),
            ("setup_id", {}, False),
            ("timecode", {}, False),
            ("frame_ref", {}, False),
            ("content_summary", {}, False),
            ("visual_description", {"content_summary": ""}, False),
            ("scene_type", {}, False),
            ("shot_scale", {}, False),
            ("camera_angle", {}, False),
            ("camera_motion", {}, False),
            ("composition", {}, False),
            ("dialogue", {}, False),
            ("speech_summary", {"dialogue": ""}, False),
            ("sound_sync", {}, False),
            ("audio_notes", {}, False),
            ("music_state", {}, False),
            ("rhythm_notes", {}, False),
            ("prompt_en", {}, False),
            ("prompt_zh", {}, True),
            ("review_notes", {}, False),
            ("style_notes", {}, False),
            ("continuity_notes", {"style_notes": ""}, False),
        )

        for field, prerequisites, zh in cases:
            with self.subTest(field=field, zh=zh):
                row = _storyboard_row(_shot(**prerequisites, **{field: STORED_XSS}), zh=zh)
                self.assertNotIn(STORED_XSS, row)
                self.assertIn(html.escape(STORED_XSS), row)

        safe_row = _storyboard_row(_shot(), zh=False)
        self.assertIn("<br><span class='small'>", safe_row)
        self.assertIn("<img class='thumb'", safe_row)

    def test_human_or_provider_silence_does_not_display_machine_speech_summary(self) -> None:
        for annotation_source in ("human", "openai"):
            with self.subTest(annotation_source=annotation_source):
                row = _storyboard_row(
                    _shot(
                        annotation_source=annotation_source,
                        dialogue="",
                        speech_summary="machine transcript",
                    ),
                    zh=False,
                )
                self.assertNotIn("machine transcript", row)

        machine_row = _storyboard_row(
            _shot(annotation_source="machine", dialogue="", speech_summary="machine transcript"),
            zh=False,
        )
        self.assertIn("machine transcript", machine_row)

    def test_rendered_report_does_not_emit_stored_model_html(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            report_path = root / "reports" / "report.html"
            shot = _shot(
                content_summary=TAG_BREAKOUT,
                dialogue=TAG_BREAKOUT,
                prompt_en=TAG_BREAKOUT,
                review_notes=TAG_BREAKOUT,
            )
            media = CanonicalMediaPackage(
                project_id="safe-project",
                source_type=SourceType.file,
                source="source.mp4",
                local_master_path="master.mp4",
                review_copy_path="review.mp4",
                audio_path="audio.wav",
                duration_seconds=1.0,
                frame_rate=24.0,
                resolution="1920x1080",
                aspect_ratio=16 / 9,
                status="analyzed",
                analysis_profile=AnalysisProfile.ads,
            )
            report = AnalysisReport(
                project_id="safe-project",
                profile=AnalysisProfile.ads,
                summary="Safe summary",
                technical={"duration": "00:01", "resolution": "1920x1080"},
                visual_observations=[],
                audio_observations=[],
                rhythm_observations=[],
                client_takeaways=[],
                artifacts={"contact_sheet": str(root / "assets" / "contact_sheet.jpg")},
            )

            render_html_report(report, media, [shot], [], [], [], [], report_path)

            rendered = report_path.read_text(encoding="utf-8")
            self.assertNotIn(TAG_BREAKOUT, rendered)
            self.assertIn(html.escape(TAG_BREAKOUT), rendered)


if __name__ == "__main__":
    unittest.main()
