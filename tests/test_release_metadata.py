from __future__ import annotations

import json
import re
import tomllib
import unittest
from pathlib import Path


class ReleaseMetadataTest(unittest.TestCase):
    def test_v030_release_metadata_is_consistent(self) -> None:
        repo = Path(__file__).parents[1]
        expected = "0.3.0"

        project = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        self.assertEqual(expected, project["version"])

        frontend = json.loads((repo / "frontend" / "package.json").read_text(encoding="utf-8"))
        frontend_lock = json.loads((repo / "frontend" / "package-lock.json").read_text(encoding="utf-8"))
        self.assertEqual(expected, frontend["version"])
        self.assertEqual(expected, frontend_lock["version"])
        self.assertEqual(expected, frontend_lock["packages"][""]["version"])

        uv_lock = tomllib.loads((repo / "uv.lock").read_text(encoding="utf-8"))
        local_package = next(item for item in uv_lock["package"] if item["name"] == "video-analysis-mvp")
        self.assertEqual(expected, local_package["version"])

        package_init = (repo / "src" / "video_analysis_mvp" / "__init__.py").read_text(encoding="utf-8")
        self.assertRegex(package_init, rf'__version__ = "{re.escape(expected)}"')
        skill = repo / ".agents" / "skills" / "video-evidence-workbench"
        self.assertRegex((skill / "SKILL.md").read_text(), rf'(?m)^  version: "{re.escape(expected)}"$')
        for helper in ("install.py", "workbench.py"):
            self.assertRegex((skill / "scripts" / helper).read_text(), rf'(?m)^EXPECTED_VERSION = "{re.escape(expected)}"$')
        vision = (repo / "src" / "video_analysis_mvp" / "vision.py").read_text(encoding="utf-8")
        self.assertIn(f'"version": "{expected}"', vision)

        citation = (repo / "CITATION.cff").read_text(encoding="utf-8")
        self.assertRegex(citation, rf"(?m)^version: {re.escape(expected)}$")
        self.assertRegex(citation, r"(?m)^date-released: 2026-09-05$")

        self.assertTrue((repo / "docs" / "releases" / f"v{expected}.md").is_file())
        changelog = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn(f"## [{expected}] - 2026-09-05", changelog)
        self.assertIn(f"[{expected}]: https://github.com/papperrollinggery/video-analysis-mvp-public/releases/tag/v{expected}", changelog)

        readme = (repo / "README.md").read_text(encoding="utf-8")
        self.assertIn(f"Project status: v{expected} pre-1.0 release candidate", readme)
        self.assertIn(f"scripts/verify-candidate-receipt.sh v{expected}", readme)
        release_language = "\n".join(
            [
                readme,
                changelog,
                (repo / "docs" / "quality-metrics.md").read_text(encoding="utf-8"),
                (repo / "docs" / "release-readiness.md").read_text(encoding="utf-8"),
                (repo / "docs" / "releases" / "v0.2.1.md").read_text(encoding="utf-8"),
                (repo / "docs" / "releases" / f"v{expected}.md").read_text(encoding="utf-8"),
            ]
        )
        self.assertNotIn("immutable release tag", release_language)
        self.assertNotIn("immutable Git tree", release_language)

        mature = json.loads(
            (repo / "docs" / "evidence" / "mature-candidate-receipt.json").read_text(encoding="utf-8")
        )
        self.assertEqual(f"v{expected}_release_candidate_pre_push", mature["status"])
        self.assertNotIn("final_independent_verdict", mature["review"])
        current_review = mature["review"][f"v{expected}"]
        self.assertEqual("approved", current_review["status"])
        self.assertRegex(current_review["verdict"], r"^APPROVE:")


if __name__ == "__main__":
    unittest.main()
