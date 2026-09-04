from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

from video_analysis_mvp.config import save_runtime_config
from video_analysis_mvp.paths import ProjectPaths
from video_analysis_mvp.schemas import Shot, dump_json, load_json
from video_analysis_mvp.vision import (
    ADS_INTERPRETATION_FIELDS,
    MAX_PROVIDER_RESPONSE_BYTES,
    MINIMAX_MCP_VERSION,
    OBSERVATION_FIELDS,
    FrameInput,
    _NoRedirectHandler,
    _call_minimax_understand_image,
    _communicate_bounded,
    _prepare_minimax_mcp,
    _read_project_frame,
    analyze_frame,
    analyze_frame_with_minimax_mcp,
    annotate_project_with_minimax_mcp,
    annotate_project_with_vision,
    apply_vision_data,
    validate_vision_payload,
)


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def payload(profile: str = "research", **updates: object) -> dict[str, object]:
    fields = [*OBSERVATION_FIELDS, *(ADS_INTERPRETATION_FIELDS if profile == "ads" else [])]
    result: dict[str, object] = {field: "none" for field in fields}
    result.update({"content_summary": "A visible subject.", "confidence": 0.8})
    result.update(updates)
    return result


def shot(number: int = 1, **updates: object) -> Shot:
    data: dict[str, object] = {
        "shot_id": f"shot_{number:04d}",
        "shot_no": number,
        "start_time": 0.0,
        "end_time": 1.0,
        "duration": 1.0,
        "frame_ref": f"shot_{number:04d}.png",
        "primary_frame_ref": f"shot_{number:04d}.png",
        "frame_refs": [f"shot_{number:04d}.png"],
        "annotation_source": "machine",
        "readiness_status": "blocked",
        "content_summary": "original",
    }
    data.update(updates)
    return Shot(**data)


class ProviderBoundaryTest(unittest.TestCase):
    def project(self, directory: str, shots: list[Shot]) -> ProjectPaths:
        paths = ProjectPaths(Path(directory) / "project")
        paths.ensure()
        dump_json(paths.data / "shots.json", shots)
        for item in shots:
            if item.frame_ref:
                (paths.keyframes / item.frame_ref).write_bytes(PNG_1X1)
        return paths

    def test_unknown_provider_is_rejected_before_file_or_network_access(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory) / "project")
            with (
                patch.dict(os.environ, {"OPENAI_API_KEY": "ambient-secret"}, clear=True),
                patch("video_analysis_mvp.vision.analyze_frame") as network,
                self.assertRaisesRegex(ValueError, "Unsupported vision provider"),
            ):
                annotate_project_with_vision(paths, provider="not-a-provider")
        network.assert_not_called()

    def test_custom_endpoints_never_receive_ambient_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self.project(directory, [shot()])
            save_runtime_config(root, {"openai_base_url": "https://proxy.example/v1"})
            with (
                patch.dict(os.environ, {"OPENAI_API_KEY": "ambient-openai"}, clear=True),
                patch("video_analysis_mvp.vision.analyze_frame") as openai,
            ):
                result = annotate_project_with_vision(paths, provider="openai")
            self.assertEqual("error", result.status)
            openai.assert_not_called()

            save_runtime_config(root, {"minimax_api_host": "https://proxy.example"})
            with (
                patch.dict(os.environ, {"MINIMAX_API_KEY": "ambient-minimax"}, clear=True),
                patch("video_analysis_mvp.vision._prepare_minimax_mcp") as prepare,
            ):
                result = annotate_project_with_minimax_mcp(paths)
            self.assertEqual("error", result.status)
            prepare.assert_not_called()

    def test_endpoint_change_clears_bound_key_and_explicit_rebind_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self.project(directory, [shot()])
            save_runtime_config(root, {"openai_api_key": "bound-first"})
            changed = save_runtime_config(root, {"openai_base_url": "https://proxy.example/v1"})
            self.assertEqual("", changed.openai_api_key)
            rebound = save_runtime_config(root, {"openai_api_key": "bound-custom"})
            self.assertEqual("bound-custom", rebound.openai_api_key)
            with patch(
                "video_analysis_mvp.vision.analyze_frame",
                return_value=payload(),
            ) as analyzer:
                result = annotate_project_with_vision(paths, provider="openai")
            self.assertEqual("success", result.status)
            self.assertEqual("bound-custom", analyzer.call_args.args[2])

    def test_invalid_payloads_do_not_mutate_shot(self) -> None:
        invalid = [
            {},
            {**payload(), "content_summary": ""},
            {**payload(), "confidence": True},
            {**payload(), "confidence": "0.8"},
            {**payload(), "confidence": float("nan")},
            {**payload(), "confidence": float("inf")},
            {**payload(), "confidence": -0.1},
            {**payload(), "confidence": 1.1},
            {**payload(), "unexpected": "field"},
        ]
        for candidate in invalid:
            with self.subTest(candidate=candidate):
                current = shot()
                before = current.model_dump(mode="json")
                with self.assertRaises(ValueError):
                    apply_vision_data(current, candidate)
                self.assertEqual(before, current.model_dump(mode="json"))

    def test_human_and_rejected_shots_are_never_submitted_or_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            protected = [
                shot(1, annotation_source="human", readiness_status="ready"),
                shot(2, annotation_source="machine", readiness_status="rejected"),
            ]
            paths = self.project(directory, protected)
            analyzer = Mock(return_value=payload())
            with (
                patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}, clear=True),
                patch("video_analysis_mvp.vision.analyze_frame", analyzer),
            ):
                result = annotate_project_with_vision(paths, provider="openai")
            self.assertEqual("warning", result.status)
            analyzer.assert_not_called()
            persisted = load_json(paths.data / "shots.json")
            self.assertEqual("human", persisted[0]["annotation_source"])
            self.assertEqual("rejected", persisted[1]["readiness_status"])

    def test_versioned_receipt_has_provider_lineage_frame_hash_and_no_secret(self) -> None:
        secret = "receipt-secret-must-not-appear"
        with tempfile.TemporaryDirectory() as directory:
            paths = self.project(directory, [shot()])
            with (
                patch.dict(os.environ, {"OPENAI_API_KEY": secret}, clear=True),
                patch("video_analysis_mvp.vision.analyze_frame", return_value=payload()),
            ):
                result = annotate_project_with_vision(paths, provider="openai")
            self.assertEqual("success", result.status)
            receipt = load_json(paths.data / "vision_annotations.json")
            self.assertEqual("1.0", receipt["schema_version"])
            self.assertEqual("openai", receipt["provider"])
            self.assertEqual("openai_chat_completions", receipt["provider_source"])
            self.assertEqual("https://api.openai.com", receipt["endpoint_origin"])
            self.assertEqual(["shot_0001"], receipt["selected_shot_ids"])
            self.assertEqual(["shot_0001"], receipt["annotated_shot_ids"])
            self.assertEqual([], receipt["skipped_shot_ids"])
            self.assertEqual(hashlib.sha256(PNG_1X1).hexdigest(), receipt["input_frames"][0]["sha256"])
            self.assertEqual(len(PNG_1X1), receipt["input_frames"][0]["size_bytes"])
            self.assertNotIn(secret, json.dumps(receipt))


class FrameBoundaryTest(unittest.TestCase):
    def test_empty_text_polyglot_oversize_and_symlink_frames_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory) / "project")
            paths.ensure()
            outside = Path(directory) / "outside.png"
            outside.write_bytes(PNG_1X1)
            cases = {
                "empty.png": b"",
                "text.png": b"not an image",
                "polyglot.png": PNG_1X1 + b"<script>alert(1)</script>",
            }
            for name, data in cases.items():
                (paths.keyframes / name).write_bytes(data)
                with self.subTest(name=name), self.assertRaises(ValueError):
                    _read_project_frame(paths, name)
            (paths.keyframes / "link.png").symlink_to(outside)
            with self.assertRaises(ValueError):
                _read_project_frame(paths, "link.png")
            outside_dir = Path(directory) / "outside-dir"
            outside_dir.mkdir()
            (outside_dir / "frame.png").write_bytes(PNG_1X1)
            (paths.keyframes / "linked-dir").symlink_to(outside_dir, target_is_directory=True)
            with self.assertRaises(ValueError):
                _read_project_frame(paths, "linked-dir/frame.png")
            with patch("video_analysis_mvp.vision.MAX_FRAME_BYTES", 8):
                (paths.keyframes / "large.png").write_bytes(PNG_1X1)
                with self.assertRaisesRegex(ValueError, "exceeds"):
                    _read_project_frame(paths, "large.png")

    def test_frame_hash_and_bytes_come_from_the_same_open_descriptor_during_swap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory) / "project")
            paths.ensure()
            target = paths.keyframes / "frame.png"
            target.write_bytes(PNG_1X1)
            replacement = paths.keyframes / "replacement.png"
            replacement.write_bytes(b"attacker text")
            from video_analysis_mvp import vision

            original = vision._frame_from_fd

            def swap_then_read(descriptor: int, reference: str):
                replacement.replace(target)
                return original(descriptor, reference)

            with patch("video_analysis_mvp.vision._frame_from_fd", side_effect=swap_then_read):
                frame = _read_project_frame(paths, "frame.png")
            self.assertEqual(PNG_1X1, frame.data)
            self.assertEqual(hashlib.sha256(frame.data).hexdigest(), frame.sha256)
            self.assertEqual(b"attacker text", target.read_bytes())


class OpenAITransportTest(unittest.TestCase):
    def test_redirect_handler_refuses_every_redirect(self) -> None:
        handler = _NoRedirectHandler()
        for code in (301, 302, 303, 307, 308):
            with self.subTest(code=code):
                try:
                    handler.redirect_request(
                        Mock(full_url="https://api.openai.com/v1/chat/completions"),
                        None,
                        code,
                        "redirect",
                        {},
                        "https://attacker.example/capture",
                    )
                except urllib.error.HTTPError as exc:
                    exc.close()
                else:  # pragma: no cover - assertion branch
                    self.fail(f"redirect {code} was accepted")

    def test_strict_payload_rejects_bool_strings_nan_inf_and_partial_objects(self) -> None:
        invalid_confidences = [True, "0.8", math.nan, math.inf, -1, 2]
        for confidence in invalid_confidences:
            with self.subTest(confidence=confidence), self.assertRaises(ValueError):
                validate_vision_payload({**payload(), "confidence": confidence})
        with self.assertRaisesRegex(ValueError, "missing required fields"):
            validate_vision_payload({"content_summary": "partial", "confidence": 0.8})

    def test_request_has_output_cap_and_response_read_is_bounded(self) -> None:
        frame = FrameInput(
            reference="frame.png",
            data=PNG_1X1,
            sha256=hashlib.sha256(PNG_1X1).hexdigest(),
            size_bytes=len(PNG_1X1),
            media_type="image/png",
            width=1,
            height=1,
        )
        captured: dict[str, object] = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_: object) -> None:
                return None

            def read(self, maximum: int) -> bytes:
                captured["read_maximum"] = maximum
                return json.dumps(
                    {"choices": [{"message": {"content": json.dumps(payload())}}]}
                ).encode("utf-8")

        class Opener:
            def open(self, request: object, **_: object) -> Response:
                captured["request"] = request
                return Response()

        with patch("video_analysis_mvp.vision.urllib.request.build_opener", return_value=Opener()):
            result = analyze_frame(frame, shot(), "bound-key", "gpt-test", None)
        request_payload = json.loads(captured["request"].data.decode("utf-8"))  # type: ignore[attr-defined]
        self.assertEqual(2000, request_payload["max_completion_tokens"])
        self.assertEqual(MAX_PROVIDER_RESPONSE_BYTES + 1, captured["read_maximum"])
        self.assertEqual("A visible subject.", result["content_summary"])

    def test_oversized_success_and_error_bodies_are_bounded_and_secrets_are_not_echoed(self) -> None:
        frame = FrameInput(
            reference="frame.png",
            data=PNG_1X1,
            sha256=hashlib.sha256(PNG_1X1).hexdigest(),
            size_bytes=len(PNG_1X1),
            media_type="image/png",
            width=1,
            height=1,
        )

        class OversizedResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_: object) -> None:
                return None

            def read(self, maximum: int) -> bytes:
                return b"x" * maximum

        with (
            patch(
                "video_analysis_mvp.vision.urllib.request.build_opener",
                return_value=Mock(open=Mock(return_value=OversizedResponse())),
            ),
            self.assertRaisesRegex(ValueError, "response exceeds"),
        ):
            analyze_frame(frame, shot(), "bound-key", "gpt-test", None)

        secret = "upstream-secret-must-not-echo"
        error = urllib.error.HTTPError(
            "https://api.openai.com/v1/chat/completions",
            500,
            "failure",
            {},
            io.BytesIO((secret.encode() + b"x" * MAX_PROVIDER_RESPONSE_BYTES)),
        )
        with patch(
            "video_analysis_mvp.vision.urllib.request.build_opener",
            return_value=Mock(open=Mock(side_effect=error)),
        ):
            with self.assertRaises(RuntimeError) as caught:
                analyze_frame(frame, shot(), "bound-key", "gpt-test", None)
        self.assertEqual("Vision API request failed with HTTP 500", str(caught.exception))
        self.assertNotIn(secret, str(caught.exception))


class MiniMaxProcessBoundaryTest(unittest.TestCase):
    def test_mcp_receives_minimal_environment_without_ambient_secrets(self) -> None:
        ambient = {
            "AWS_SECRET_ACCESS_KEY": "aws-secret",
            "GITHUB_TOKEN": "github-secret",
            "OPENAI_API_KEY": "openai-secret",
            "DATABASE_URL": "db-secret",
        }
        process = Mock()
        with (
            patch.dict(os.environ, ambient, clear=True),
            patch("video_analysis_mvp.vision.subprocess.Popen", return_value=process) as popen,
            patch(
                "video_analysis_mvp.vision._communicate_bounded",
                return_value=(
                    b'{"jsonrpc":"2.0","id":2,"result":{"content":[{"text":"ok"}]}}\n',
                    b"",
                ),
            ),
        ):
            result = _call_minimax_understand_image(
                "/private/frame.png",
                "inspect",
                "minimax-bound-key",
                executable="/tools/minimax",
            )
        self.assertEqual("ok", result)
        child_env = popen.call_args.kwargs["env"]
        for name in ambient:
            self.assertNotIn(name, child_env)
        self.assertEqual("minimax-bound-key", child_env["MINIMAX_API_KEY"])
        self.assertEqual("/private", child_env["MINIMAX_MCP_BASE_PATH"])

    def test_timeout_kills_and_reaps_the_process(self) -> None:
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=os.name == "posix",
        )
        with self.assertRaisesRegex(RuntimeError, "timed out"):
            _communicate_bounded(proc, b"", timeout=0.05, maximum=1024)
        self.assertIsNotNone(proc.poll())

    def test_combined_stdout_stderr_limit_terminates_process(self) -> None:
        script = "import os,time; os.write(1,b'x'*4096); os.write(2,b'y'*4096); time.sleep(30)"
        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=os.name == "posix",
        )
        with self.assertRaisesRegex(RuntimeError, "output exceeds"):
            _communicate_bounded(proc, b"", timeout=5, maximum=1024)
        self.assertIsNotNone(proc.poll())

    @unittest.skipUnless(os.name == "posix", "process-group cleanup requires POSIX")
    def test_exited_parent_does_not_leave_pipe_holding_descendant(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "descendant-survived"
            child = (
                "import pathlib,sys,time; time.sleep(0.35); "
                "pathlib.Path(sys.argv[1]).write_text('alive', encoding='utf-8')"
            )
            parent = (
                "import subprocess,sys; "
                "subprocess.Popen([sys.executable, '-c', sys.argv[2], sys.argv[1]])"
            )
            proc = subprocess.Popen(
                [sys.executable, "-c", parent, str(marker), child],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            with patch("video_analysis_mvp.vision.MINIMAX_DRAIN_GRACE_SECONDS", 0.05):
                _communicate_bounded(proc, b"", timeout=5, maximum=1024)
            self.assertIsNotNone(proc.poll())
            time.sleep(0.45)
            self.assertFalse(marker.exists(), "a pipe-holding descendant survived parent exit")

    @unittest.skipUnless(os.name == "posix", "process-group cleanup requires POSIX")
    def test_exited_parent_does_not_leave_closed_stdio_descendant(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "closed-stdio-descendant-survived"
            child = (
                "import pathlib,sys,time; time.sleep(0.35); "
                "pathlib.Path(sys.argv[1]).write_text('alive', encoding='utf-8')"
            )
            parent = (
                "import subprocess,sys; "
                "subprocess.Popen([sys.executable, '-c', sys.argv[2], sys.argv[1]], "
                "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)"
            )
            proc = subprocess.Popen(
                [sys.executable, "-c", parent, str(marker), child],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            _communicate_bounded(proc, b"", timeout=5, maximum=1024)
            self.assertIsNotNone(proc.poll())
            time.sleep(0.45)
            self.assertFalse(marker.exists(), "a closed-stdio descendant survived parent exit")

    def test_stdin_write_is_covered_by_the_process_deadline(self) -> None:
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(1.2)"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=os.name == "posix",
        )
        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "timed out"):
            _communicate_bounded(
                proc,
                b"x" * (4 * 1024 * 1024),
                timeout=0.05,
                maximum=1024,
            )
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.5, f"blocked stdin write ignored the deadline for {elapsed:.3f}s")
        self.assertIsNotNone(proc.poll())

    def test_broken_stdin_pipe_fails_closed_and_reaps_process(self) -> None:
        proc = subprocess.Popen(
            [sys.executable, "-c", "import os,time; os.close(0); time.sleep(30)"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=os.name == "posix",
        )
        with self.assertRaisesRegex(RuntimeError, "stdin communication failed"):
            _communicate_bounded(
                proc,
                b"x" * (1024 * 1024),
                timeout=5,
                maximum=1024,
            )
        self.assertIsNotNone(proc.poll())

    def test_oversized_input_fails_immediately_and_reaps_process(self) -> None:
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=os.name == "posix",
        )
        started = time.monotonic()
        try:
            with self.assertRaisesRegex(RuntimeError, "input exceeds"):
                _communicate_bounded(
                    proc,
                    b"x" * 1025,
                    timeout=5,
                    maximum=1024,
                    input_maximum=1024,
                )
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 0.5, f"oversized input cleanup took {elapsed:.3f}s")
            self.assertIsNotNone(proc.poll())
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=2)

    def test_preinstalled_executable_must_report_exact_pinned_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "minimax-coding-plan-mcp"
            executable.write_text("#!/bin/sh\necho minimax-coding-plan-mcp 0.0.4\n", encoding="utf-8")
            executable.chmod(0o755)
            with patch.dict(os.environ, {"MINIMAX_MCP_EXECUTABLE": str(executable)}, clear=True):
                path, version = _prepare_minimax_mcp()
            self.assertEqual(executable.resolve(), Path(path))
            self.assertEqual(MINIMAX_MCP_VERSION, version)
            executable.write_text("#!/bin/sh\necho minimax-coding-plan-mcp 0.0.5\n", encoding="utf-8")
            with (
                patch.dict(os.environ, {"MINIMAX_MCP_EXECUTABLE": str(executable)}, clear=True),
                self.assertRaisesRegex(RuntimeError, "exactly version"),
            ):
                _prepare_minimax_mcp()

    def test_minimax_uses_private_validated_snapshot_and_removes_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            frame_path = Path(directory) / "frame.png"
            frame_path.write_bytes(PNG_1X1)
            observed: dict[str, object] = {}

            def inspect_snapshot(image_source: str, _prompt: str, _key: str, **_: object) -> str:
                snapshot = Path(image_source)
                observed["path"] = snapshot
                observed["bytes"] = snapshot.read_bytes()
                observed["mode"] = snapshot.stat().st_mode & 0o777
                return json.dumps(payload())

            with patch("video_analysis_mvp.vision._call_minimax_understand_image", side_effect=inspect_snapshot):
                result = analyze_frame_with_minimax_mcp(
                    frame_path,
                    shot(),
                    "bound-key",
                    executable="/tools/minimax",
                )
            self.assertEqual("A visible subject.", result["content_summary"])
            self.assertEqual(PNG_1X1, observed["bytes"])
            self.assertEqual(0o600, observed["mode"])
            self.assertFalse(Path(observed["path"]).exists())


if __name__ == "__main__":
    unittest.main()
