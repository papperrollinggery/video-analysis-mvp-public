from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from video_analysis_mvp.doctor import run_doctor


class DoctorEnvelopeTest(unittest.TestCase):
    def test_success_uses_diagnostics_instead_of_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch("video_analysis_mvp.doctor.importlib.util.find_spec", return_value=object()),
                patch("video_analysis_mvp.doctor.require_tool", side_effect=lambda tool: f"/tools/{tool}"),
                patch("video_analysis_mvp.doctor.vision_provider_capability", return_value=(False, "not configured")),
                patch.dict("os.environ", {}, clear=True),
            ):
                result = run_doctor(workspace=str(Path(directory) / "workspace"))

        self.assertEqual("success", result.status)
        self.assertIsNone(result.error)
        self.assertIn("tool ffmpeg: /tools/ffmpeg", result.diagnostics)
        self.assertIn(
            "evidence readiness: export blocked until current complete provider annotation or all-shot human review and a current v3 readiness receipt",
            result.diagnostics,
        )
        self.assertIn(
            "background run limits: max 1 active per workspace; source bytes + 256 MiB free-space reserve",
            result.diagnostics,
        )
        self.assertIn(
            "subprocess controls: bounded combined output, hard timeout, and process-group cancellation",
            result.diagnostics,
        )

    def test_configured_key_is_capability_only_and_does_not_claim_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch("video_analysis_mvp.doctor.importlib.util.find_spec", return_value=object()),
                patch("video_analysis_mvp.doctor.require_tool", side_effect=lambda tool: f"/tools/{tool}"),
                patch("video_analysis_mvp.doctor.vision_provider_capability", return_value=(True, "configured endpoint")),
                patch.dict("os.environ", {"OPENAI_API_KEY": "configured-only"}, clear=True),
            ):
                result = run_doctor(workspace=str(Path(directory) / "workspace"))

        receipt = "\n".join(result.diagnostics)
        self.assertIn("provider access configured", receipt)
        self.assertIn("export remains blocked", receipt)
        self.assertIn("complete provider annotation", receipt)
        self.assertNotIn("provider annotation complete", receipt)

    def test_current_provider_and_human_completion_are_reported_separately(self) -> None:
        cases = [
            ((["provider-project"], [], ["provider-project"]), "current provider-complete"),
            (([], ["human-project"], ["human-project"]), "current all-shot human review complete"),
        ]
        for projects, expected in cases:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as directory:
                with (
                    patch("video_analysis_mvp.doctor.importlib.util.find_spec", return_value=object()),
                    patch("video_analysis_mvp.doctor.require_tool", side_effect=lambda tool: f"/tools/{tool}"),
                    patch("video_analysis_mvp.doctor.vision_provider_capability", return_value=(False, "not configured")),
                    patch("video_analysis_mvp.doctor._readiness_projects", return_value=projects),
                    patch.dict("os.environ", {}, clear=True),
                ):
                    result = run_doctor(workspace=str(Path(directory) / "workspace"))
            receipt = "\n".join(result.diagnostics)
            self.assertIn(expected, receipt)
            self.assertIn("professional export allowed", receipt)

    def test_unknown_provider_diagnostic_never_claims_capability(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch("video_analysis_mvp.doctor.importlib.util.find_spec", return_value=object()),
                patch("video_analysis_mvp.doctor.require_tool", side_effect=lambda tool: f"/tools/{tool}"),
                patch.dict(
                    "os.environ",
                    {"VIDEO_ANALYSIS_VISION_PROVIDER": "unknown", "OPENAI_API_KEY": "ambient"},
                    clear=True,
                ),
            ):
                result = run_doctor(workspace=str(Path(directory) / "workspace"))
        receipt = "\n".join(result.diagnostics)
        self.assertIn("unsupported vision provider", receipt)
        self.assertNotIn("provider access configured", receipt)


if __name__ == "__main__":
    unittest.main()
