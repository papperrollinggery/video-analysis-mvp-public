from __future__ import annotations

import re
import unittest
from pathlib import Path


class ExportFrontendContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parents[1]
        cls.component = (root / "frontend/src/features/exports/ExportCenter.tsx").read_text(encoding="utf-8")
        cls.client = (root / "frontend/src/api/client.ts").read_text(encoding="utf-8")
        cls.styles = (root / "frontend/src/features/exports/exports.css").read_text(encoding="utf-8")
        cls.app = (root / "frontend/src/App.tsx").read_text(encoding="utf-8")

    def test_mount_reads_status_but_never_generates(self) -> None:
        effect = re.search(r"useEffect\(\(\) => \{(.*?)\}, \[projectId\]\);", self.component, re.DOTALL)
        self.assertIsNotNone(effect)
        self.assertIn("refresh(true)", effect.group(1))
        self.assertNotIn("generateClientExport", effect.group(1))
        self.assertIn('onClick={() => void generate()}', self.component)

    def test_generation_is_explicit_bounded_and_double_click_safe(self) -> None:
        self.assertIn('disabled={!allowed || actionLocked}', self.component)
        self.assertIn('selection === "both" ? ["xlsx", "pdf"]', self.component)
        self.assertIn("idempotency_key: key", self.component)
        self.assertIn("generationInFlight.current", self.component)
        self.assertIn('anotherProjectGenerating ? "another project exporting"', self.component)
        self.assertIn("timeoutMs: 5 * 60 * 1000", self.client)
        self.assertNotIn("setInterval(() => void generate", self.component)

    def test_progress_poll_is_serial_and_cancel_remains_available_during_generation(self) -> None:
        self.assertIn("getClientExportState", self.component)
        self.assertIn("window.setTimeout(() => void poll(), 1_200)", self.component)
        self.assertNotIn("window.setInterval", self.component)
        self.assertIn('busy !== null || cancelRequested', self.component)
        self.assertNotIn('busy !== "generate"', self.component)

    def test_project_switch_invalidates_old_payload_and_late_responses(self) -> None:
        self.assertIn("payloadRecord?.projectId === projectId", self.component)
        self.assertIn("refreshSequence.current === sequence", self.component)
        self.assertIn("setPayloadRecord(null)", self.component)
        self.assertIn("setConfirmDelete(null)", self.component)
        self.assertIn("mounted.current && currentProject.current === candidate", self.component)
        self.assertIn("changed(operationProject)", self.component)
        self.assertIn("if (isActiveProject(operationProject)) await onChanged?.()", self.component)

    def test_cancel_stale_saved_download_and_explicit_delete_states_are_visible(self) -> None:
        for marker in (
            "cancelClientExport",
            'current?.lifecycle_state === "current"',
            "saveClientExport",
            "deleteClientExport",
            "confirmDelete === item.version_id",
            "current.downloads[format]",
            "item.downloads[format]",
        ):
            self.assertIn(marker, self.component)
        self.assertIn('state?.status === "failed"', self.component)
        self.assertIn("recoverClientExports", self.component)

    def test_desktop_and_mobile_surfaces_share_one_component(self) -> None:
        self.assertGreaterEqual(self.app.count("<ExportCenter"), 2)
        self.assertIn("compact", self.app)
        self.assertIn("onChanged={() => refresh(false)}", self.app)
        self.assertIn("@media (max-width: 900px)", self.styles)
        self.assertIn("@media (max-width: 760px)", self.styles)
        self.assertIn("min-height: 44px", self.styles)
        self.assertIn("input:focus-visible", self.styles)
        self.assertIn("audio_review_complete", self.app)
        self.assertIn("Audio timeline unavailable · unknown, not silence", self.app)

    def test_stale_and_saved_packages_remain_downloadable_with_truthful_labels(self) -> None:
        self.assertIn("Historical package · not current", self.component)
        self.assertIn("Stale—inspect only; do not present as current", self.component)
        self.assertIn("Download saved", self.component)
        self.assertNotIn("{allowed ? item.formats.map", self.component)

    def test_api_client_accepts_no_runtime_paths_or_credentials(self) -> None:
        generate = re.search(
            r"export function generateClientExport\((.*?)\n\}",
            self.client,
            re.DOTALL,
        )
        self.assertIsNotNone(generate)
        body = generate.group(1)
        self.assertIn("formats: ExportFormat[]", body)
        self.assertIn("settings: Record<string, string>", body)
        self.assertIn("idempotency_key: string", body)
        for forbidden in ("browser_path", "node_modules", "font_path", "api_key", "token"):
            self.assertNotIn(forbidden, body)


if __name__ == "__main__":
    unittest.main()
