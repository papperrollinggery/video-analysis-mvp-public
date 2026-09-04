from __future__ import annotations

import contextlib
import base64
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from video_analysis_mvp.cli import main
from video_analysis_mvp.paths import ProjectPaths
from video_analysis_mvp.schemas import Shot, StatusEnvelope, dump_json, load_json
from video_analysis_mvp.vision import (
    OBSERVATION_FIELDS,
    annotate_project_with_minimax_mcp,
    annotate_project_with_vision,
    canonical_shot_digest,
)


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _payload(summary: str, confidence: float) -> dict[str, object]:
    payload: dict[str, object] = {field: "none" for field in OBSERVATION_FIELDS}
    payload["content_summary"] = summary
    payload["confidence"] = confidence
    return payload


def _shot(number: int, frame_ref: str) -> Shot:
    return Shot(
        shot_id=f"shot_{number:04d}",
        shot_no=number,
        start_time=float(number - 1),
        end_time=float(number),
        duration=1.0,
        frame_ref=frame_ref,
        primary_frame_ref=frame_ref,
        frame_refs=[frame_ref] if frame_ref else [],
        content_summary=f"original-{number}",
    )


class VisionAnnotationReceiptTest(unittest.TestCase):
    def _project(self, root: Path, shots: list[Shot]) -> ProjectPaths:
        paths = ProjectPaths(root / "project")
        paths.ensure()
        dump_json(paths.data / "shots.json", shots)
        return paths

    def _run(
        self,
        provider: str,
        paths: ProjectPaths,
        analyzer: Mock,
        limit: int | None = None,
    ) -> StatusEnvelope:
        environment = (
            {"OPENAI_API_KEY": "test-openai-key"}
            if provider == "openai"
            else {"MINIMAX_API_KEY": "test-minimax-key"}
        )
        target = (
            "video_analysis_mvp.vision.analyze_frame"
            if provider == "openai"
            else "video_analysis_mvp.vision.analyze_frame_with_minimax_mcp"
        )
        with (
            patch.dict(os.environ, environment, clear=True),
            patch(target, analyzer),
            patch("video_analysis_mvp.vision._prepare_minimax_mcp", return_value=("/tools/minimax", "0.0.4")),
        ):
            if provider == "openai":
                return annotate_project_with_vision(paths, provider="openai", limit=limit)
            return annotate_project_with_minimax_mcp(paths, limit=limit)

    def _invalid_frame_receipt(self, provider: str) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside_absolute = root / "outside-absolute.jpg"
            outside_absolute.write_bytes(b"outside")
            paths = self._project(root, [])
            (paths.keyframes / "directory").mkdir()
            (paths.assets / "outside-traversal.jpg").write_bytes(b"outside")
            (paths.keyframes / "escape.jpg").symlink_to(outside_absolute)
            shots = [
                _shot(1, ""),
                _shot(2, "directory"),
                _shot(3, "missing.jpg"),
                _shot(4, "../outside-traversal.jpg"),
                _shot(5, str(outside_absolute)),
                _shot(6, "escape.jpg"),
            ]
            dump_json(paths.data / "shots.json", shots)
            analyzer = Mock()

            result = self._run(provider, paths, analyzer)

            label = "OpenAI vision" if provider == "openai" else "MiniMax MCP vision"
            self.assertEqual("warning", result.status)
            self.assertEqual(f"{label}: annotated 0 of 6 selected shots; skipped 6.", result.summary)
            self.assertEqual(
                [
                    "shot_0001: skipped — frame_ref is empty",
                    "shot_0002: skipped — frame_ref is not a regular file",
                    "shot_0003: skipped — frame file is missing",
                    "shot_0004: skipped — frame_ref escapes the keyframes directory",
                    "shot_0005: skipped — frame_ref escapes the keyframes directory",
                    "shot_0006: skipped — frame_ref cannot be opened without following symlinks",
                ],
                result.diagnostics,
            )
            analyzer.assert_not_called()
            self.assertEqual([], load_json(paths.data / "vision_annotations.json")["annotations"])
            persisted = load_json(paths.data / "shots.json")
            self.assertEqual([shot.shot_id for shot in shots], [item["shot_id"] for item in persisted])
            self.assertEqual([shot.frame_ref for shot in shots], [item["frame_ref"] for item in persisted])

    def _partial_receipt(self, provider: str) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shots = [_shot(1, "one.jpg"), _shot(2, "two.jpg")]
            paths = self._project(root, shots)
            (paths.keyframes / "one.jpg").write_bytes(PNG_1X1)
            (paths.keyframes / "two.jpg").write_bytes(PNG_1X1)
            analyzer = Mock(
                side_effect=[
                    _payload("provider-success", 0.9),
                    RuntimeError("upstream unavailable"),
                ]
            )

            result = self._run(provider, paths, analyzer)

            label = "OpenAI vision" if provider == "openai" else "MiniMax MCP vision"
            self.assertEqual("warning", result.status)
            self.assertEqual(f"{label}: annotated 1 of 2 selected shots; skipped 1.", result.summary)
            self.assertEqual(
                ["shot_0002: skipped — provider analysis failed (RuntimeError)"],
                result.diagnostics,
            )
            self.assertEqual(2, analyzer.call_count)
            annotations = load_json(paths.data / "vision_annotations.json")["annotations"]
            self.assertEqual(["shot_0001"], [item["shot_id"] for item in annotations])
            persisted = load_json(paths.data / "shots.json")
            self.assertEqual(2, len(persisted))
            self.assertEqual("provider-success", persisted[0]["content_summary"])
            self.assertEqual("original-2", persisted[1]["content_summary"])

    def _full_receipt(self, provider: str) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shots = [_shot(1, "one.jpg"), _shot(2, "two.jpg")]
            paths = self._project(root, shots)
            (paths.keyframes / "one.jpg").write_bytes(PNG_1X1)
            (paths.keyframes / "two.jpg").write_bytes(PNG_1X1)
            analyzer = Mock(
                side_effect=[
                    _payload("provider-one", 0.9),
                    _payload("provider-two", 0.8),
                ]
            )

            result = self._run(provider, paths, analyzer)

            label = "OpenAI vision" if provider == "openai" else "MiniMax MCP vision"
            self.assertEqual("success", result.status)
            self.assertEqual(f"{label}: annotated 2 of 2 selected shots; skipped 0.", result.summary)
            self.assertEqual([], result.diagnostics)
            self.assertEqual(2, analyzer.call_count)
            annotations = load_json(paths.data / "vision_annotations.json")["annotations"]
            self.assertEqual(["shot_0001", "shot_0002"], [item["shot_id"] for item in annotations])
            receipt = load_json(paths.data / "vision_annotations.json")
            expected_provider = "openai" if provider == "openai" else "minimax_mcp"
            expected_source = (
                "openai_chat_completions"
                if provider == "openai"
                else "minimax_mcp_understand_image"
            )
            self.assertEqual(expected_provider, receipt["provider"])
            self.assertEqual(expected_source, receipt["provider_source"])
            self.assertEqual(
                [canonical_shot_digest(item) for item in annotations],
                [item["shot_sha256"] for item in receipt["shot_receipts"]],
            )
            self.assertTrue(all(item["annotation_source"] == expected_provider for item in annotations))

    def test_openai_invalid_frames_are_all_reported_as_skipped(self) -> None:
        self._invalid_frame_receipt("openai")

    def test_minimax_invalid_frames_are_all_reported_as_skipped(self) -> None:
        self._invalid_frame_receipt("minimax")

    def test_openai_partial_success_uses_actual_annotation_count(self) -> None:
        self._partial_receipt("openai")

    def test_minimax_partial_success_uses_actual_annotation_count(self) -> None:
        self._partial_receipt("minimax")

    def test_openai_full_success_requires_every_selected_shot(self) -> None:
        self._full_receipt("openai")

    def test_minimax_full_success_requires_every_selected_shot(self) -> None:
        self._full_receipt("minimax")

    def test_provider_boundaries_reject_non_positive_limits_before_analysis(self) -> None:
        for provider in ("openai", "minimax"):
            for limit in (0, -1):
                with self.subTest(provider=provider, limit=limit), tempfile.TemporaryDirectory() as directory:
                    paths = self._project(Path(directory), [_shot(1, "one.jpg")])
                    (paths.keyframes / "one.jpg").write_bytes(PNG_1X1)
                    analyzer = Mock()

                    with self.assertRaisesRegex(
                        ValueError,
                        "Vision annotation limit must be greater than zero",
                    ):
                        self._run(provider, paths, analyzer, limit=limit)

                    analyzer.assert_not_called()
                    self.assertFalse((paths.data / "vision_annotations.json").exists())

    def test_cli_rejects_non_positive_limits_before_dispatch(self) -> None:
        for limit in (0, -1):
            with self.subTest(limit=limit):
                stderr = io.StringIO()
                with (
                    patch("video_analysis_mvp.cli.run_vision") as run_vision,
                    contextlib.redirect_stderr(stderr),
                    self.assertRaises(SystemExit) as raised,
                ):
                    main(["vision", "project", "--limit", str(limit)])

                self.assertEqual(2, raised.exception.code)
                self.assertIn("argument --limit: must be a positive integer", stderr.getvalue())
                run_vision.assert_not_called()


if __name__ == "__main__":
    unittest.main()
