from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_audio_review import audio_review_fixture, ready_client_export_fixture
from video_analysis_mvp.cli import main
from video_analysis_mvp.workspace_api import ApiError, dispatch_api

OPENPYXL_AVAILABLE = importlib.util.find_spec("openpyxl") is not None


@unittest.skipUnless(OPENPYXL_AVAILABLE, "XLSX export runtime is unavailable")
class ExportCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="export-cli-")
        self.addCleanup(self.temp.cleanup)
        self.paths = ready_client_export_fixture(Path(self.temp.name))

    def command(self, *args: str) -> tuple[int, dict[str, object]]:
        output = io.StringIO()
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("VEW_PDF_")
        }
        with patch.dict(os.environ, environment, clear=True), contextlib.redirect_stdout(output):
            status = main(["--workspace", str(self.paths.root.parent), "export", *args])
        return status, json.loads(output.getvalue())

    def test_generate_status_save_and_delete_are_explicit_actions(self) -> None:
        self.assertFalse((self.paths.reports / "client").exists())

        status, generated = self.command(
            "generate",
            self.paths.root.name,
            "--format",
            "xlsx",
            "--idempotency-key",
            "cli-generate-1",
            "--language",
            "bilingual",
        )
        self.assertEqual(0, status)
        self.assertEqual(["xlsx"], generated["formats"])
        self.assertTrue((self.paths.reports / "client" / "current" / "client_breakdown.xlsx").is_file())
        self.assertFalse(list(self.paths.root.rglob("*.pdf")))

        status, state = self.command("status", self.paths.root.name)
        self.assertEqual(0, status)
        self.assertEqual("current", state["status"])

        status, saved = self.command("save", self.paths.root.name, "client-v1")
        self.assertEqual(0, status)
        self.assertEqual(generated["export_id"], saved["export_id"])

        status, deleted = self.command("delete", self.paths.root.name, "client-v1")
        self.assertEqual(0, status)
        self.assertEqual("deleted", deleted["status"])

    def test_workspace_api_uses_the_same_export_service_and_strict_body(self) -> None:
        body = json.dumps(
            {
                "formats": ["xlsx"],
                "settings": {"language": "bilingual"},
                "idempotency_key": "api-generate-1",
            }
        ).encode()
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("VEW_PDF_")
        }
        with patch.dict(os.environ, environment, clear=True):
            status, generated = dispatch_api(
                self.paths.root.parent,
                "POST",
                f"/api/projects/{self.paths.root.name}/exports",
                "",
                body,
            )
        self.assertEqual(200, status)
        self.assertEqual(["xlsx"], generated["formats"])

        status, state = dispatch_api(
            self.paths.root.parent,
            "GET",
            f"/api/projects/{self.paths.root.name}/exports",
            "",
            b"{}",
        )
        self.assertEqual(200, status)
        self.assertEqual("current", state["state"]["status"])
        self.assertEqual("current", state["current"]["lifecycle_state"])

        with self.assertRaises(ApiError) as caught:
            dispatch_api(
                self.paths.root.parent,
                "POST",
                f"/api/projects/{self.paths.root.name}/exports",
                "",
                b'{"formats":["xlsx"],"settings":{},"idempotency_key":"bad","browser_path":"forbidden"}',
            )
        self.assertEqual(400, caught.exception.status)

        dispatch_api(
            self.paths.root.parent,
            "POST",
            f"/api/projects/{self.paths.root.name}/exports/save",
            "",
            b'{"version_id":"api-v1"}',
        )
        with self.assertRaises(ApiError) as caught:
            dispatch_api(
                self.paths.root.parent,
                "DELETE",
                f"/api/projects/{self.paths.root.name}/exports/saved/api-v1",
                "",
                b'{"confirm":false,"unexpected":"ignored"}',
            )
        self.assertEqual(400, caught.exception.status)
        self.assertTrue((self.paths.reports / "client" / "saved" / "api-v1").is_dir())

    def test_cli_missing_project_never_creates_ghost_directories(self) -> None:
        ghost = self.paths.root.parent / "ghost-project"
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            status = main(
                [
                    "--workspace",
                    str(self.paths.root.parent),
                    "export",
                    "generate",
                    ghost.name,
                    "--format",
                    "xlsx",
                    "--idempotency-key",
                    "ghost-key",
                ]
            )
        self.assertEqual(1, status)
        self.assertFalse(ghost.exists())

    def test_cli_rejects_unreviewed_audio_without_creating_a_current_package(self) -> None:
        blocked = audio_review_fixture(
            Path(self.temp.name) / "blocked",
            professionally_ready=True,
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            status = main(
                [
                    "--workspace",
                    str(blocked.root.parent),
                    "export",
                    "generate",
                    blocked.root.name,
                    "--format",
                    "xlsx",
                    "--idempotency-key",
                    "blocked-unreviewed-audio",
                ]
            )

        self.assertEqual(1, status)
        self.assertIn("audio event", stderr.getvalue())
        self.assertFalse((blocked.reports / "client" / "current").exists())
        self.assertFalse(list(blocked.root.rglob("*.xlsx")))


if __name__ == "__main__":
    unittest.main()
