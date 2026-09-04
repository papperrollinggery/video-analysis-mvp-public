from __future__ import annotations

import importlib.util
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from tests.test_audio_review import audio_review_fixture
from video_analysis_mvp.client_export_dataset import build_client_export_dataset
from video_analysis_mvp.export_xlsx import render_client_xlsx
from video_analysis_mvp.utils import run_command

OFFICE = shutil.which("libreoffice") or shutil.which("soffice")
PYPDF_AVAILABLE = importlib.util.find_spec("pypdf") is not None
if os.environ.get("VEW_REQUIRE_OFFICE_RUNTIME") == "1" and not (
    OFFICE and PYPDF_AVAILABLE
):
    raise RuntimeError(
        "VEW_REQUIRE_OFFICE_RUNTIME=1 but LibreOffice and pypdf are unavailable"
    )


@unittest.skipUnless(
    OFFICE and PYPDF_AVAILABLE,
    "LibreOffice visual acceptance runtime is unavailable",
)
class LibreOfficeExportAcceptanceTest(unittest.TestCase):
    def test_xlsx_opens_as_bounded_searchable_landscape_pages(self) -> None:
        from pypdf import PdfReader

        with tempfile.TemporaryDirectory(prefix="vew-office-acceptance-") as directory:
            root = Path(directory)
            paths = audio_review_fixture(root)
            dataset = build_client_export_dataset(paths)
            workbook = root / "client-breakdown.xlsx"
            converted = root / "converted"
            converted.mkdir(mode=0o700)
            private_home = root / "home"
            private_home.mkdir(mode=0o700)
            render_client_xlsx(
                dataset,
                workbook,
                settings={"language": "bilingual"},
                project_root=paths.root,
            )
            environment = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": str(private_home),
                "TMPDIR": str(private_home),
            }
            run_command(
                [
                    str(OFFICE),
                    "--headless",
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    str(converted),
                    str(workbook),
                ],
                timeout=120,
                environment=environment,
                max_output_bytes=2 * 1024 * 1024,
            )
            rendered = converted / "client-breakdown.pdf"
            self.assertTrue(rendered.is_file())
            reader = PdfReader(rendered)
            self.assertGreaterEqual(len(reader.pages), 1)
            self.assertLessEqual(len(reader.pages), 20)
            self.assertTrue(
                all(
                    float(page.mediabox.width) > float(page.mediabox.height)
                    for page in reader.pages
                )
            )
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
            self.assertIn(dataset["project"]["title"]["text"], text)
            self.assertIn("shot_0001", text)
            self.assertIn("Original VO", text)


if __name__ == "__main__":
    unittest.main()
