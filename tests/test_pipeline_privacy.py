from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from video_analysis_mvp.paths import ProjectPaths
from video_analysis_mvp.pipeline import run_full_pipeline
from video_analysis_mvp.workspace_api import ApiError, create_project_from_intake
from video_analysis_mvp.schemas import AnalysisProfile, CanonicalMediaPackage, SourceType, StatusEnvelope


class PipelineProviderConsentTest(unittest.TestCase):
    def test_string_false_cannot_opt_in_to_external_vision_from_json_api(self) -> None:
        result = StatusEnvelope(status="success", summary="stub", artifacts={})
        with tempfile.TemporaryDirectory() as directory, patch(
            "video_analysis_mvp.pipeline.run_full_pipeline",
            return_value=result,
        ) as run:
            fixture = Path(directory) / "fixture.mp4"
            fixture.write_bytes(b"fixture")
            with self.assertRaisesRegex(ApiError, "skip_asr must be a JSON boolean"):
                create_project_from_intake(
                    Path(directory),
                    {"source": str(fixture), "with_vision": "false", "skip_asr": "false"},
                )

        run.assert_not_called()

    def test_only_json_true_opts_in_to_external_vision(self) -> None:
        result = StatusEnvelope(status="success", summary="stub", artifacts={})
        with tempfile.TemporaryDirectory() as directory, patch(
            "video_analysis_mvp.pipeline.run_full_pipeline",
            return_value=result,
        ) as run:
            fixture = Path(directory) / "fixture.mp4"
            fixture.write_bytes(b"fixture")
            create_project_from_intake(Path(directory), {"source": str(fixture), "with_vision": True})

        self.assertTrue(run.call_args.kwargs["with_vision"])

    def test_explicit_non_boolean_flags_never_dispatch_pipeline(self) -> None:
        result = StatusEnvelope(status="success", summary="stub", artifacts={})
        for field in ("with_vision", "skip_asr"):
            for invalid in ("false", 0, None):
                with self.subTest(field=field, invalid=invalid), tempfile.TemporaryDirectory() as directory, patch(
                    "video_analysis_mvp.pipeline.run_full_pipeline",
                    return_value=result,
                ) as run:
                    fixture = Path(directory) / "fixture.mp4"
                    fixture.write_bytes(b"fixture")
                    with self.assertRaisesRegex(ApiError, f"{field} must be a JSON boolean"):
                        create_project_from_intake(Path(directory), {"source": str(fixture), field: invalid})
                    run.assert_not_called()

    def test_full_pipeline_never_calls_provider_without_explicit_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory) / "consent-test")
            paths.ensure()
            media = CanonicalMediaPackage(
                project_id="consent-test",
                source_type=SourceType.file,
                source="source.mp4",
                local_master_path=str(paths.ingest / "master.mp4"),
                review_copy_path=str(paths.assets / "review.mp4"),
                audio_path=str(paths.assets / "audio.wav"),
                duration_seconds=1.0,
                frame_rate=24.0,
                resolution="320x180",
                aspect_ratio=16 / 9,
                status="analyzed",
                analysis_profile=AnalysisProfile.research,
            )
            with (
                patch("video_analysis_mvp.pipeline.new_project_paths", return_value=paths),
                patch("video_analysis_mvp.pipeline.ingest_source", return_value=media),
                patch("video_analysis_mvp.pipeline.set_delivery_language"),
                patch("video_analysis_mvp.pipeline.analyze_visual"),
                patch("video_analysis_mvp.pipeline.analyze_audio"),
                patch("video_analysis_mvp.pipeline.audio_intelligence_binding", return_value={"capabilities": {"asr": {"status": "skipped", "reason": "test-only skip"}}}),
                patch("video_analysis_mvp.pipeline.synthesize", return_value=SimpleNamespace(artifacts={})),
                patch(
                    "video_analysis_mvp.pipeline.annotate_project_with_vision",
                    return_value=StatusEnvelope(status="success", summary="annotated"),
                ) as provider,
            ):
                result = run_full_pipeline("source.mp4")
                self.assertEqual("success", result.status)
                provider.assert_not_called()

                result = run_full_pipeline("source.mp4", with_vision=True)
                self.assertEqual("success", result.status)
                provider.assert_called_once_with(paths)


if __name__ == "__main__":
    unittest.main()
