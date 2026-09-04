from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_audio_review import audio_review_fixture
from tests.test_evidence_handoff import PNG_1X1
from video_analysis_mvp.client_export_dataset import (
    _canonical_digest,
    build_client_export_dataset,
)
from video_analysis_mvp.export_templates import (
    ExportTemplateError,
    load_client_template,
    preflight_client_layout,
)


class ExportTemplateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="vew-template-")
        self.addCleanup(self.temp.cleanup)
        self.paths = audio_review_fixture(Path(self.temp.name))
        self.dataset = build_client_export_dataset(self.paths)

    def redigest(self) -> None:
        base = {key: value for key, value in self.dataset.items() if key not in {"dataset_id", "dataset_digest"}}
        digest = _canonical_digest(base)
        self.dataset["dataset_id"] = self.dataset["dataset_digest"] = digest

    def test_template_files_and_digest_are_deterministic_and_fixed(self):
        first = load_client_template()
        second = load_client_template()
        self.assertEqual(first, second)
        self.assertEqual("client-storyboard", first["template_id"])
        self.assertEqual("1.0.0", first["template_version"])
        self.assertEqual("client-export-dataset/v1", first["compatible_dataset_schemas"][0])
        self.assertEqual(3, len(first["asset_digests"]))
        self.assertRegex(first["template_digest"], r"^[0-9a-f]{64}$")
        self.assertEqual(8.5, first["design_tokens"]["typography"]["minimum_pt"])

    def test_branding_is_bounded_and_does_not_mutate_template(self):
        template = load_client_template()
        before = json.dumps(template, sort_keys=True)
        result = preflight_client_layout(
            self.dataset,
            {
                "language": "bilingual",
                "density": "compact",
                "formats": ["xlsx"],
                "project_subtitle": "=Campaign launch",
                "accent_color": "#165DCC",
            },
            project_root=self.paths.root,
        )
        self.assertEqual("ready", result["status"])
        self.assertTrue(result["settings"]["project_subtitle"]["formula_neutralized"])
        self.assertEqual("#165DCC", result["settings"]["accent_color"])
        self.assertEqual(before, json.dumps(template, sort_keys=True))
        with self.assertRaises(ExportTemplateError):
            preflight_client_layout(self.dataset, {"language": "fr"}, project_root=self.paths.root)
        with self.assertRaisesRegex(ExportTemplateError, "contrast"):
            preflight_client_layout(self.dataset, {"accent_color": "#FFFFFF"}, project_root=self.paths.root)
        with self.assertRaisesRegex(ExportTemplateError, "subtitle"):
            preflight_client_layout(
                self.dataset,
                {"formats": ["xlsx"], "project_subtitle": "x" * 200_000},
                project_root=self.paths.root,
            )
        with self.assertRaisesRegex(ExportTemplateError, "CJK font"):
            preflight_client_layout(
                self.dataset,
                {"formats": ["pdf"], "language": "en", "project_subtitle": "中文副标题"},
                project_root=self.paths.root,
            )

    def test_logo_is_local_bound_or_explicitly_omitted(self):
        omitted = preflight_client_layout(self.dataset, {"formats": ["xlsx"]}, project_root=self.paths.root)
        self.assertIsNone(omitted["settings"]["logo"])
        self.assertIn("logo omitted", " ".join(omitted["warnings"]))
        logo = self.paths.assets / "client-logo.png"
        logo.write_bytes(PNG_1X1)
        result = preflight_client_layout(
            self.dataset, {"formats": ["xlsx"], "logo_path": str(logo)}, project_root=self.paths.root
        )
        self.assertEqual("assets/client-logo.png", result["settings"]["logo"]["path"])
        self.assertEqual("image/png", result["settings"]["logo"]["media_type"])
        outside = Path(self.temp.name) / "outside.png"
        outside.write_bytes(PNG_1X1)
        with self.assertRaisesRegex(ExportTemplateError, "project root"):
            preflight_client_layout(self.dataset, {"logo_path": str(outside)}, project_root=self.paths.root)
        with self.assertRaisesRegex(ExportTemplateError, "logo is missing"):
            preflight_client_layout(
                self.dataset,
                {"logo_path": str(self.paths.assets / "missing.png")},
                project_root=self.paths.root,
            )

    def test_cjk_pdf_font_is_explicit_while_xlsx_uses_declared_fallback(self):
        self.dataset["shots"][0]["text"]["content_summary"]["text"] = "中文客户说明"
        self.dataset["shots"][0]["text"]["content_summary"]["spreadsheet_text"] = "中文客户说明"
        self.dataset["shots"][0]["text"]["content_summary"]["is_blank"] = False
        self.redigest()
        xlsx = preflight_client_layout(self.dataset, {"formats": ["xlsx"]}, project_root=self.paths.root)
        self.assertEqual("ready", xlsx["status"])
        self.assertTrue(any("CJK font" in item for item in xlsx["warnings"]))
        with self.assertRaisesRegex(ExportTemplateError, "CJK font"):
            preflight_client_layout(self.dataset, {"formats": ["pdf"]}, project_root=self.paths.root)
        with self.assertRaisesRegex(ExportTemplateError, "CJK font"):
            preflight_client_layout(self.dataset, {"formats": ["pdf"]}, project_root=self.paths.root, available_fonts=["Arial"])
        pdf = preflight_client_layout(
            self.dataset,
            {"formats": ["pdf"]},
            project_root=self.paths.root,
            available_fonts=["Noto Sans CJK SC"],
        )
        self.assertEqual("Noto Sans CJK SC", pdf["font_plan"]["selected_cjk_font"])

    def test_long_content_continues_or_fails_without_shrinking(self):
        cell = self.dataset["shots"][0]["text"]["content_summary"]
        cell.update(text="中" * 20_000, spreadsheet_text="中" * 20_000, is_blank=False)
        self.redigest()
        result = preflight_client_layout(
            self.dataset, {"formats": ["xlsx"], "density": "client"}, project_root=self.paths.root
        )
        shot = result["shot_plan"][0]
        self.assertGreater(shot["continuation_blocks"], 0)
        self.assertEqual(8.5, result["typography"]["minimum_font_pt"])
        cell.update(text=("line\n" * 1000), spreadsheet_text=("line\n" * 1000))
        self.redigest()
        newline_plan = preflight_client_layout(self.dataset, {"formats": ["xlsx"]}, project_root=self.paths.root)
        self.assertGreater(newline_plan["shot_plan"][0]["continuation_blocks"], 0)
        cell.update(text="中" * 80_000, spreadsheet_text="中" * 80_000)
        self.redigest()
        with self.assertRaisesRegex(ExportTemplateError, "continuation"):
            preflight_client_layout(self.dataset, {"formats": ["xlsx"]}, project_root=self.paths.root)

    def test_missing_frame_and_outputs_are_planned_not_rendered(self):
        self.dataset["shots"][0]["frame"] = {
            "path": None,
            "present": False,
            "sha256": None,
            "size_bytes": None,
            "media_type": None,
            "width": None,
            "height": None,
            "failure": {
                "text": "Frame unavailable",
                "spreadsheet_text": "Frame unavailable",
                "is_blank": False,
                "formula_neutralized": False,
            },
        }
        self.redigest()
        before = set(self.paths.root.rglob("*"))
        result = preflight_client_layout(self.dataset, {"formats": ["xlsx"]}, project_root=self.paths.root)
        self.assertEqual("missing-frame", result["shot_plan"][0]["frame_variant"])
        self.assertEqual(before, set(self.paths.root.rglob("*")))
        self.assertFalse(list(self.paths.root.rglob("*.xlsx")))
        self.assertFalse(list(self.paths.root.rglob("*.pdf")))

    def test_per_image_limit_and_settings_types_fail_cleanly(self):
        self.dataset["shots"][0]["frame"].update(
            present=True,
            path="assets/keyframes/frame.jpg",
            sha256="a" * 64,
            size_bytes=17 * 1024 * 1024,
            media_type="image/png",
            width=1,
            height=1,
            failure={"text": "", "spreadsheet_text": "", "is_blank": True, "formula_neutralized": False},
        )
        self.redigest()
        with self.assertRaisesRegex(ExportTemplateError, "per-image"):
            preflight_client_layout(self.dataset, {"formats": ["xlsx"]}, project_root=self.paths.root)
        clean = build_client_export_dataset(self.paths)
        for settings in ({"language": []}, {"formats": [{}]}, {"logo_path": []}, {"accent_color": 0}, {"accent_color": False}):
            with self.subTest(settings=settings), self.assertRaises(ExportTemplateError):
                preflight_client_layout(clean, settings, project_root=self.paths.root)
        audio_cell = clean["audio"]["events"][0]["original_proposal"]["text"]
        audio_cell.update(text="a" * 20_000, spreadsheet_text="a" * 20_000, is_blank=False)
        self.dataset = clean
        self.redigest()
        plan = preflight_client_layout(clean, {"formats": ["xlsx"]}, project_root=self.paths.root)
        self.assertGreater(plan["metrics"]["non_storyboard_continuation_block_count"], 0)
        self.assertEqual(
            plan["metrics"]["continuation_block_count"],
            plan["metrics"]["storyboard_continuation_block_count"] + plan["metrics"]["non_storyboard_continuation_block_count"],
        )

    def test_xlsx_capacity_is_bounded_to_the_measured_renderer_envelope(self):
        template = load_client_template()
        limits = template["layout"]["renderer_limits"]["xlsx"]
        self.assertEqual(3_200, limits["maximum_shots"])
        self.assertEqual(8_000, limits["maximum_audio_events"])
        oversized = {"shots": [{}] * 3_201, "audio": {"events": []}}
        with patch(
            "video_analysis_mvp.export_templates.validate_client_export_dataset",
            return_value=oversized,
        ), self.assertRaisesRegex(ExportTemplateError, "XLSX renderer capacity"):
            preflight_client_layout({}, {"formats": ["xlsx"]}, project_root=self.paths.root)


if __name__ == "__main__":
    unittest.main()
