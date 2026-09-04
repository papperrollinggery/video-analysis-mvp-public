from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path


class ArtifactAndFixturePolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1]

    def test_fixture_manifest_is_complete_and_contains_no_external_media(self) -> None:
        manifest = json.loads(
            (self.root / "tests/fixtures/manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(1, manifest["schema_version"])
        fixtures = manifest["fixtures"]
        self.assertEqual(3, len(fixtures))
        self.assertEqual(3, len({item["id"] for item in fixtures}))
        for item in fixtures:
            self.assertEqual("CC0-1.0", item["license"])
            self.assertFalse(item["retained_media"])
            self.assertIn("none", item["source_assets"])
            self.assertEqual("Video Evidence Workbench contributors", item["rights_holder"])
            self.assertTrue(item["generator_version"].endswith("-v1"))
            self.assertTrue(item["generation_command"])
            self.assertIn("CC0-1.0", item["output_license_declaration"])
            self.assertIn("no third-party media", item["output_license_declaration"])
            self.assertTrue((self.root / item["generator"].split("#", 1)[0]).is_file())
        self.assertIs(manifest["private_reference_policy"]["repository_media"], False)
        self.assertIs(manifest["private_reference_policy"]["repository_hash"], False)

    def test_release_candidate_and_diagnostics_pass_cleanup_policy(self) -> None:
        result = subprocess.run(
            ["sh", str(self.root / "scripts/audit-test-artifacts.sh")],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr or result.stdout)
        self.assertIn("artifact cleanup policy ok", result.stdout)


if __name__ == "__main__":
    unittest.main()
