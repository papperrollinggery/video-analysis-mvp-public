from __future__ import annotations

import csv
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from video_analysis_mvp.cli import (
    MAX_CLI_SOURCE_VALUE_BYTES,
    _read_private_value_file,
    main,
)
from video_analysis_mvp.media import create_project_id
from video_analysis_mvp.paths import project_paths
from video_analysis_mvp.safe_io import _PATH_LOCKS, _PATH_LOCKS_GUARD, path_lock
from video_analysis_mvp.schemas import AnalysisProfile, Shot, StatusEnvelope
from video_analysis_mvp.utils import ToolError, run_command
from video_analysis_mvp.visual import write_shots_csv
from video_analysis_mvp.workspace_api import (
    ApiError,
    _deliverable_specs,
    _path_payload,
    _project_root,
    deliverable_preview,
    dispatch_api,
    ensure_workspace_api_files,
)


class ProjectPathBoundaryTest(unittest.TestCase):
    def test_project_paths_reject_escape_inputs_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            workspace = base / "workspace"
            workspace.mkdir()

            outside_absolute = base / "absolute-escape"
            for project_id in ("../dotdot-escape", str(outside_absolute)):
                with self.subTest(project_id=project_id):
                    with self.assertRaisesRegex(ValueError, "Invalid project id"):
                        project_paths(project_id, workspace)

            self.assertFalse((base / "dotdot-escape").exists())
            self.assertFalse(outside_absolute.exists())

    def test_project_paths_reject_symlink_escape_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            workspace = base / "workspace"
            workspace.mkdir()
            outside = base / "outside"
            outside.mkdir()
            (workspace / "linked-project").symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "stay within the workspace"):
                project_paths("linked-project", workspace)

            self.assertEqual([], list(outside.iterdir()))

    def test_project_paths_reject_symlink_to_workspace_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            workspace.mkdir()
            (workspace / "linked-project").symlink_to(workspace, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "stay within the workspace"):
                project_paths("linked-project", workspace)

            self.assertFalse((workspace / "data").exists())

    def test_workspace_api_rejects_prefix_sibling_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            workspace = base / "workspace"
            workspace.mkdir()
            prefix_sibling = base / "workspace-escape"
            prefix_sibling.mkdir()
            (prefix_sibling / "project_manifest.json").write_text("{}", encoding="utf-8")
            (workspace / "linked-project").symlink_to(prefix_sibling, target_is_directory=True)

            with self.assertRaises(ApiError) as caught:
                _project_root(workspace, "linked-project")

            self.assertEqual(400, caught.exception.status)

    def test_workspace_discovery_skips_symlinked_project_before_initialization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            workspace = base / "workspace"
            workspace.mkdir()
            outside = base / "outside"
            outside.mkdir()
            (outside / "project_manifest.json").write_text(
                json.dumps(
                    {
                        "project_id": "linked-project",
                        "profile": "research",
                        "root_path": str(outside),
                        "source": "https://example.test/video",
                        "status": "ingested",
                        "artifacts": {},
                    }
                ),
                encoding="utf-8",
            )
            (workspace / "linked-project").symlink_to(outside, target_is_directory=True)

            initialized = ensure_workspace_api_files(workspace)

            self.assertEqual([], initialized)
            self.assertFalse((outside / "data").exists())

    def test_valid_slug_stays_inside_workspace_and_creates_expected_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            paths = project_paths("valid-project-123", workspace)

            self.assertTrue(paths.root.is_relative_to(workspace.resolve()))
            self.assertTrue(paths.data.is_dir())
            self.assertTrue(paths.reports.is_dir())

    def test_generated_project_id_remains_a_supported_slug(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            project_id = create_project_id(f"{'a' * 200}.mp4")

            paths = project_paths(project_id, workspace)

            self.assertEqual(project_id, paths.root.name)
            self.assertTrue(paths.root.is_relative_to(workspace.resolve()))

    def test_manifest_deliverables_cannot_preview_files_outside_project(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            workspace = base / "workspace"
            project = workspace / "safe-project"
            project.mkdir(parents=True)
            outside = base / "outside.txt"
            outside.write_text("private-content", encoding="utf-8")
            (project / "project_manifest.json").write_text(
                json.dumps(
                    {
                        "project_id": "safe-project",
                        "profile": "research",
                        "root_path": str(project),
                        "source": "synthetic",
                        "status": "reported",
                        "artifacts": {
                            "absolute_leak": str(outside),
                            "relative_leak": "../../outside.txt",
                        },
                    }
                ),
                encoding="utf-8",
            )

            artifact_ids = {item[1] for item in _deliverable_specs(project)}
            self.assertNotIn("absolute_leak", artifact_ids)
            self.assertNotIn("relative_leak", artifact_ids)
            with self.assertRaises(ApiError) as caught:
                deliverable_preview(workspace, project, "absolute_leak")
            self.assertEqual(404, caught.exception.status)

    def test_media_payload_does_not_disclose_or_serve_outside_project_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            workspace = base / "workspace"
            project = workspace / "safe-project"
            project.mkdir(parents=True)
            outside = base / "private-video.mp4"
            outside.write_bytes(b"private")

            payload = _path_payload(workspace, project, outside)

            self.assertIsNone(payload["path"])
            self.assertIsNone(payload["relative_path"])
            self.assertIsNone(payload["url"])
            self.assertFalse(payload["present"])
            self.assertNotIn(str(outside), json.dumps(payload))


class SpreadsheetExportSafetyTest(unittest.TestCase):
    def test_csv_text_cells_are_neutralized_before_spreadsheet_export(self) -> None:
        shot = Shot(
            shot_id="shot-1",
            shot_no=1,
            start_time=0.0,
            end_time=1.0,
            duration=1.0,
            content_summary='=HYPERLINK("https://example.test","click")',
            prompt_en="@SUM(1+1)",
        )
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "shots.csv"
            write_shots_csv(target, [shot], AnalysisProfile.ads)
            with target.open(newline="", encoding="utf-8") as handle:
                row = next(csv.DictReader(handle))

        self.assertTrue(row["content_summary"].startswith("'="))
        self.assertTrue(row["prompt_en"].startswith("'@"))


class PathLockLifecycleTest(unittest.TestCase):
    def test_unique_path_locks_are_removed_after_use(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(500):
                with path_lock(root / f"item-{index}"):
                    pass
        with _PATH_LOCKS_GUARD:
            self.assertEqual({}, _PATH_LOCKS)

    def test_waiter_keeps_the_same_lock_entry_alive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "shared"
            key = os.path.abspath(os.fspath(path))
            holder_entered = threading.Event()
            release_holder = threading.Event()
            waiter_entered = threading.Event()

            def holder() -> None:
                with path_lock(path):
                    holder_entered.set()
                    release_holder.wait(timeout=5)

            def waiter() -> None:
                with path_lock(path):
                    waiter_entered.set()

            first = threading.Thread(target=holder)
            second = threading.Thread(target=waiter)
            first.start()
            self.assertTrue(holder_entered.wait(timeout=5))
            second.start()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with _PATH_LOCKS_GUARD:
                    users = _PATH_LOCKS.get(key).users if key in _PATH_LOCKS else 0
                if users == 2:
                    break
                time.sleep(0.01)
            self.assertEqual(2, users)
            release_holder.set()
            first.join(timeout=5)
            second.join(timeout=5)

        self.assertTrue(waiter_entered.is_set())
        with _PATH_LOCKS_GUARD:
            self.assertNotIn(key, _PATH_LOCKS)


class PasswordRedactionTest(unittest.TestCase):
    def test_timeout_redacts_password_from_command_and_stderr(self) -> None:
        secret = "timeout-secret"
        command = [
            sys.executable,
            "-c",
            "import sys,time; sys.stderr.write(sys.argv[2]); sys.stderr.flush(); time.sleep(1)",
            "--video-password",
            secret,
        ]

        with self.assertRaises(ToolError) as caught:
            run_command(command, timeout=0.05)

        rendered = str(caught.exception)
        self.assertNotIn(secret, rendered)
        self.assertIn("[REDACTED]", rendered)

    def test_failed_url_ingest_redacts_password_from_cli_error(self) -> None:
        secret = "cold-review-secret"
        with tempfile.TemporaryDirectory() as directory:
            source_file = Path(directory) / "source.txt"
            password_file = Path(directory) / "password.txt"
            source_file.write_text("https://example.test/video", encoding="utf-8")
            password_file.write_text(secret, encoding="utf-8")
            source_file.chmod(0o600)
            password_file.chmod(0o600)
            stderr = io.StringIO()
            with self._failed_external_tool(), redirect_stderr(stderr):
                exit_code = main(
                    [
                        "--workspace",
                        directory,
                        "ingest",
                        "--source-value-file",
                        str(source_file),
                        "--acknowledge-url-risk",
                        "--password-file",
                        str(password_file),
                        "--project-id",
                        "password-test",
                    ]
                )

        output = stderr.getvalue()
        self.assertEqual(1, exit_code)
        self.assertNotIn(secret, output)
        self.assertIn("[REDACTED]", output)
        self.assertEqual("error", json.loads(output)["status"])

    def test_cli_rejects_sensitive_url_or_plaintext_password_argv_before_dispatch(self) -> None:
        for arguments, secret in (
            (
                [
                    "ingest",
                    "https://example.test/private-path-token",
                    "--acknowledge-url-risk",
                ],
                "private-path-token",
            ),
            (
                [
                    "ingest",
                    "https://example.test/video",
                    "--acknowledge-url-risk",
                    "--password",
                    "argv-password",
                ],
                "argv-password",
            ),
        ):
            with self.subTest(arguments=arguments), tempfile.TemporaryDirectory() as directory:
                stderr = io.StringIO()
                with (
                    patch("video_analysis_mvp.cli.run_ingest_only") as dispatch,
                    redirect_stderr(stderr),
                ):
                    exit_code = main(["--workspace", directory, *arguments])
                self.assertEqual(1, exit_code)
                dispatch.assert_not_called()
                self.assertNotIn(secret, stderr.getvalue())
                self.assertEqual("error", json.loads(stderr.getvalue())["status"])

    def test_cli_private_value_files_preserve_signed_url_and_password(self) -> None:
        source = "https://example.test/video?token=private-query#private-fragment"
        password = "private-password"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_file = root / "source.txt"
            password_file = root / "password.txt"
            source_file.write_text(source, encoding="utf-8")
            password_file.write_text(password, encoding="utf-8")
            source_file.chmod(0o600)
            password_file.chmod(0o600)
            stdout = io.StringIO()
            result = StatusEnvelope(status="success", summary="accepted")
            with (
                patch(
                    "video_analysis_mvp.cli.run_ingest_only",
                    return_value=result,
                ) as dispatch,
                redirect_stdout(stdout),
            ):
                exit_code = main(
                    [
                        "--workspace",
                        directory,
                        "ingest",
                        "--source-value-file",
                        str(source_file),
                        "--password-file",
                        str(password_file),
                        "--acknowledge-url-risk",
                    ]
                )

        self.assertEqual(0, exit_code)
        self.assertEqual(source, dispatch.call_args.args[0])
        self.assertEqual(password, dispatch.call_args.kwargs["password"])

    @unittest.skipUnless(os.name == "posix", "owner-only mode is a POSIX contract")
    def test_cli_private_value_file_rejects_group_or_world_access(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source_file = Path(directory) / "source.txt"
            source_file.write_text("https://example.test/video", encoding="utf-8")
            source_file.chmod(0o644)
            stderr = io.StringIO()
            with (
                patch("video_analysis_mvp.cli.run_ingest_only") as dispatch,
                redirect_stderr(stderr),
            ):
                exit_code = main(
                    [
                        "--workspace",
                        directory,
                        "ingest",
                        "--source-value-file",
                        str(source_file),
                        "--acknowledge-url-risk",
                    ]
                )
        self.assertEqual(1, exit_code)
        dispatch.assert_not_called()
        self.assertIn("mode 0600", stderr.getvalue())

    def test_cli_value_file_rejects_multiple_lines_and_preserves_local_whitespace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source_file = Path(directory) / "source.txt"
            source_file.write_text("https://example.test/video\n\n", encoding="utf-8")
            source_file.chmod(0o600)
            stderr = io.StringIO()
            with (
                patch("video_analysis_mvp.cli.run_ingest_only") as dispatch,
                redirect_stderr(stderr),
            ):
                rejected = main(
                    [
                        "--workspace",
                        directory,
                        "ingest",
                        "--source-value-file",
                        str(source_file),
                        "--acknowledge-url-risk",
                    ]
                )
            self.assertEqual(1, rejected)
            dispatch.assert_not_called()

            local_source = " local file.mp4 "
            result = StatusEnvelope(status="success", summary="accepted")
            with patch(
                "video_analysis_mvp.cli.run_ingest_only",
                return_value=result,
            ) as local_dispatch:
                accepted = main(
                    ["--workspace", directory, "ingest", local_source]
                )
            self.assertEqual(0, accepted)
            self.assertEqual(local_source, local_dispatch.call_args.args[0])

    @unittest.skipUnless(os.name == "posix", "descriptor swap check uses POSIX mode")
    def test_cli_value_file_validates_and_reads_the_same_inode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "source.txt"
            replacement = Path(directory) / "replacement.txt"
            target.write_text("https://safe.example/video", encoding="utf-8")
            replacement.write_text("https://swapped.example/video", encoding="utf-8")
            target.chmod(0o600)
            replacement.chmod(0o644)
            real_open = os.open

            def swap_then_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
                os.replace(replacement, target)
                return real_open(path, flags, *args, **kwargs)

            with (
                patch("video_analysis_mvp.cli.os.open", side_effect=swap_then_open),
                self.assertRaisesRegex(ValueError, "mode 0600"),
            ):
                _read_private_value_file(
                    target,
                    label="Source value",
                    maximum=MAX_CLI_SOURCE_VALUE_BYTES,
                )

    def test_workspace_api_rejects_url_ingest_before_external_dispatch(self) -> None:
        secret = "cold-review-secret"
        body = json.dumps(
            {
                "source": "https://example.test/video",
                "password": secret,
                "project_id": "password-test",
            }
        ).encode("utf-8")

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ApiError) as caught:
                dispatch_api(Path(directory), "POST", "/api/projects", "", body)

        rendered = json.dumps(
            {"message": caught.exception.message, "details": caught.exception.details},
            ensure_ascii=False,
        )
        self.assertEqual(400, caught.exception.status)
        self.assertNotIn(secret, rendered)
        self.assertIn("CLI", rendered)
        self.assertIn("trusted operator", rendered)

    @staticmethod
    def _failed_external_tool():
        require_tool = patch("video_analysis_mvp.media.require_tool", return_value="/usr/bin/fake-tool")
        validate_target = patch("video_analysis_mvp.media._validate_initial_url_target", return_value=None)
        run_json = patch(
            "video_analysis_mvp.media.run_json",
            side_effect=ToolError("external tool rejected credentials: [REDACTED]"),
        )

        class Patches:
            def __enter__(self) -> None:
                require_tool.start()
                validate_target.start()
                run_json.start()

            def __exit__(self, *args: object) -> None:
                run_json.stop()
                validate_target.stop()
                require_tool.stop()

        return Patches()


if __name__ == "__main__":
    unittest.main()
