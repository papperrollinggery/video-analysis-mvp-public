from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from video_analysis_mvp.benchmark import (
    _generate_fixture,
    benchmark_case_definitions,
    benchmark_summary,
    boundary_metrics,
    quality_gate,
    run_audio_quality_benchmark,
)


class SyntheticBenchmarkContractTest(unittest.TestCase):
    def test_contract_has_six_distinct_maturity_cases(self) -> None:
        cases = benchmark_case_definitions()
        self.assertEqual(6, len(cases))
        self.assertEqual(6, len({case["id"] for case in cases}))
        self.assertEqual(
            {
                "hard_cuts",
                "fade_dissolve",
                "animation",
                "vertical_video",
                "variable_frame_rate",
                "speech_heavy_surrogate",
            },
            {case["category"] for case in cases},
        )

    def test_boundary_metrics_use_one_to_one_tolerance_matching(self) -> None:
        metrics = boundary_metrics([1.0, 2.0], [0.9, 1.1, 2.2], tolerance=0.25)
        self.assertEqual(2, metrics["matched"])
        self.assertEqual(0.667, metrics["precision"])
        self.assertEqual(1.0, metrics["recall"])
        self.assertEqual(0.8, metrics["f1"])
        self.assertEqual(0.15, metrics["mean_absolute_error_seconds"])

    def test_boundary_exactly_at_tolerance_is_a_match(self) -> None:
        metrics = boundary_metrics([1.0], [1.6], tolerance=0.6)
        self.assertEqual(1, metrics["matched"])
        self.assertEqual(1.0, metrics["recall"])

    def test_no_cut_case_has_perfect_score_when_detector_adds_no_boundary(self) -> None:
        metrics = boundary_metrics([], [])
        self.assertEqual(1.0, metrics["precision"])
        self.assertEqual(1.0, metrics["recall"])

    def test_hard_cut_quality_gate_rejects_low_precision(self) -> None:
        gate = quality_gate("hard_cuts", boundary_metrics([1.0], [1.0, 2.0]))
        self.assertEqual("accuracy", gate["mode"])
        self.assertIs(gate["passed"], False)

    def test_no_cut_quality_gate_rejects_a_false_positive(self) -> None:
        gate = quality_gate("animation", boundary_metrics([], [1.0]))
        self.assertEqual("accuracy", gate["mode"])
        self.assertIs(gate["passed"], False)

    def test_fade_case_is_observational_and_cannot_count_as_passed(self) -> None:
        gate = quality_gate("fade_dissolve", boundary_metrics([1.0], [1.0]))
        self.assertEqual("observational", gate["mode"])
        self.assertIsNone(gate["passed"])

    def test_overall_summary_requires_every_functional_and_accuracy_gate(self) -> None:
        results = [
            {
                "functional_passed": True,
                "quality_gate": {"mode": "accuracy"},
                "performance_gate": {"passed": True},
                "passed": True,
                "status": "passed",
            }
            for _ in range(5)
        ]
        results.append(
            {
                "functional_passed": True,
                "quality_gate": {"mode": "observational"},
                "performance_gate": {"passed": True},
                "passed": None,
                "status": "observational",
            }
        )
        passed, summary = benchmark_summary(results)
        self.assertIs(passed, True)
        self.assertEqual(6, summary["functional_passed"])
        self.assertEqual(5, summary["accuracy_gated_passed"])
        self.assertEqual(1, summary["observational"])

        results[0]["passed"] = False
        results[0]["status"] = "failed"
        passed, summary = benchmark_summary(results)
        self.assertIs(passed, False)
        self.assertEqual(4, summary["accuracy_gated_passed"])
        self.assertEqual(1, summary["failed"])

        results[0]["passed"] = True
        results[0]["status"] = "passed"
        results[0]["performance_gate"] = {"passed": False}
        passed, summary = benchmark_summary(results)
        self.assertIs(passed, False)
        self.assertEqual(5, summary["performance_passed"])

    def test_audio_quality_benchmark_is_generated_bounded_and_honest(self) -> None:
        with tempfile.TemporaryDirectory(prefix="audio-benchmark-") as directory:
            root = Path(directory)
            result = run_audio_quality_benchmark(root)
            retained = list(root.glob("*.wav"))

        self.assertTrue(result["passed"], result)
        self.assertEqual(5, result["case_count"])
        self.assertEqual(5, result["passed_case_count"])
        self.assertLessEqual(result["elapsed_seconds"], result["maximum_elapsed_seconds"])
        self.assertEqual("not_run", result["asr_accuracy"]["status"])
        self.assertEqual("not_run", result["semantic_identity_accuracy"]["status"])
        self.assertEqual([], retained)

    def test_fixture_generation_rejects_a_symlink_destination(self) -> None:
        with tempfile.TemporaryDirectory(prefix="benchmark-symlink-") as directory:
            root = Path(directory)
            outside = root / "outside.mp4"
            outside.write_bytes(b"unchanged")
            destination = root / "hard-cuts-landscape.mp4"
            destination.symlink_to(outside)
            with patch("video_analysis_mvp.benchmark.require_tool", return_value="ffmpeg"), patch(
                "video_analysis_mvp.benchmark.run_command"
            ) as runner, self.assertRaises(ValueError):
                _generate_fixture(benchmark_case_definitions()[0], destination)
            self.assertEqual(b"unchanged", outside.read_bytes())
            runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
