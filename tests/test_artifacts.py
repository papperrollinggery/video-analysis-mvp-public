from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from video_analysis_mvp.artifacts import (
    ADS_ONLY_REPORT_ARTIFACT_IDS,
    ARTIFACT_SPECS,
    PROFESSIONAL_EXPORT_IDS,
    REPORT_ARTIFACT_RELATIVE_PATHS,
    artifact_path,
    artifact_spec,
    empty_artifact_registry,
    iter_report_artifacts,
    iter_workspace_artifacts,
    load_artifact_registry,
    mark_artifacts_stale,
    record_committed_report_artifacts,
    register_artifact,
    transition_artifact,
    validate_artifact_registry,
)
from video_analysis_mvp.paths import ProjectPaths
from video_analysis_mvp.workspace_api import ApiError, _invalidate_report_for_review
from video_analysis_mvp.web import project_artifacts


SHA256 = "a" * 64
EXPECTED_REPORT_PATHS = {
    "overview_pdf": "reports/overview.pdf",
    "report_html": "reports/report.html",
    "storyboard_html": "reports/storyboard.html",
    "shot_list_csv": "reports/shot_list.csv",
    "profile_analysis_html": "reports/profile_analysis.html",
    "shot_breakdown_csv": "reports/shot_breakdown.csv",
    "shot_table_csv": "reports/shot_table.csv",
    "lineage_json": "data/lineage.json",
    "readiness_json": "data/readiness.json",
    "boundary_review_json": "data/boundary_review.json",
    "transcript_srt": "reports/transcript.srt",
    "music_rhythm_summary": "reports/music_rhythm_summary.json",
    "contact_sheet": "assets/contact_sheet.jpg",
    "keyframes": "assets/keyframes",
    "project_manifest": "project_manifest.json",
    "codex_handoff": "reports/codex_handoff.md",
    "visualization_dataset": "data/visualization_dataset.json",
    "remake_brief": "reports/remake_brief.md",
    "branch_board_html": "reports/branch_board.html",
    "prompt_reverse_engineering": "reports/prompt_reverse_engineering.md",
    "model_prompt_pack": "reports/model_prompt_pack.json",
    "revision_plan": "reports/revision_plan.md",
}


def _digest() -> dict[str, object]:
    return {"algorithm": "sha256-file-v1", "sha256": SHA256, "size_bytes": 12}


def _record(
    *,
    artifact_id: str = "client_breakdown_xlsx",
    scope: str = "client_export",
    relative_path: str = "reports/client/current/client_breakdown.xlsx",
    state: str = "current",
    retention: str = "current",
    stale_reason: str | None = None,
) -> dict[str, object]:
    durable = state in {"current", "saved", "stale", "superseded"}
    return {
        "artifact_id": artifact_id,
        "scope": scope,
        "kind": "file",
        "relative_path": relative_path,
        "state": state,
        "retention": retention,
        "generation_id": "export-generation-1" if durable else None,
        "source_generation_id": "report-generation-1" if durable else None,
        "digest": _digest() if durable else None,
        "stale_reason": stale_reason,
    }


class StaticArtifactCatalogTest(unittest.TestCase):
    def test_catalog_ids_paths_and_report_contract_are_unique(self) -> None:
        ids = [spec.artifact_id for spec in ARTIFACT_SPECS]
        paths = [spec.relative_path for spec in ARTIFACT_SPECS]

        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(paths), len(set(paths)))
        self.assertEqual(
            EXPECTED_REPORT_PATHS,
            dict(REPORT_ARTIFACT_RELATIVE_PATHS),
        )
        self.assertEqual("manifest", artifact_spec("project_manifest").kind)
        self.assertEqual("directory", artifact_spec("keyframes").kind)
        self.assertEqual("reports/client/current", artifact_spec("client_current_package").relative_path)

    def test_profile_and_professional_export_policies_preserve_existing_behavior(self) -> None:
        research_reports = {spec.artifact_id for spec in iter_report_artifacts("research")}
        ads_reports = {spec.artifact_id for spec in iter_report_artifacts("ads")}
        research_workspace = {spec.artifact_id for spec in iter_workspace_artifacts("research")}
        ads_workspace = {spec.artifact_id for spec in iter_workspace_artifacts("ads")}

        self.assertTrue(ADS_ONLY_REPORT_ARTIFACT_IDS.isdisjoint(research_reports))
        self.assertTrue(ADS_ONLY_REPORT_ARTIFACT_IDS.issubset(ads_reports))
        self.assertTrue(ADS_ONLY_REPORT_ARTIFACT_IDS.isdisjoint(research_workspace))
        self.assertTrue(ADS_ONLY_REPORT_ARTIFACT_IDS.issubset(ads_workspace))
        self.assertEqual(
            {"profile_analysis_html", "remake_brief", "model_prompt_pack"},
            set(PROFESSIONAL_EXPORT_IDS),
        )

    def test_unknown_artifact_id_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unknown artifact id"):
            artifact_spec("unknown_alias")


class ArtifactRegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="artifact-registry-")
        self.paths = ProjectPaths(Path(self.temporary.name) / "registry-project")
        self.paths.ensure()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_missing_registry_is_a_read_only_legacy_empty_state(self) -> None:
        registry = load_artifact_registry(self.paths)

        self.assertEqual(empty_artifact_registry("registry-project"), registry)
        self.assertFalse((self.paths.data / "artifact_registry.json").exists())

    def test_schema_rejects_unknown_fields_unsafe_paths_duplicates_and_nonfinite_json(self) -> None:
        base = empty_artifact_registry("registry-project")
        unknown = dict(base)
        unknown["extra"] = True
        with self.assertRaisesRegex(ValueError, "exactly"):
            validate_artifact_registry(unknown, project_id="registry-project")

        boolean_revision = dict(base)
        boolean_revision["revision"] = True
        with self.assertRaisesRegex(ValueError, "non-negative integer"):
            validate_artifact_registry(boolean_revision, project_id="registry-project")

        boolean_schema = dict(base)
        boolean_schema["schema_version"] = True
        with self.assertRaisesRegex(ValueError, "schema version"):
            validate_artifact_registry(boolean_schema, project_id="registry-project")

        unsafe = dict(base)
        unsafe["artifacts"] = [_record(relative_path="reports/client/../outside.xlsx")]
        with self.assertRaisesRegex(ValueError, "unsafe"):
            validate_artifact_registry(unsafe, project_id="registry-project")

        duplicate = dict(base)
        duplicate["artifacts"] = [_record(), _record()]
        with self.assertRaisesRegex(ValueError, "duplicate artifact_id"):
            validate_artifact_registry(duplicate, project_id="registry-project")

        unknown_entry = dict(base)
        unknown_entry["artifacts"] = [
            _record(
                artifact_id="unknown_analysis",
                scope="analysis",
                relative_path="data/unknown.json",
            )
        ]
        with self.assertRaisesRegex(ValueError, "unknown artifact ids"):
            validate_artifact_registry(unknown_entry, project_id="registry-project")

        unhashable_enum = dict(base)
        invalid_record = _record()
        invalid_record["scope"] = []
        unhashable_enum["artifacts"] = [invalid_record]
        with self.assertRaisesRegex(ValueError, "scope is unsupported"):
            validate_artifact_registry(unhashable_enum, project_id="registry-project")

        registry_path = self.paths.data / "artifact_registry.json"
        registry_path.write_text(
            '{"schema_version":1,"project_id":"registry-project","revision":NaN,"artifacts":[]}',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "unreadable"):
            load_artifact_registry(self.paths)

    def test_state_machine_is_explicit_and_revision_checked(self) -> None:
        staging = _record(state="staging", retention="transient")
        created = register_artifact(self.paths, staging)
        self.assertEqual(1, created["revision"])

        with self.assertRaisesRegex(ValueError, "revision conflict"):
            transition_artifact(
                self.paths,
                "client_breakdown_xlsx",
                "current",
                expected_revision=0,
            )
        with self.assertRaisesRegex(ValueError, "non-negative integer"):
            transition_artifact(
                self.paths,
                "client_breakdown_xlsx",
                "current",
                expected_revision=True,
                retention="current",
                generation_id="export-generation-1",
                source_generation_id="report-generation-1",
                digest=_digest(),
            )

        current = transition_artifact(
            self.paths,
            "client_breakdown_xlsx",
            "current",
            expected_revision=1,
            retention="current",
            generation_id="export-generation-1",
            source_generation_id="report-generation-1",
            digest=_digest(),
        )
        self.assertEqual("current", current["artifacts"][0]["state"])
        stale = transition_artifact(
            self.paths,
            "client_breakdown_xlsx",
            "stale",
            stale_reason="source_report_generation_changed",
        )
        self.assertEqual("stale", stale["artifacts"][0]["state"])
        self.assertEqual("source_report_generation_changed", stale["artifacts"][0]["stale_reason"])

        superseded = transition_artifact(self.paths, "client_breakdown_xlsx", "superseded")
        self.assertEqual("superseded", superseded["artifacts"][0]["state"])
        with self.assertRaisesRegex(ValueError, "Invalid artifact state transition"):
            transition_artifact(self.paths, "client_breakdown_xlsx", "current")
        with self.assertRaisesRegex(ValueError, "stale_reason"):
            transition_artifact(
                self.paths,
                "client_breakdown_xlsx",
                "superseded",
                stale_reason="must-not-be-ignored",
            )

        saved_version = _record(
            artifact_id="client_export.saved.version-1",
            relative_path="reports/client/saved/version-1",
            state="saved",
            retention="saved",
        )
        saved_version["kind"] = "package"
        saved = register_artifact(self.paths, saved_version)
        saved_by_id = {item["artifact_id"]: item for item in saved["artifacts"]}
        self.assertEqual("saved", saved_by_id["client_export.saved.version-1"]["state"])

    def test_mark_stale_is_idempotent_and_never_generates_an_export(self) -> None:
        register_artifact(self.paths, _record())
        before_files = {
            path.relative_to(self.paths.root).as_posix()
            for path in self.paths.root.rglob("*")
            if path.is_file()
        }

        first = mark_artifacts_stale(
            self.paths,
            scopes={"client_export"},
            reason="shot_review_saved",
        )
        second = mark_artifacts_stale(
            self.paths,
            scopes={"client_export"},
            reason="shot_review_saved",
        )

        self.assertEqual("stale", first["artifacts"][0]["state"])
        self.assertEqual(first, second)
        after_files = {
            path.relative_to(self.paths.root).as_posix()
            for path in self.paths.root.rglob("*")
            if path.is_file()
        }
        self.assertEqual(before_files, after_files)
        self.assertFalse(artifact_path(self.paths.root, "client_breakdown_xlsx").exists())

    def test_committed_report_records_digests_and_stales_current_client_export(self) -> None:
        register_artifact(self.paths, _record())
        report_path = artifact_path(self.paths.root, "report_html")
        manifest_path = artifact_path(self.paths.root, "project_manifest")
        manifest = {
            "project_id": self.paths.root.name,
            "profile": "research",
            "root_path": str(self.paths.root),
            "source": "synthetic.mp4",
            "status": "reported",
            "artifacts": {
                "report_html": str(report_path),
                "project_manifest": str(manifest_path),
            },
            "report_generation": {
                "schema_version": 3,
                "generation_id": "report-generation-2",
                "run_id": "report-generation-2",
                "state": "committed",
                "digest_algorithm": "sha256",
                "source_receipts": {"audio_generation": {"generation_id": "audio-1"}},
                "artifact_digests": {
                    "report_html": {
                        "path": str(report_path),
                        "kind": "file",
                        "digest_mode": "sha256-file-v1",
                        "sha256": "b" * 64,
                        "size_bytes": 42,
                    },
                    "project_manifest": {
                        "path": str(manifest_path),
                        "kind": "manifest",
                        "digest_mode": "canonical-json-without-report-generation-v1",
                        "sha256": "c" * 64,
                        "size_bytes": 84,
                    },
                },
            },
        }

        registry = record_committed_report_artifacts(self.paths, manifest)
        by_id = {item["artifact_id"]: item for item in registry["artifacts"]}

        self.assertEqual("current", by_id["report_html"]["state"])
        self.assertEqual("report", by_id["report_html"]["scope"])
        self.assertEqual("b" * 64, by_id["report_html"]["digest"]["sha256"])
        self.assertEqual("stale", by_id["client_breakdown_xlsx"]["state"])
        self.assertEqual(
            "source_report_generation_changed",
            by_id["client_breakdown_xlsx"]["stale_reason"],
        )

    def test_omitted_report_artifact_becomes_superseded_after_successful_commit(self) -> None:
        first_manifest = self._report_manifest(
            generation_id="report-generation-1",
            artifact_ids=("report_html", "overview_pdf", "project_manifest"),
        )
        record_committed_report_artifacts(self.paths, first_manifest)
        mark_artifacts_stale(
            self.paths,
            scopes={"report"},
            reason="report_generation_started",
        )

        second_manifest = self._report_manifest(
            generation_id="report-generation-2",
            artifact_ids=("report_html", "project_manifest"),
        )
        registry = record_committed_report_artifacts(self.paths, second_manifest)
        by_id = {item["artifact_id"]: item for item in registry["artifacts"]}

        self.assertEqual("current", by_id["report_html"]["state"])
        self.assertEqual("superseded", by_id["overview_pdf"]["state"])
        self.assertIsNone(by_id["overview_pdf"]["stale_reason"])

    def test_corrupted_registry_blocks_review_invalidation_without_rewrite(self) -> None:
        manifest_path = self.paths.manifest
        manifest_path.write_text(
            json.dumps(
                {
                    "project_id": self.paths.root.name,
                    "profile": "research",
                    "root_path": str(self.paths.root),
                    "source": "synthetic.mp4",
                    "status": "reported",
                    "artifacts": {},
                }
            ),
            encoding="utf-8",
        )
        registry_path = self.paths.data / "artifact_registry.json"
        corrupted = b'{"schema_version": 999}'
        registry_path.write_bytes(corrupted)
        manifest_before = manifest_path.read_bytes()

        with self.assertRaises(ApiError) as caught:
            _invalidate_report_for_review(self.paths.root, "shot_0001")

        self.assertEqual(409, caught.exception.status)
        self.assertEqual(manifest_before, manifest_path.read_bytes())
        self.assertEqual(corrupted, registry_path.read_bytes())

    def test_review_invalidation_marks_client_export_stale_without_rendering(self) -> None:
        self.paths.manifest.write_text(
            json.dumps(
                {
                    "project_id": self.paths.root.name,
                    "profile": "research",
                    "root_path": str(self.paths.root),
                    "source": "synthetic.mp4",
                    "status": "reported",
                    "artifacts": {},
                }
            ),
            encoding="utf-8",
        )
        register_artifact(self.paths, _record())

        _invalidate_report_for_review(self.paths.root, "shot_0001")

        manifest = json.loads(self.paths.manifest.read_text(encoding="utf-8"))
        registry = load_artifact_registry(self.paths)
        self.assertEqual("review_pending", manifest["status"])
        self.assertEqual("stale", registry["artifacts"][0]["state"])
        self.assertEqual("shot_review_saved", registry["artifacts"][0]["stale_reason"])
        self.assertFalse(artifact_path(self.paths.root, "client_breakdown_xlsx").exists())

    def test_terminal_artifact_cannot_be_re_registered_or_revived(self) -> None:
        staging = _record(state="staging", retention="transient")
        register_artifact(self.paths, staging)
        transition_artifact(self.paths, "client_breakdown_xlsx", "failed")

        with self.assertRaisesRegex(ValueError, "already registered"):
            register_artifact(self.paths, _record())
        with self.assertRaisesRegex(ValueError, "Invalid artifact state transition"):
            transition_artifact(
                self.paths,
                "client_breakdown_xlsx",
                "current",
                generation_id="export-generation-2",
                source_generation_id="report-generation-2",
                digest=_digest(),
                retention="current",
            )

    def test_legacy_artifact_listing_ignores_symlinks_outside_workspace(self) -> None:
        outside = Path(self.temporary.name) / "outside-report.html"
        outside.write_text("outside", encoding="utf-8")
        report_path = artifact_path(self.paths.root, "report_html")
        report_path.symlink_to(outside)
        self.paths.manifest.write_text(
            json.dumps(
                {
                    "project_id": self.paths.root.name,
                    "profile": "research",
                    "root_path": str(self.paths.root),
                    "source": "synthetic.mp4",
                    "status": "reported",
                    "artifacts": {},
                }
            ),
            encoding="utf-8",
        )

        listed = project_artifacts(
            self.paths.root.parent,
            self.paths.root,
            self.paths.manifest,
        )

        legacy = next(item for item in listed if item["label"] == "Legacy report / 旧报告")
        self.assertFalse(legacy["present"])
        self.assertEqual("report.html", legacy["rel"])

    def test_legacy_artifact_listing_ignores_symlinked_parent_directory(self) -> None:
        outside_reports = Path(self.temporary.name) / "outside-reports"
        outside_reports.mkdir()
        (outside_reports / "report.html").write_text("outside", encoding="utf-8")
        self.paths.reports.rmdir()
        self.paths.reports.symlink_to(outside_reports, target_is_directory=True)
        self.paths.manifest.write_text(
            json.dumps(
                {
                    "project_id": self.paths.root.name,
                    "profile": "research",
                    "root_path": str(self.paths.root),
                    "source": "synthetic.mp4",
                    "status": "reported",
                    "artifacts": {},
                }
            ),
            encoding="utf-8",
        )

        listed = project_artifacts(
            self.paths.root.parent,
            self.paths.root,
            self.paths.manifest,
        )

        legacy = next(item for item in listed if item["label"] == "Legacy report / 旧报告")
        self.assertFalse(legacy["present"])
        self.assertEqual("report.html", legacy["rel"])

    def _report_manifest(
        self,
        *,
        generation_id: str,
        artifact_ids: tuple[str, ...],
    ) -> dict[str, object]:
        artifacts: dict[str, str] = {}
        receipts: dict[str, dict[str, object]] = {}
        for index, artifact_id in enumerate(artifact_ids, start=1):
            spec = artifact_spec(artifact_id)
            path = artifact_path(self.paths.root, artifact_id)
            artifacts[artifact_id] = str(path)
            receipts[artifact_id] = {
                "path": str(path),
                "kind": spec.kind,
                "digest_mode": (
                    "canonical-json-without-report-generation-v1"
                    if spec.kind == "manifest"
                    else "sha256-tree-v1"
                    if spec.kind == "directory"
                    else "sha256-file-v1"
                ),
                "sha256": f"{index:064x}",
                "size_bytes": index,
            }
        return {
            "project_id": self.paths.root.name,
            "profile": "research",
            "root_path": str(self.paths.root),
            "source": "synthetic.mp4",
            "status": "reported",
            "artifacts": artifacts,
            "report_generation": {
                "schema_version": 3,
                "generation_id": generation_id,
                "run_id": generation_id,
                "state": "committed",
                "digest_algorithm": "sha256",
                "source_receipts": {"audio_generation": {"generation_id": "audio-1"}},
                "artifact_digests": receipts,
            },
        }


if __name__ == "__main__":
    unittest.main()
