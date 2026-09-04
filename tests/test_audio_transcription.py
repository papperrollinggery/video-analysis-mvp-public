from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from video_analysis_mvp.audio_transcription import (
    _minimal_environment,
    transcribe_local,
)
from video_analysis_mvp.utils import ToolError, run_command


class LocalTranscriptionTest(unittest.TestCase):
    def _model(self, directory: Path, name: str = "model.pt") -> Path:
        path = directory / name
        path.write_bytes(b"local checkpoint")
        return path

    def test_no_model_is_unknown_and_skip_takes_precedence(self) -> None:
        audio = Path("/private/input.wav")

        unknown = transcribe_local(audio, duration=3.0)
        skipped = transcribe_local(audio, duration=3.0, skip=True)

        self.assertEqual("unknown", unknown.status)
        self.assertIsNone(unknown.model_sha256)
        self.assertEqual("skipped", skipped.status)

    def test_missing_whisper_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            model = self._model(Path(directory))
            with patch(
                "video_analysis_mvp.audio_transcription.shutil.which", return_value=None
            ):
                result = transcribe_local(
                    Path("/private/input.wav"), duration=3.0, model_path=str(model)
                )

        self.assertEqual("unknown", result.status)
        self.assertIsNone(result.model_sha256)

    def test_command_failure_does_not_leak_paths_or_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = self._model(root, "private-model.pt")
            audio = root / "private-audio.wav"
            audio.write_bytes(b"audio")
            with (
                patch(
                    "video_analysis_mvp.audio_transcription.shutil.which",
                    return_value="/bin/whisper",
                ),
                patch(
                    "video_analysis_mvp.audio_transcription.run_command",
                    side_effect=ToolError(f"secret stderr: {model}"),
                ),
            ):
                result = transcribe_local(audio, duration=3.0, model_path=str(model))

        self.assertEqual("failed", result.status)
        self.assertEqual("local whisper execution failed", result.reason)
        self.assertNotIn("private-model", result.reason or "")
        self.assertIsNotNone(result.model_sha256)

    def test_successful_empty_result_is_produced_with_isolated_environment(
        self,
    ) -> None:
        captured: dict[str, object] = {}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = self._model(root)
            audio = root / "take.wav"
            audio.write_bytes(b"audio")

            def fake_run(
                args: list[str], timeout: int, *, environment: dict[str, str]
            ) -> object:
                captured["args"] = args
                captured["timeout"] = timeout
                captured["environment"] = environment
                output_dir = Path(args[args.index("--output_dir") + 1])
                (output_dir / "take.json").write_text(
                    json.dumps({"language": "en", "segments": []}), encoding="utf-8"
                )
                return object()

            with (
                patch.dict(
                    os.environ,
                    {"ASR_SECRET": "do-not-copy", "HTTP_PROXY": "http://proxy"},
                    clear=False,
                ),
                patch(
                    "video_analysis_mvp.audio_transcription.shutil.which",
                    return_value="/bin/whisper",
                ),
                patch(
                    "video_analysis_mvp.audio_transcription.run_command",
                    side_effect=fake_run,
                ),
            ):
                result = transcribe_local(audio, duration=3.0, model_path=str(model))

        self.assertEqual("produced", result.status)
        self.assertEqual([], result.segments)
        self.assertEqual(300, captured["timeout"])
        args = captured["args"]
        self.assertEqual("/bin/whisper", args[0])
        self.assertIn(str(model), args)
        self.assertNotIn("turbo", args)
        self.assertEqual("cpu", args[args.index("--device") + 1])
        self.assertEqual("False", args[args.index("--fp16") + 1])
        self.assertEqual("4", args[args.index("--threads") + 1])
        environment = captured["environment"]
        self.assertNotIn("ASR_SECRET", environment)
        self.assertNotIn("HTTP_PROXY", environment)
        self.assertTrue(str(environment["HOME"]).startswith("/"))
        self.assertEqual(environment["HOME"], environment["TMPDIR"])
        self.assertEqual(environment["HOME"], environment["XDG_CACHE_HOME"])

    def test_rejects_invalid_segment_time_without_returning_partial_result(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = self._model(root)
            audio = root / "take.wav"
            audio.write_bytes(b"audio")

            def fake_run(
                args: list[str], timeout: int, *, environment: dict[str, str]
            ) -> object:
                output_dir = Path(args[args.index("--output_dir") + 1])
                (output_dir / "take.json").write_text(
                    '{"language":"en","segments":[{"start":2.5,"end":1,"text":"bad"}]}',
                    encoding="utf-8",
                )
                return object()

            with (
                patch(
                    "video_analysis_mvp.audio_transcription.shutil.which",
                    return_value="/bin/whisper",
                ),
                patch(
                    "video_analysis_mvp.audio_transcription.run_command",
                    side_effect=fake_run,
                ),
            ):
                result = transcribe_local(audio, duration=3.0, model_path=str(model))

        self.assertEqual("failed", result.status)
        self.assertEqual([], result.segments)
        self.assertEqual("local whisper output was invalid", result.reason)

    def test_rejects_symlink_model_and_does_not_execute(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = self._model(root)
            link = root / "link.pt"
            link.symlink_to(model)
            with (
                patch(
                    "video_analysis_mvp.audio_transcription.shutil.which",
                    return_value="/bin/whisper",
                ),
                patch("video_analysis_mvp.audio_transcription.run_command") as command,
            ):
                result = transcribe_local(
                    Path("/private/input.wav"), duration=3.0, model_path=str(link)
                )

        self.assertEqual("failed", result.status)
        self.assertEqual("local ASR model is unsafe", result.reason)
        command.assert_not_called()

    def test_rejects_relative_model_name_even_when_a_local_turbo_file_exists(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._model(root, "turbo")
            previous = Path.cwd()
            os.chdir(root)
            try:
                with (
                    patch(
                        "video_analysis_mvp.audio_transcription.shutil.which",
                        return_value="/bin/whisper",
                    ),
                    patch(
                        "video_analysis_mvp.audio_transcription.run_command"
                    ) as command,
                ):
                    result = transcribe_local(
                        Path("/private/input.wav"), duration=3.0, model_path="turbo"
                    )
            finally:
                os.chdir(previous)

        self.assertEqual("failed", result.status)
        self.assertEqual("local ASR model is unsafe", result.reason)
        command.assert_not_called()

    def test_rejects_model_below_a_symlinked_parent_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real = root / "real"
            real.mkdir()
            model = self._model(real)
            linked = root / "linked"
            linked.symlink_to(real, target_is_directory=True)
            path_through_link = linked / model.name
            with (
                patch(
                    "video_analysis_mvp.audio_transcription.shutil.which",
                    return_value="/bin/whisper",
                ),
                patch("video_analysis_mvp.audio_transcription.run_command") as command,
            ):
                result = transcribe_local(
                    Path("/private/input.wav"),
                    duration=3.0,
                    model_path=str(path_through_link),
                )

        self.assertEqual("failed", result.status)
        self.assertEqual("local ASR model is unsafe", result.reason)
        command.assert_not_called()

    def test_model_parent_traversal_cannot_hide_a_symlink_from_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            local = root / "local"
            local.mkdir()
            outside = root / "outside"
            outside.mkdir()
            (outside / "child").mkdir()
            self._model(local)
            (outside / "model.pt").write_bytes(b"different model")
            (local / "link").symlink_to(outside / "child", target_is_directory=True)
            model = local / "link" / ".." / "model.pt"
            with (
                patch(
                    "video_analysis_mvp.audio_transcription.shutil.which",
                    return_value="/test-only/whisper",
                ),
                patch("video_analysis_mvp.audio_transcription.run_command") as command,
            ):
                result = transcribe_local(
                    root / "audio.wav", duration=1, model_path=str(model)
                )
            self.assertEqual("failed", result.status)
            command.assert_not_called()

    def test_rejects_deep_json_and_unordered_segments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = self._model(root)
            audio = root / "take.wav"
            audio.write_bytes(b"audio")
            payloads = [
                b'{"segments":' + (b"[" * 2_000) + (b"]" * 2_000) + b"}",
                b'{"language":"en","segments":[{"start":2,"end":3,"text":"later"},{"start":1,"end":2,"text":"earlier"}]}',
            ]
            for payload in payloads:

                def fake_run(
                    args: list[str],
                    timeout: int,
                    *,
                    environment: dict[str, str],
                    payload: bytes = payload,
                ) -> object:
                    output_dir = Path(args[args.index("--output_dir") + 1])
                    (output_dir / "take.json").write_bytes(payload)
                    return object()

                with (
                    patch(
                        "video_analysis_mvp.audio_transcription.shutil.which",
                        return_value="/bin/whisper",
                    ),
                    patch(
                        "video_analysis_mvp.audio_transcription.run_command",
                        side_effect=fake_run,
                    ),
                ):
                    result = transcribe_local(
                        audio, duration=3.0, model_path=str(model)
                    )
                self.assertEqual("failed", result.status)
                self.assertEqual("local whisper output was invalid", result.reason)

    def test_accepts_duration_boundary_and_ignores_blank_timing_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = self._model(root)
            audio = root / "take.wav"
            audio.write_bytes(b"audio")

            def fake_run(
                args: list[str], timeout: int, *, environment: dict[str, str]
            ) -> object:
                output_dir = Path(args[args.index("--output_dir") + 1])
                (output_dir / "take.json").write_text(
                    '{"language":"en","segments":[{"start":0,"end":0,"text":"  "},{"start":0,"end":3,"text":"line"}]}',
                    encoding="utf-8",
                )
                return object()

            with (
                patch(
                    "video_analysis_mvp.audio_transcription.shutil.which",
                    return_value="/bin/whisper",
                ),
                patch(
                    "video_analysis_mvp.audio_transcription.run_command",
                    side_effect=fake_run,
                ),
            ):
                result = transcribe_local(audio, duration=3.0, model_path=str(model))

        self.assertEqual("produced", result.status)
        self.assertEqual(["line"], [segment.text for segment in result.segments])
        self.assertEqual(3.0, result.segments[0].end_time)

    def test_minimal_environment_is_really_passed_to_child_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            environment = _minimal_environment(Path(directory))
            environment["ASR_SECRET"] = "only-this-value"
            result = run_command(
                [
                    sys.executable,
                    "-c",
                    "import json, os; print(json.dumps({k: os.environ.get(k) for k in ('ASR_SECRET', 'HOME', 'HTTP_PROXY')}))",
                ],
                timeout=10,
                environment=environment,
            )

        child = json.loads(result.stdout)
        self.assertEqual("only-this-value", child["ASR_SECRET"])
        self.assertEqual(str(Path(directory)), child["HOME"])
        self.assertIsNone(child["HTTP_PROXY"])
