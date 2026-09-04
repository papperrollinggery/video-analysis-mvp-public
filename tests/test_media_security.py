from __future__ import annotations

import os
import shlex
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from video_analysis_mvp.media import (
    FFPROBE_TIMEOUT_SECONDS,
    _build_review_copy,
    _copy_local_source,
    _download_url,
    _extract_audio,
    _validate_initial_url_target,
    ffprobe_metadata,
    ingest_source,
    normalized_source,
)
from video_analysis_mvp.paths import ProjectPaths
from video_analysis_mvp.schemas import AnalysisProfile
from video_analysis_mvp.utils import (
    ProcessCancelledError,
    ToolError,
    _tool_works,
    run_command,
    run_json,
    sanitize_url_for_storage,
)


def metadata(duration: float = 1.0, *, video: bool = True) -> dict:
    streams = (
        [{"codec_type": "video", "duration": str(duration), "width": 2, "height": 2, "avg_frame_rate": "24/1"}]
        if video
        else [{"codec_type": "audio", "duration": str(duration)}]
    )
    return {"streams": streams, "format": {"duration": str(duration)}}


class SourceUrlBoundaryTest(unittest.TestCase):
    def test_ffprobe_uses_the_bounded_subprocess_deadline(self) -> None:
        with (
            patch(
                "video_analysis_mvp.media.require_tool",
                return_value="/verified/ffprobe",
            ),
            patch(
                "video_analysis_mvp.media.run_json",
                return_value={"streams": [], "format": {}},
            ) as runner,
        ):
            ffprobe_metadata(Path("synthetic.mp4"))

        self.assertEqual(FFPROBE_TIMEOUT_SECONDS, runner.call_args.kwargs["timeout"])
        self.assertEqual("/verified/ffprobe", runner.call_args.args[0][0])

    def test_ffmpeg_helpers_execute_the_verified_tool_path(self) -> None:
        commands: list[list[str]] = []

        def fake_command(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            command = list(args)
            commands.append(command)
            Path(command[-1]).write_bytes(b"generated")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as directory, patch(
            "video_analysis_mvp.media.require_tool",
            return_value="/verified/ffmpeg",
        ), patch("video_analysis_mvp.media.run_command", side_effect=fake_command):
            root = Path(directory)
            master = root / "master.mp4"
            review = root / "review.mp4"
            audio = root / "audio.wav"
            master.write_bytes(b"master")
            _build_review_copy(master, review, 360)
            _extract_audio(review, audio)

        self.assertEqual(2, len(commands))
        self.assertTrue(all(command[0] == "/verified/ffmpeg" for command in commands))

    def test_userinfo_is_rejected_and_query_fragment_are_removed_for_storage(self) -> None:
        with self.assertRaisesRegex(ValueError, "userinfo"):
            normalized_source("https://user:pass@example.test/video?token=secret")
        self.assertEqual(
            "https://example.test/video",
            sanitize_url_for_storage("https://example.test/video?token=secret#private"),
        )

    def test_tool_error_renders_only_sanitized_url_and_redacts_password(self) -> None:
        source = "https://example.test/video?token=query-secret#fragment-secret"
        command = [
            sys.executable,
            "-c",
            "import sys; print('failed', *sys.argv[1:], file=sys.stderr); raise SystemExit(1)",
            "--video-password",
            "password-secret",
            source,
        ]
        with self.assertRaises(ToolError) as caught:
            run_command(command)
        rendered = str(caught.exception)
        self.assertIn("https://example.test/video", rendered)
        self.assertNotIn("query-secret", rendered)
        self.assertNotIn("fragment-secret", rendered)
        self.assertNotIn("password-secret", rendered)

    def test_explicit_sensitive_url_redacts_independently_echoed_components(self) -> None:
        source = "https://example.test/video?access%5Ftoken=component%2Dvalue#component%2Dfragment"
        command = [
            sys.executable,
            "-c",
            (
                "import sys; "
                "sys.stderr.write('access_' + 'token\\n' + 'access%5F' + 'token\\n' + "
                "'component-' + 'value\\n' + 'component%2D' + 'value\\n' + "
                "'component-' + 'fragment\\n' + 'component%2D' + 'fragment'); "
                "raise SystemExit(1)"
            ),
        ]
        with self.assertRaises(ToolError) as caught:
            run_command(command, sensitive_values=(source,))
        rendered = str(caught.exception)
        self.assertNotIn("access_token", rendered)
        self.assertNotIn("access%5Ftoken", rendered)
        self.assertNotIn("component-value", rendered)
        self.assertNotIn("component%2Dvalue", rendered)
        self.assertNotIn("component-fragment", rendered)
        self.assertNotIn("component%2Dfragment", rendered)
        self.assertIn("[REDACTED]", rendered)

    def test_error_rendering_handles_userinfo_with_malformed_port(self) -> None:
        self.assertEqual(
            "https://example.test/video",
            sanitize_url_for_storage(
                "https://user:pass@example.test:not-a-port/video?token=secret",
                reject_userinfo=False,
            ),
        )


class CommandOutputBoundaryTest(unittest.TestCase):
    def test_cancellation_terminates_the_current_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "descendant-survived"
            child = (
                "import pathlib,sys,time; time.sleep(0.5); "
                "pathlib.Path(sys.argv[1]).write_text('alive', encoding='utf-8')"
            )
            parent = (
                "import subprocess,sys,time; "
                "subprocess.Popen([sys.executable, '-c', sys.argv[2], sys.argv[1]]); "
                "time.sleep(5)"
            )
            cancel_after = time.monotonic() + 0.1
            started = time.monotonic()
            with self.assertRaises(ProcessCancelledError):
                run_command(
                    [sys.executable, "-c", parent, str(marker), child],
                    timeout=5,
                    cancelled=lambda: time.monotonic() >= cancel_after,
                )

            self.assertLess(time.monotonic() - started, 2.0)
            time.sleep(0.55)
            self.assertFalse(marker.exists(), "a descendant survived cancellation")

    def test_unverified_cancellation_cleanup_is_exposed_on_the_exception(self) -> None:
        from video_analysis_mvp import utils

        real_terminate = utils._terminate_process_group

        def terminate_but_report_unverified(process: subprocess.Popen[bytes]) -> bool:
            real_terminate(process)
            return False

        cancel_after = time.monotonic() + 0.05
        with (
            patch(
                "video_analysis_mvp.utils._terminate_process_group",
                side_effect=terminate_but_report_unverified,
            ),
            self.assertRaises(ProcessCancelledError) as caught,
        ):
            run_command(
                [sys.executable, "-c", "import time; time.sleep(5)"],
                timeout=5,
                cancelled=lambda: time.monotonic() >= cancel_after,
            )

        self.assertIs(caught.exception.cleanup_verified, False)

    def test_transient_group_cleanup_permission_error_preserves_output_limit_error(self) -> None:
        command = [
            sys.executable,
            "-c",
            (
                "import sys,time; "
                "sys.stdout.write('o' * 700); sys.stdout.flush(); "
                "sys.stderr.write('e' * 700); sys.stderr.flush(); time.sleep(5)"
            ),
        ]
        real_killpg = os.killpg
        injected = False

        def transient_permission_error(process_group_id: int, sig: int) -> None:
            nonlocal injected
            if sig == signal.SIGKILL and not injected:
                injected = True
                raise PermissionError
            real_killpg(process_group_id, sig)

        started = time.monotonic()
        with (
            patch("video_analysis_mvp.utils.MAX_COMMAND_OUTPUT_BYTES", 1024),
            patch(
                "video_analysis_mvp.utils.os.killpg",
                side_effect=transient_permission_error,
            ),
            self.assertRaisesRegex(ToolError, "output exceeded"),
        ):
            run_command(command)
        self.assertTrue(injected)
        self.assertLess(time.monotonic() - started, 2.0)

    def test_persistent_group_cleanup_permission_error_is_reported_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            process_group_file = Path(directory) / "process-group"
            marker = Path(directory) / "descendant-survived"
            child = (
                "import pathlib,sys,time; time.sleep(0.35); "
                "pathlib.Path(sys.argv[1]).write_text('alive', encoding='utf-8')"
            )
            parent = (
                "import os,pathlib,subprocess,sys,time; "
                "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()), encoding='utf-8'); "
                "subprocess.Popen([sys.executable, '-c', sys.argv[3], sys.argv[2]]); "
                "sys.stdout.write('o' * 700); sys.stdout.flush(); "
                "sys.stderr.write('e' * 700); sys.stderr.flush(); time.sleep(5)"
            )
            command = [
                sys.executable,
                "-c",
                parent,
                str(process_group_file),
                str(marker),
                child,
            ]
            started = time.monotonic()
            try:
                with (
                    patch("video_analysis_mvp.utils.MAX_COMMAND_OUTPUT_BYTES", 1024),
                    patch("video_analysis_mvp.utils.os.killpg", side_effect=PermissionError),
                ):
                    with self.assertRaises(ToolError) as caught:
                        run_command(command)
                    rendered = str(caught.exception)
                    self.assertIn("output exceeded", rendered)
                    self.assertIn("Process-group cleanup could not be verified", rendered)
            finally:
                if process_group_file.exists():
                    try:
                        os.killpg(int(process_group_file.read_text(encoding="utf-8")), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            self.assertLess(time.monotonic() - started, 2.0)
            time.sleep(0.45)
            self.assertFalse(marker.exists(), "test cleanup left a descendant running")

    def test_combined_output_limit_terminates_the_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "descendant-survived"
            child = (
                "import pathlib,sys,time; time.sleep(0.35); "
                "pathlib.Path(sys.argv[1]).write_text('alive', encoding='utf-8')"
            )
            parent = (
                "import subprocess,sys,time; "
                "subprocess.Popen([sys.executable, '-c', sys.argv[2], sys.argv[1]]); "
                "sys.stdout.write('o' * 700); sys.stdout.flush(); "
                "sys.stderr.write('e' * 700); sys.stderr.flush(); time.sleep(0.8)"
            )
            with (
                patch("video_analysis_mvp.utils.MAX_COMMAND_OUTPUT_BYTES", 1024),
                self.assertRaisesRegex(ToolError, "output exceeded"),
            ):
                run_command([sys.executable, "-c", parent, str(marker), child])
            time.sleep(0.45)
            self.assertFalse(marker.exists(), "a descendant survived output-limit termination")

    def test_timeout_terminates_the_process_group_and_redacts_output(self) -> None:
        secret = "timeout-secret"
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "descendant-survived"
            child = (
                "import pathlib,sys,time; time.sleep(0.35); "
                "pathlib.Path(sys.argv[1]).write_text('alive', encoding='utf-8')"
            )
            parent = (
                "import subprocess,sys,time; "
                "subprocess.Popen([sys.executable, '-c', sys.argv[2], sys.argv[1]]); "
                "sys.stderr.write(sys.argv[4]); sys.stderr.flush(); time.sleep(0.8)"
            )
            command = [
                sys.executable,
                "-c",
                parent,
                str(marker),
                child,
                "--video-password",
                secret,
            ]
            with self.assertRaises(ToolError) as caught:
                run_command(command, timeout=0.08)
            time.sleep(0.45)
            self.assertFalse(marker.exists(), "a descendant survived timeout termination")
            self.assertNotIn(secret, str(caught.exception))
            self.assertIn("[REDACTED]", str(caught.exception))

    def test_run_json_inherits_the_combined_output_limit(self) -> None:
        script = "import sys; sys.stdout.write('{\"value\":\"' + 'x' * 2048 + '\"}')"
        with (
            patch("video_analysis_mvp.utils.MAX_COMMAND_OUTPUT_BYTES", 1024),
            self.assertRaisesRegex(ToolError, "output exceeded"),
        ):
            run_json([sys.executable, "-c", script])

    def test_tool_probe_inherits_the_combined_output_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tool = Path(directory) / "noisy-tool"
            tool.write_text(
                f"#!{sys.executable}\nimport sys\nsys.stdout.write('x' * 2048)\n",
                encoding="utf-8",
            )
            tool.chmod(0o700)
            with patch("video_analysis_mvp.utils.MAX_COMMAND_OUTPUT_BYTES", 1024):
                self.assertFalse(_tool_works(str(tool)))

    def test_success_preserves_completed_process_text_output(self) -> None:
        result = run_command(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.write('stdout'); sys.stderr.write('stderr')",
            ]
        )
        self.assertEqual(0, result.returncode)
        self.assertEqual("stdout", result.stdout)
        self.assertEqual("stderr", result.stderr)


class InitialUrlNetworkBoundaryTest(unittest.TestCase):
    def test_literal_non_public_targets_are_rejected_without_dns(self) -> None:
        blocked = (
            "127.0.0.1",
            "10.0.0.1",
            "100.64.0.1",
            "169.254.1.2",
            "0.0.0.0",
            "224.0.0.1",
            "240.0.0.1",
            "::1",
            "fe80::1",
            "ff02::1",
            "::",
        )
        with patch("video_analysis_mvp.media.socket.getaddrinfo") as getaddrinfo:
            for address in blocked:
                host = f"[{address}]" if ":" in address else address
                with self.subTest(address=address), self.assertRaisesRegex(ValueError, "public network"):
                    _validate_initial_url_target(f"https://{host}/video")
        getaddrinfo.assert_not_called()

    def test_dns_target_is_rejected_if_any_answer_is_non_public(self) -> None:
        answers = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
        ]
        with (
            patch("video_analysis_mvp.media.socket.getaddrinfo", return_value=answers),
            self.assertRaisesRegex(ValueError, "public network"),
        ):
            _validate_initial_url_target("https://video.example.test/watch")

    def test_dns_target_with_only_public_answers_is_accepted(self) -> None:
        answers = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:2800:220:1:248:1893:25c8:1946", 443, 0, 0)),
        ]
        with patch("video_analysis_mvp.media.socket.getaddrinfo", return_value=answers):
            _validate_initial_url_target("https://video.example.test/watch")

    def test_unresolvable_initial_target_is_rejected(self) -> None:
        with (
            patch(
                "video_analysis_mvp.media.socket.getaddrinfo",
                side_effect=socket.gaierror(socket.EAI_NONAME, "not found"),
            ),
            self.assertRaisesRegex(ValueError, "could not be resolved"),
        ):
            _validate_initial_url_target("https://missing.example.test/watch")


class YtDlpSecretTransportTest(unittest.TestCase):
    @staticmethod
    def public_dns(*_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    def test_url_and_password_use_private_temp_files_not_argv(self) -> None:
        source = "https://video.example.test/watch?token=query-secret#fragment-secret"
        password = "password secret 'quoted'"
        commands: list[list[str]] = []
        private_paths: set[Path] = set()

        def inspect_args(args: list[str] | tuple[str, ...]) -> None:
            command = list(args)
            commands.append(command)
            rendered = "\0".join(command)
            self.assertNotIn(source, rendered)
            self.assertNotIn(password, rendered)
            self.assertNotIn("query-secret", rendered)
            batch = Path(command[command.index("--batch-file") + 1])
            private_paths.add(batch)
            self.assertEqual(0o600, stat.S_IMODE(batch.stat().st_mode))
            self.assertEqual(f"{source}\n", batch.read_text(encoding="utf-8"))
            config = Path(command[command.index("--config-locations") + 1])
            private_paths.add(config)
            self.assertEqual(0o600, stat.S_IMODE(config.stat().st_mode))
            config_args = shlex.split(config.read_text(encoding="utf-8"))
            self.assertEqual(password, config_args[config_args.index("--video-password") + 1])

        def fake_json(args: list[str], **kwargs: object) -> dict[str, object]:
            inspect_args(args)
            self.assertEqual((source, password), tuple(kwargs["sensitive_values"]))
            return {"title": "fixture", "webpage_url": source}

        def fake_command(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            inspect_args(args)
            self.assertEqual((source, password), tuple(kwargs["sensitive_values"]))
            template = args[args.index("-o") + 1]
            Path(template.replace("%(ext)s", "mp4")).write_bytes(b"video")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as directory:
            master = Path(directory) / "master.mp4"
            with (
                patch("video_analysis_mvp.media.require_tool", return_value="/tools/yt-dlp"),
                patch("video_analysis_mvp.media.socket.getaddrinfo", side_effect=self.public_dns),
                patch("video_analysis_mvp.media.run_json", side_effect=fake_json),
                patch("video_analysis_mvp.media.run_command", side_effect=fake_command),
            ):
                metadata_result = _download_url(
                    source,
                    master,
                    password,
                    360,
                    max_source_bytes=1024,
                )
            self.assertEqual(b"video", master.read_bytes())
            self.assertEqual("https://video.example.test/watch", metadata_result["webpage_url"])

        self.assertEqual(2, len(commands))
        self.assertTrue(private_paths)
        self.assertTrue(all(not path.exists() for path in private_paths))

    def test_playlist_metadata_is_rejected_before_download(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            master = Path(directory) / "master.mp4"
            with (
                patch(
                    "video_analysis_mvp.media.require_tool",
                    return_value="/tools/yt-dlp",
                ),
                patch(
                    "video_analysis_mvp.media.socket.getaddrinfo",
                    side_effect=self.public_dns,
                ),
                patch(
                    "video_analysis_mvp.media.run_json",
                    return_value={"_type": "playlist", "entries": [{"id": "one"}]},
                ) as metadata_call,
                patch("video_analysis_mvp.media.run_command") as downloader,
                self.assertRaisesRegex(ValueError, "exactly one video"),
            ):
                _download_url(
                    "https://video.example.test/playlist",
                    master,
                    None,
                    360,
                    max_source_bytes=1024,
                )

        self.assertIn("--no-playlist", metadata_call.call_args.args[0])
        downloader.assert_not_called()

    def test_config_and_batch_injection_characters_are_rejected_before_tool_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            master = Path(directory) / "master.mp4"
            for source, password in (
                ("https://video.example.test/watch\n--exec calc", None),
                ("https://video.example.test/watch\r--exec calc", None),
                ("https://video.example.test/watch\0tail", None),
                ("https://video.example.test/watch", "secret\n--exec calc"),
                ("https://video.example.test/watch", "secret\r--exec calc"),
                ("https://video.example.test/watch", "secret\0tail"),
            ):
                with (
                    self.subTest(source=source, password=password),
                    patch("video_analysis_mvp.media.require_tool") as require_tool,
                    self.assertRaisesRegex(ValueError, "CR, LF, or NUL"),
                ):
                    _download_url(source, master, password, 360, max_source_bytes=1024)
                require_tool.assert_not_called()

    def test_failed_tool_output_is_redacted_and_private_files_are_cleaned(self) -> None:
        source = "https://video.example.test/watch?token=query-secret#fragment-secret"
        password = "password-secret"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "private-paths.txt"
            tool = root / "fake-yt-dlp"
            tool.write_text(
                f"#!{sys.executable}\n"
                "from pathlib import Path\n"
                "import sys\n"
                "batch = Path(sys.argv[sys.argv.index('--batch-file') + 1])\n"
                "config = Path(sys.argv[sys.argv.index('--config-locations') + 1])\n"
                f"Path({str(marker)!r}).write_text(f'{{batch}}\\n{{config}}', encoding='utf-8')\n"
                "sys.stderr.write(batch.read_text(encoding='utf-8'))\n"
                "sys.stderr.write(config.read_text(encoding='utf-8'))\n"
                "raise SystemExit(2)\n",
                encoding="utf-8",
            )
            tool.chmod(0o700)
            with (
                patch("video_analysis_mvp.media.require_tool", return_value=str(tool)),
                patch("video_analysis_mvp.media.socket.getaddrinfo", side_effect=self.public_dns),
                self.assertRaises(ToolError) as caught,
            ):
                _download_url(
                    source,
                    root / "master.mp4",
                    password,
                    360,
                    max_source_bytes=1024,
                )

            rendered = str(caught.exception)
            self.assertIn("https://video.example.test/watch", rendered)
            self.assertNotIn("query-secret", rendered)
            self.assertNotIn("fragment-secret", rendered)
            self.assertNotIn(password, rendered)
            self.assertIn("[REDACTED]", rendered)
            private_paths = [Path(value) for value in marker.read_text(encoding="utf-8").splitlines()]
            self.assertTrue(private_paths)
            self.assertTrue(all(not path.exists() for path in private_paths))


class LocalIngestBoundaryTest(unittest.TestCase):
    def test_symlink_source_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"video")
            link = root / "link.mp4"
            link.symlink_to(source)
            target = root / "master.mp4"
            with self.assertRaises(ValueError):
                _copy_local_source(link, target, 100)
            self.assertFalse(target.exists())

    def test_source_swap_after_open_copies_original_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"original-video")
            replacement = root / "replacement.mp4"
            replacement.write_bytes(b"attacker-video")
            target = root / "master.mp4"
            actual_read = os.read
            swapped = False

            def swap_then_read(descriptor: int, size: int) -> bytes:
                nonlocal swapped
                if not swapped:
                    swapped = True
                    replacement.replace(source)
                return actual_read(descriptor, size)

            with patch("video_analysis_mvp.media.os.read", side_effect=swap_then_read):
                _copy_local_source(source, target, 100)
            self.assertEqual(b"original-video", target.read_bytes())
            self.assertEqual(b"attacker-video", source.read_bytes())

    def test_relative_source_has_explicit_cwd_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            previous = Path.cwd()
            try:
                os.chdir(directory)
                source_type, source = normalized_source("same-name.mp4")
            finally:
                os.chdir(previous)
            self.assertEqual("file", source_type.value)
            self.assertEqual(str(Path(os.path.realpath(directory)) / "same-name.mp4"), source)

    def test_local_duration_limit_cleans_partial_ingest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"video-bytes")
            paths = ProjectPaths(root / "workspace" / "project")
            paths.ensure()
            with (
                patch("video_analysis_mvp.media.require_tool", return_value="/tools/ffmpeg"),
                patch("video_analysis_mvp.media.ffprobe_metadata", return_value=metadata(61.0)),
                self.assertRaisesRegex(ValueError, "duration"),
            ):
                ingest_source(source.as_posix(), paths, AnalysisProfile.research, max_duration_seconds=60)
            self.assertFalse((paths.ingest / "master.mp4").exists())
            self.assertFalse((paths.data / "media_package.json").exists())
            self.assertFalse(paths.manifest.exists())

    def test_url_download_is_revalidated_and_failure_is_cleaned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = ProjectPaths(root / "workspace" / "project")
            paths.ensure()

            def fake_download(_source: str, master: Path, *_: object, **__: object) -> dict:
                master.write_bytes(b"not-really-video")
                return {"webpage_url": "https://example.test/video?secret=value"}

            with (
                patch("video_analysis_mvp.media.require_tool", return_value="/tools/tool"),
                patch("video_analysis_mvp.media._download_url", side_effect=fake_download),
                patch("video_analysis_mvp.media.ffprobe_metadata", return_value=metadata(video=False)),
                self.assertRaisesRegex(ValueError, "video stream"),
            ):
                ingest_source("https://example.test/video?secret=value", paths, AnalysisProfile.research)
            self.assertFalse((paths.ingest / "master.mp4").exists())
            self.assertFalse((paths.data / "media_package.json").exists())

    def test_persisted_url_and_download_metadata_are_sanitized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = ProjectPaths(root / "workspace" / "project")
            paths.ensure()

            def fake_download(_source: str, master: Path, *_: object, **__: object) -> dict:
                master.write_bytes(b"video")
                return {"webpage_url": "https://example.test/video?secret=value"}

            def fake_review(_master: Path, review: Path, _height: int) -> None:
                review.write_bytes(b"review")

            def fake_audio(_review: Path, audio: Path) -> None:
                audio.write_bytes(b"audio")

            with (
                patch("video_analysis_mvp.media.require_tool", return_value="/tools/tool"),
                patch("video_analysis_mvp.media._download_url", side_effect=fake_download),
                patch("video_analysis_mvp.media._build_review_copy", side_effect=fake_review),
                patch("video_analysis_mvp.media._extract_audio", side_effect=fake_audio),
                patch("video_analysis_mvp.media.ffprobe_metadata", return_value=metadata()),
            ):
                package = ingest_source(
                    "https://example.test/video?secret=value#fragment",
                    paths,
                    AnalysisProfile.research,
                )
            self.assertEqual("https://example.test/video", package.source)
            self.assertEqual("https://example.test/video", package.metadata["yt_dlp"]["webpage_url"])
            receipt = package.metadata["media_receipt"]
            self.assertEqual("1.0", receipt["schema_version"])
            self.assertEqual(64, len(receipt["master"]["sha256"]))
            self.assertEqual(64, len(receipt["review"]["sha256"]))
            self.assertEqual(5, receipt["master"]["size_bytes"])
            self.assertEqual(6, receipt["review"]["size_bytes"])
            serialized = (paths.data / "media_package.json").read_text(encoding="utf-8")
            self.assertNotIn("secret=value", serialized)
            self.assertNotIn("fragment", serialized)
            self.assertNotIn("secret=value", paths.manifest.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
