from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import unittest
from pathlib import Path

from PIL import Image


class FrontendContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        repo = Path(__file__).parents[1]
        cls.repo = repo
        frontend_root = repo / "frontend"
        frontend = frontend_root / "src"
        cls.styles = (frontend / "styles.css").read_text(encoding="utf-8")
        cls.app = (frontend / "App.tsx").read_text(encoding="utf-8")
        cls.client = (frontend / "api" / "client.ts").read_text(encoding="utf-8")
        cls.codex_panel = (frontend / "components" / "CodexAnalysisPanel.tsx").read_text(encoding="utf-8")
        cls.audio_panel = (frontend / "features" / "audio" / "AudioReviewPanel.tsx").read_text(encoding="utf-8")
        cls.types = (frontend / "types.ts").read_text(encoding="utf-8")
        cls.vite_config = (frontend_root / "vite.config.ts").read_text(encoding="utf-8")
        cls.frontend_package = json.loads((frontend_root / "package.json").read_text(encoding="utf-8"))
        cls.vite_integration = (
            frontend_root / "tests" / "vite-proxy-origin.integration.mjs"
        ).read_text(encoding="utf-8")
        cls.ci = (repo / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        cls.ui_receipt = json.loads(
            (repo / "docs" / "screenshots" / "ui-acceptance-receipt.json").read_text(encoding="utf-8")
        )

    def declarations(self, selector: str) -> str:
        match = re.search(re.escape(selector) + r"\s*\{([^}]+)\}", self.styles)
        self.assertIsNotNone(match, f"missing CSS rule for {selector}")
        return match.group(1) if match else ""

    def assert_touch_target(self, selector: str, *, width_property: str = "min-width") -> None:
        declarations = self.declarations(selector)
        self.assertRegex(declarations, rf"{re.escape(width_property)}:\s*44px")
        self.assertRegex(declarations, r"(?:min-height|height):\s*44px")

    def test_named_interactive_targets_meet_the_44px_contract(self) -> None:
        self.assert_touch_target(".brand-mark", width_property="width")
        self.assert_touch_target(".table-shot-button")
        self.assert_touch_target(".mobile-back", width_property="width")
        self.assertRegex(self.declarations(".evidence-files a"), r"min-height:\s*44px")
        self.assertRegex(self.declarations(".back-link"), r"min-height:\s*44px")
        self.assertRegex(self.declarations(".skip-link"), r"min-height:\s*44px")

    def css_color(self, token: str) -> str:
        match = re.search(rf"{re.escape(token)}:\s*(#[0-9a-fA-F]{{6}})", self.styles)
        self.assertIsNotNone(match, f"missing six-digit color for {token}")
        return match.group(1) if match else "#000000"

    @staticmethod
    def relative_luminance(value: str) -> float:
        components = [int(value[index:index + 2], 16) / 255 for index in (1, 3, 5)]

        def linearize(channel: float) -> float:
            return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4

        red, green, blue = (linearize(channel) for channel in components)
        return 0.2126 * red + 0.7152 * green + 0.0722 * blue

    @classmethod
    def contrast_ratio(cls, first: str, second: str) -> float:
        light, dark = sorted((cls.relative_luminance(first), cls.relative_luminance(second)), reverse=True)
        return (light + 0.05) / (dark + 0.05)

    def test_action_and_warning_colors_meet_wcag_aa_on_white_and_paper(self) -> None:
        paper = self.css_color("--paper")
        for token in ("--accent", "--amber", "--green"):
            color = self.css_color(token)
            for background in ("#ffffff", paper):
                with self.subTest(token=token, background=background):
                    self.assertGreaterEqual(self.contrast_ratio(color, background), 4.5)

    def test_placeholder_and_control_boundaries_meet_wcag_contrast(self) -> None:
        paper = self.css_color("--paper")
        placeholder_match = re.search(r"input::placeholder\s*\{[^}]*color:\s*(#[0-9a-fA-F]{6})", self.styles)
        self.assertIsNotNone(placeholder_match)
        placeholder = placeholder_match.group(1) if placeholder_match else "#000000"
        border = self.css_color("--control-border")
        for background in ("#ffffff", paper):
            with self.subTest(kind="placeholder", background=background):
                self.assertGreaterEqual(self.contrast_ratio(placeholder, background), 4.5)
            with self.subTest(kind="control-border", background=background):
                self.assertGreaterEqual(self.contrast_ratio(border, background), 3.0)
        self.assertIn(
            ".primary-button, .secondary-button, .icon-button {\n  min-height: 44px; border: 1px solid var(--control-border)",
            self.styles,
        )
        self.assertIn(
            "input, select, textarea { width: 100%; border: 1px solid var(--control-border)",
            self.styles,
        )

    def test_modal_drawer_has_a_complete_keyboard_and_background_contract(self) -> None:
        required_source = (
            "createPortal(",
            'background?.setAttribute("inert", "")',
            'background?.setAttribute("aria-hidden", "true")',
            'drawerCloseRef.current?.focus()',
            'event.key === "Escape"',
            'event.key !== "Tab"',
            "event.shiftKey",
            "dialog.querySelectorAll<HTMLElement>(drawerFocusableSelector)",
            "last.focus()",
            "first.focus()",
            "opener?.focus()",
            "ref={drawerCloseRef}",
            "onClick={closeDrawer}",
            "event.currentTarget === event.target) closeDrawer()",
        )
        for source in required_source:
            with self.subTest(source=source):
                self.assertIn(source, self.app)

    def test_workspace_renders_authoritative_gate_receipts(self) -> None:
        self.assertIn("authoritativeReadinessReasons(bundle, readiness)", self.app)
        self.assertIn("readiness?.checks ?? []", self.app)
        self.assertIn("readinessReasons.map", self.app)
        self.assertIn("readinessChecks.map", self.app)
        self.assertIn("bundle.deliverables.export?.blocked_reasons", self.app)
        self.assertNotIn('"Media verified"', self.app)
        self.assertIn('"Media file present"', self.app)

    def test_codex_panel_uses_shared_workflow_without_auto_finalization(self) -> None:
        self.assertIn("<CodexAnalysisPanel", self.app)
        for label in ("Prepare Codex analysis", "Apply model analysis", "Human review is still required"):
            self.assertIn(label, self.codex_panel)
        self.assertIn("const maxResponseBytes = 1024 * 1024", self.codex_panel)
        self.assertIn("body: responseText", self.client)
        self.assertNotIn("regenerateProjectReport", self.codex_panel)
        self.assertNotIn("OPENAI_API_KEY", self.codex_panel)
        self.assertIn("agent_submission_bound", self.types)
        self.assertIn("const epoch = ++statusEpoch.current", self.codex_panel)
        self.assertIn("if (epoch === statusEpoch.current) setStatus(value)", self.codex_panel)

    def test_audio_panel_uses_shared_bound_review_without_automatic_export(self) -> None:
        for source in (
            "getAudioReview(projectId",
            "saveAudioReview(projectId, event.event_id",
            "expected_generation_id: generationId",
            "expected_proposal_sha256: event.proposal_sha256",
            "confirm_operator_review: true",
            "exports_generated !== false",
            "Finalize remains a separate action",
            "No events match these filters. This is not evidence of silence.",
            'type MobileMode = "video" | "shots" | "audio" | "evidence" | "export"',
        ):
            with self.subTest(source=source):
                self.assertIn(source, self.client + self.audio_panel + self.app)
        self.assertIn("const generationId = useRef<string | undefined>(undefined)", self.audio_panel)
        self.assertIn("const shotFilter = onlyShot ? selectedShotId : undefined", self.audio_panel)
        self.assertIn('const cursorScope = shotFilter ?? "all-shots"', self.audio_panel)
        self.assertIn("cursor.scope === cursorScope ? cursor.offset : 0", self.audio_panel)
        self.assertIn("shot_id: shotFilter", self.audio_panel)
        effect = re.search(r"useEffect\(\(\) => \{(.*?)\n  \}, \[([^]]+)\]\);", self.audio_panel, re.DOTALL)
        self.assertIsNotNone(effect)
        effect_body, dependencies = effect.groups() if effect else ("", "")
        self.assertIn('setPage(null);', effect_body)
        self.assertLess(effect_body.index('setPage(null);'), effect_body.index('getAudioReview(projectId'))
        self.assertNotIn('setSelectedEventId("");', effect_body)
        self.assertIn("shotFilter", dependencies)
        self.assertNotIn("selectedShotId", dependencies)
        self.assertIn('code === "audio_commit_failed"', self.audio_panel)
        self.assertIn("left: min(var(--event-left), calc(100% - 44px))", self.styles)
        self.assertIn('className={`audio-event-band ${state}`}', self.audio_panel)
        self.assertIn("Timeline scale unavailable because media duration is unknown", self.audio_panel)
        self.assertIn("onCue={onCue}", self.audio_panel)
        self.assertIn('setMobileMode("video")', self.app)
        self.assertIn("window.requestAnimationFrame(() => videoRef.current?.focus())", self.app)
        self.assertIn('"0 events on this page"', self.audio_panel)
        self.assertNotIn("setInterval", self.audio_panel)
        self.assertNotIn("regenerateProjectReport", self.audio_panel)
        self.assertNotIn(".pdf", self.audio_panel.lower())
        self.assertNotIn(".xlsx", self.audio_panel.lower())

    def test_blocked_deliverables_are_not_rendered_as_open_links(self) -> None:
        self.assertIn('const openBlocked = artifact.readiness_status === "blocked";', self.app)
        self.assertIn("artifact.present && artifact.url && !openBlocked", self.app)
        self.assertIn('aria-disabled="true"', self.app)
        self.assertIn("Open blocked by readiness gate", self.app)

    def test_available_deliverables_are_successful_while_blocked_and_missing_are_explicit(self) -> None:
        self.assertIn('const available = artifact.present && artifact.readiness_status === "available";', self.app)
        self.assertIn('const missing = !artifact.present || artifact.readiness_status === "missing";', self.app)
        self.assertIn('available ? "is-ready"', self.app)
        self.assertIn('openBlocked || missing ? "is-blocked"', self.app)
        self.assertIn('aria-label={disabledActionLabel}', self.app)
        self.assertIn("disabled", self.app)
        self.assertIn(".status-text.is-blocked", self.styles)

    def test_tablet_keeps_the_cross_shot_table_until_mobile_modes_take_over(self) -> None:
        tablet_start = self.styles.index("@media (max-width: 900px)")
        mobile_start = self.styles.index("@media (max-width: 760px)")
        tablet = self.styles[tablet_start:mobile_start]
        mobile = self.styles[mobile_start:]
        self.assertNotIn(".shot-table-section { display: none; }", tablet)
        self.assertIn(".shot-table-section { display: none; }", mobile)
        self.assertIn(".mobile-mode-nav { min-height: 62px; display: grid;", mobile)

    def test_mobile_compact_deliverables_keep_status_text_visible(self) -> None:
        mobile = self.styles[self.styles.index("@media (max-width: 760px)"):]
        self.assertNotIn(
            ".deliverable-groups.is-compact .artifact-row .status-text { display: none; }",
            self.styles,
        )
        self.assertIn(
            ".deliverable-groups.is-compact .artifact-row .status-text { "
            "grid-column: 2; grid-row: 2; display: inline-flex; justify-self: start; }",
            mobile,
        )

    def test_project_navigation_disables_context_links_and_uses_exact_shots_matching(self) -> None:
        self.assertIn("projectId ? (", self.app)
        self.assertIn('className="nav-item is-disabled"', self.app)
        self.assertIn('aria-disabled="true"', self.app)
        self.assertIn("<NavLink to={workspacePath} end>", self.app)
        self.assertIn("<NavLink to={deliverablesPath}>", self.app)
        self.assertNotIn('const workspacePath = projectId ? `/projects/${encodeURIComponent(projectId)}` : "/";', self.app)
        self.assertNotIn('const deliverablesPath = projectId ? `${workspacePath}/deliverables` : "/";', self.app)

    def test_workspace_load_uses_one_atomic_snapshot_request(self) -> None:
        match = re.search(
            r"export async function loadWorkspace\(projectId: string\): Promise<WorkspaceBundle> \{(.*?)\n\}",
            self.client,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        body = match.group(1) if match else ""
        self.assertIn("/workspace", body)
        self.assertIn("request<WorkspaceSnapshot>", body)
        self.assertNotIn("Promise.all", body)
        for old_call in ("getProject(", "getCanvas(", "getMedia(", "getDeliverables("):
            self.assertNotIn(old_call, body)
        self.assertIn("snapshot_id: string", self.types)
        self.assertIn("generation_id: string | null", self.types)

    def test_run_cancellation_restarts_polling_and_csrf_uses_one_bounded_refresh(self) -> None:
        cancel_body = re.search(r"async function cancel\(\) \{(.*?)\n  \}", self.app, re.DOTALL)
        self.assertIsNotNone(cancel_body)
        self.assertIn("setPollGeneration((generation) => generation + 1)", cancel_body.group(1) if cancel_body else "")
        self.assertIn("setRun((previous) => newestRun(previous, current))", cancel_body.group(1) if cancel_body else "")
        self.assertIn("status === undefined || status === 408 || status === 429 || status >= 500", self.app)
        self.assertIn("async function csrfToken(signal?: AbortSignal)", self.client)
        self.assertIn("signal\n    })", self.client)
        self.assertIn("if (mutation && await isInvalidCsrfResponse(response))", self.client)
        self.assertIn("let csrfTokenValue: string | undefined", self.client)
        self.assertNotIn("csrfTokenPromise", self.client)
        self.assertEqual(1, self.client.count("csrfTokenValue = undefined;\n      headers.set"))

    def test_provider_receipt_verification_is_distinct_from_factual_human_review(self) -> None:
        self.assertIn('"provider_receipt_verified"', self.types)
        self.assertIn('"Provider receipt verified"', self.app)
        self.assertIn("not factual human review", self.app)

    def test_source_panel_uses_only_dynamic_provenance_artifacts(self) -> None:
        for artifact_id in ("project_manifest", "media_package", "lineage_json", "readiness_json", "boundary_review_json"):
            with self.subTest(artifact_id=artifact_id):
                self.assertIn(f'findArtifact(bundle.deliverables.artifacts, "{artifact_id}")', self.app)
        self.assertIn('label="Media binding"', self.app)
        self.assertIn('aria-label="Provenance artifacts"', self.app)

    def test_primary_workspace_owns_the_full_review_and_finalize_loop(self) -> None:
        for source in (
            'openDrawer("review"',
            "updateShotReview(projectId, shot.id, shot.edit_version, review)",
            "loadWorkspace(projectId)",
            "Save & next unresolved",
            "I checked this low-confidence boundary.",
            "Finalize package",
            'aria-live="polite"',
        ):
            with self.subTest(source=source):
                self.assertIn(source, self.app)
        self.assertIn("export async function updateShotReview", self.client)
        self.assertIn("export async function regenerateProjectReport", self.client)
        self.assertNotIn("slice(0, 24)", self.app)
        self.assertNotIn("Regenerating the evidence package", self.app)
        self.assertIn('boundaryReviewBound ? "Low-confidence cuts reviewed" : "No low-confidence cuts"', self.app)

    def test_vite_proxy_origin_isolation_is_explicit_and_ci_enforced(self) -> None:
        self.assertRegex(self.vite_config, r"server:\s*\{\s*cors:\s*false,")
        self.assertRegex(self.vite_config, r"preview:\s*\{\s*cors:\s*false")
        self.assertIn("changeOrigin: true", self.vite_config)
        self.assertIn("headers: { Origin: localBackend }", self.vite_config)
        self.assertEqual(
            self.frontend_package["scripts"]["test:integration"],
            "node tests/vite-proxy-origin.integration.mjs",
        )
        self.assertIn('const backendOrigin = "http://127.0.0.1:8787";', self.vite_integration)
        self.assertIn('headers: { Origin: siblingOrigin }', self.vite_integration)
        self.assertIn('headers: { Origin: devOrigin }', self.vite_integration)
        self.assertIn("npm run test:integration", self.ci)

    def test_ui_acceptance_receipt_binds_current_assets_and_screenshots(self) -> None:
        receipt = self.ui_receipt
        self.assertEqual("local_candidate_only", receipt["status"])
        self.assertEqual("Playwright Chrome", receipt["capture"]["browser"])
        self.assertRegex(receipt["capture"]["browser_version"], r"^\d+(?:\.\d+){3}$")
        self.assertEqual("/projects/review-workflow-final", receipt["capture"]["path"])
        self.assertEqual("scripts/run-demo.sh", receipt["capture"]["fixture"]["generator"])
        self.assertEqual("blocked", receipt["capture"]["fixture"]["initial_readiness"])
        self.assertEqual("ready", receipt["capture"]["fixture"]["final_readiness"])
        self.assertIs(receipt["capture"]["fixture"]["vision_provider_run"], False)

        reviewed = receipt["reviewed_source_candidate"]
        self.assertEqual(
            "sha256(mode\\0path\\0size\\0bytes\\0 for each UTF-8 path in sorted Git candidate order)",
            reviewed["hash_algorithm"],
        )

        candidate_digest = hashlib.sha256()
        output = subprocess.check_output(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=self.repo,
        )
        expected_exclusions = {
            "docs/screenshots/ui-acceptance-receipt.json",
            "docs/evidence/mature-candidate-receipt.json",
            "docs/release-readiness.md",
            "docs/cold-review.md",
            "progress.txt",
        }
        self.assertEqual(
            expected_exclusions,
            set(reviewed["excluded_paths"]),
        )
        candidate_paths = sorted(
            item.decode("utf-8")
            for item in output.split(b"\0")
            if item and item.decode("utf-8") not in expected_exclusions
        )
        for relative in candidate_paths:
            path = self.repo / relative
            if path.is_symlink():
                mode = "120000"
                payload = os.readlink(path).encode("utf-8")
            else:
                mode = "100755" if os.access(path, os.X_OK) else "100644"
                payload = path.read_bytes()
            candidate_digest.update(mode.encode("ascii"))
            candidate_digest.update(b"\0")
            candidate_digest.update(relative.encode("utf-8"))
            candidate_digest.update(b"\0")
            candidate_digest.update(str(len(payload)).encode("ascii"))
            candidate_digest.update(b"\0")
            candidate_digest.update(payload)
            candidate_digest.update(b"\0")
        self.assertEqual(reviewed["file_count"], len(candidate_paths))
        self.assertEqual(reviewed["sha256"], candidate_digest.hexdigest())

        source_digest = hashlib.sha256()
        source_files = sorted(path for path in (self.repo / "frontend" / "src").rglob("*") if path.is_file())
        for path in source_files:
            relative = path.relative_to(self.repo).as_posix()
            payload = path.read_bytes()
            source_digest.update(relative.encode("utf-8"))
            source_digest.update(b"\0")
            source_digest.update(str(len(payload)).encode("ascii"))
            source_digest.update(b"\0")
            source_digest.update(payload)
            source_digest.update(b"\0")
        self.assertEqual(receipt["frontend_source"]["file_count"], len(source_files))
        self.assertEqual(receipt["frontend_source"]["sha256"], source_digest.hexdigest())

        expected_asset_paths = {
            path.relative_to(self.repo).as_posix()
            for path in (self.repo / "src" / "video_analysis_mvp" / "frontend_dist").rglob("*")
            if path.is_file()
        }
        expected_screenshots = {
            "docs/screenshots/workspace-desktop-1440x900.png": (1440, 900),
            "docs/screenshots/review-drawer-desktop-1440x900.png": (1440, 900),
            "docs/screenshots/workspace-tablet-900x1000.png": (900, 1000),
            "docs/screenshots/mobile-export-390x844.png": (390, 844),
            "docs/screenshots/run-complete-desktop-1440x900.png": (1440, 900),
            "docs/screenshots/run-complete-mobile-390x844.png": (390, 844),
        }
        self.assertEqual(len(expected_asset_paths), len(receipt["served_assets"]))
        self.assertEqual(expected_asset_paths, {item["path"] for item in receipt["served_assets"]})
        self.assertEqual(len(expected_screenshots), len(receipt["screenshots"]))
        self.assertEqual(set(expected_screenshots), {item["path"] for item in receipt["screenshots"]})
        for item in [*receipt["served_assets"], *receipt["screenshots"]]:
            with self.subTest(path=item["path"]):
                path = self.repo / item["path"]
                payload = path.read_bytes()
                self.assertEqual(item["size_bytes"], len(payload))
                self.assertEqual(item["sha256"], hashlib.sha256(payload).hexdigest())

        for item in receipt["screenshots"]:
            path = self.repo / item["path"]
            with self.subTest(verify=item["path"]), Image.open(path) as image:
                image.verify()
            with self.subTest(dimensions=item["path"]), Image.open(path) as image:
                image.load()
                self.assertEqual("PNG", image.format)
                self.assertEqual(expected_screenshots[item["path"]], image.size)
                self.assertEqual(expected_screenshots[item["path"]], (
                    item["viewport"]["width"], item["viewport"]["height"]
                ))

        expected_viewports = {"desktop": (1440, 900), "tablet": (900, 1000), "mobile": (390, 844)}
        self.assertEqual(3, len(receipt["viewport_checks"]))
        self.assertEqual(set(expected_viewports), {item["name"] for item in receipt["viewport_checks"]})
        for viewport in receipt["viewport_checks"]:
            with self.subTest(viewport=viewport["name"]):
                self.assertEqual(expected_viewports[viewport["name"]], (viewport["width"], viewport["height"]))
                self.assertEqual(200, viewport["http_status"])
                self.assertEqual(0, viewport["console_warnings_or_errors"])
                self.assertEqual(0, viewport["page_errors"])
                self.assertEqual(viewport["document_client_width"], viewport["document_scroll_width"])
                self.assertLessEqual(viewport["document_scroll_width"], viewport["width"])
                self.assertEqual(0, viewport["visible_controls_below_44px"])
                self.assertEqual(0, viewport["horizontal_out_of_view_elements"])

        expected_interactions = {
            "initial_blocked_state_visible",
            "review_drawer_opened",
            "review_drawer_focus_restored",
            "all_shots_human_reviewed",
            "low_confidence_boundary_review_bound",
            "save_without_finalize_blocked",
            "explicit_finalize_ready",
            "professional_artifact_opened",
            "mutation_after_finalize_blocked",
            "byte_identical_restore_still_requires_finalize",
            "second_finalize_ready",
            "source_drawer_opened",
            "source_drawer_focus_restored_after_two_animation_frames",
            "codex_drawer_opened",
            "codex_drawer_focus_restored",
            "mobile_codex_drawer_focus_restored",
            "available_state_visible",
            "blocked_state_visible",
            "missing_or_unavailable_state_visible",
            "unverified_state_visible",
            "codex_boundary_states_no_embedded_or_launched_codex",
            "persistent_run_created_before_analysis_completed",
            "run_page_survived_direct_reload",
            "completed_run_exposed_stage_timings",
            "completed_run_opened_workspace",
        }
        self.assertEqual(expected_interactions, set(receipt["interaction_checks"]))
        self.assertTrue(all(value is True for value in receipt["interaction_checks"].values()))


if __name__ == "__main__":
    unittest.main()
