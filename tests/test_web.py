from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from video_analysis_mvp import web as web_module
from video_analysis_mvp.audio import _stage_and_commit_audio_generation
from video_analysis_mvp.paths import ProjectPaths
from video_analysis_mvp.schemas import (
    AnalysisProfile,
    CanonicalMediaPackage,
    Shot,
    SourceType,
    dump_json,
    load_json,
)
from video_analysis_mvp.synthesis import _commit_report_generation
from video_analysis_mvp.visual import _build_visual_generation_receipt
from video_analysis_mvp.web import MAX_REQUEST_BODY_BYTES, serve


REPO_ROOT = Path(__file__).resolve().parents[1]


class WebContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="video-analysis-web-test-")
        self.root = Path(self.tempdir.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.dist = self.root / "dist"
        (self.dist / "assets").mkdir(parents=True)
        (self.dist / "index.html").write_text(
            "<!doctype html><title>React fixture</title><div id='react-contract'>ready</div>",
            encoding="utf-8",
        )
        (self.dist / "assets" / "app-abc123.js").write_text("export const ready = true;", encoding="utf-8")

        reports = self.workspace / "blocked-project" / "reports"
        data = self.workspace / "blocked-project" / "data"
        reports.mkdir(parents=True)
        data.mkdir(parents=True)
        (self.workspace / "blocked-project" / "project_manifest.json").write_text(
            json.dumps(
                {
                    "project_id": "blocked-project",
                    "profile": "research",
                    "root_path": str(self.workspace / "blocked-project"),
                    "source": "synthetic",
                    "status": "reported",
                    "artifacts": {
                        "storyboard_html": str(reports / "storyboard.html"),
                        "profile_analysis_html": str(reports / "profile_analysis.html"),
                    },
                }
            ),
            encoding="utf-8",
        )
        (reports / "storyboard.html").write_text("primary", encoding="utf-8")
        (reports / "profile_analysis.html").write_text("blocked client export", encoding="utf-8")
        (reports / "active.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
            encoding="utf-8",
        )
        (reports / "active.xhtml").write_text(
            '<html xmlns="http://www.w3.org/1999/xhtml"><script>alert(1)</script></html>',
            encoding="utf-8",
        )
        (data / "readiness.json").write_text(
            json.dumps({"status": "blocked", "professional_export_allowed": False}),
            encoding="utf-8",
        )
        (data / "shots.json").write_text(
            json.dumps(
                [
                    {
                        "shot_id": "shot_0001",
                        "shot_no": 1,
                        "start_time": 0.0,
                        "end_time": 1.0,
                        "duration": 1.0,
                        "frame_ref": "frame-0001.jpg",
                        "primary_frame_ref": "frame-0001.jpg",
                        "frame_refs": ["frame-0001.jpg"],
                        "story_beat": "heuristic_unverified:opening_sequence",
                        "annotation_source": "machine",
                        "readiness_status": "blocked",
                        "readiness_reasons": ["verified annotation provenance required"],
                    }
                ]
            ),
            encoding="utf-8",
        )
        self._publish_reports()

        self.process: subprocess.Popen[bytes] | None = None
        self._start_server(self.dist)

    def _publish_reports(self) -> None:
        project = self.workspace / "blocked-project"
        paths = ProjectPaths(project)
        paths.ensure()
        shots = [Shot.model_validate(item) for item in load_json(paths.data / "shots.json")]
        dump_json(paths.data / "shots.json", shots)
        Image.new("RGB", (2, 2), color=(24, 48, 72)).save(paths.keyframes / "frame-0001.jpg")
        (paths.assets / "contact_sheet.jpg").write_bytes(b"contact-sheet")
        dump_json(paths.data / "scenes.json", [])
        dump_json(
            paths.data / "visual_generation.json",
            _build_visual_generation_receipt(paths, shots, []),
        )
        _stage_and_commit_audio_generation(paths, [], [], [])
        media = CanonicalMediaPackage(
            project_id=project.name,
            source_type=SourceType.file,
            source="synthetic",
            local_master_path=str(project / "ingest" / "master.mp4"),
            review_copy_path=str(project / "assets" / "review.mp4"),
            audio_path=str(project / "assets" / "audio.wav"),
            duration_seconds=1.0,
            frame_rate=24.0,
            resolution="320x180",
            aspect_ratio=16 / 9,
            status="analyzed",
            analysis_profile=AnalysisProfile.research,
        )
        dump_json(paths.data / "media_package.json", media)
        artifacts = {
            "project_manifest": str(paths.manifest),
            "storyboard_html": str(paths.reports / "storyboard.html"),
            "profile_analysis_html": str(paths.reports / "profile_analysis.html"),
        }
        _commit_report_generation(paths, media, str(uuid.uuid4()), artifacts)

    def tearDown(self) -> None:
        self._stop_server()
        self.tempdir.cleanup()

    def _start_server(self, dist: Path) -> None:
        self.port = self._free_port()
        env = os.environ.copy()
        env["PYTHONPATH"] = str(REPO_ROOT / "src")
        env["VIDEO_ANALYSIS_FRONTEND_DIST"] = str(dist)
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "video_analysis_mvp.cli",
                "--workspace",
                str(self.workspace),
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
            ],
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.fail(f"web server exited early with {self.process.returncode}")
            try:
                status, _, _ = self._request("/api/not-found")
                if status == 404:
                    return
            except OSError:
                time.sleep(0.05)
        self.fail("web server did not become ready")

    def _stop_server(self) -> None:
        if self.process is None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=4)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=4)
        self.process = None

    @staticmethod
    def _free_port() -> int:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        body: bytes | str | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=8)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            response_headers = {key.lower(): value for key, value in response.getheaders()}
            return response.status, response_headers, response.read()
        finally:
            connection.close()

    def _raw_request(self, payload: bytes) -> bytes:
        chunks: list[bytes] = []
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as connection:
            connection.settimeout(5)
            connection.sendall(payload)
            connection.shutdown(socket.SHUT_WR)
            while True:
                chunk = connection.recv(64 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
        return b"".join(chunks)

    def test_built_frontend_assets_and_spa_routes_share_one_origin(self) -> None:
        status, headers, body = self._request("/")
        self.assertEqual(200, status)
        self.assertIn(b"react-contract", body)
        self.assertEqual("no-cache", headers["cache-control"])
        self.assertIn("script-src 'self'", headers["content-security-policy"])
        self.assertIn("object-src 'none'", headers["content-security-policy"])

        status, headers, body = self._request("/assets/app-abc123.js")
        self.assertEqual(200, status)
        self.assertEqual(b"export const ready = true;", body)
        self.assertIn("immutable", headers["cache-control"])
        self.assertIn("javascript", headers["content-type"])

        status, _, body = self._request("/projects/example/workspace")
        self.assertEqual(200, status)
        self.assertIn(b"react-contract", body)

    def test_legacy_ui_remains_explicit_and_is_fallback_without_dist(self) -> None:
        status, headers, body = self._request("/legacy")
        self.assertEqual(200, status)
        self.assertIn(b"Shot Breakdown Workbench", body)
        self.assertIn("frame-ancestors 'none'", headers["content-security-policy"])
        self.assertIn(b'<input type="password" name="password" autocomplete="off"', body)

        self._stop_server()
        self._start_server(self.root / "missing-dist")
        status, _, body = self._request("/")
        self.assertEqual(200, status)
        self.assertIn(b"Shot Breakdown Workbench", body)

    def test_legacy_project_keeps_evidence_visible_without_review_mutations(self) -> None:
        status, _, body = self._request("/legacy/projects/blocked-project")

        self.assertEqual(200, status)
        self.assertIn(b"Read-only evidence fallback", body)
        self.assertIn(b'href="/projects/blocked-project"', body)
        self.assertIn(b'action="/regenerate/blocked-project"', body)
        self.assertNotIn(b'action="/shots/', body)
        self.assertNotIn(b'action="/vision/', body)

    def test_retired_legacy_review_endpoints_are_non_mutating_and_explain_migration(self) -> None:
        project = self.workspace / "blocked-project"
        evidence_paths = (
            project / "project_manifest.json",
            project / "data" / "shots.json",
            project / "data" / "visual_generation.json",
        )
        before = {path: path.read_bytes() for path in evidence_paths}

        for path in ("/shots/blocked-project/shot_0001", "/vision/blocked-project"):
            with self.subTest(path=path):
                status, _, body = self._request(path, method="POST")
                self.assertEqual(410, status)
                self.assertIn(b"use the primary workspace", body)
                self.assertEqual(before, {item: item.read_bytes() for item in evidence_paths})

    def test_legacy_finalize_obeys_the_shared_readiness_gate(self) -> None:
        project = self.workspace / "blocked-project"
        evidence_paths = (
            project / "project_manifest.json",
            project / "data" / "shots.json",
            project / "data" / "visual_generation.json",
        )
        before = {path: path.read_bytes() for path in evidence_paths}
        status, _, body = self._request("/api/session")
        self.assertEqual(200, status)
        token = json.loads(body)["csrf_token"]

        status, _, body = self._request(
            "/regenerate/blocked-project",
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=f"_csrf={token}",
        )

        self.assertEqual(409, status)
        self.assertIn(b"Project is not ready to finalize", body)
        self.assertEqual(before, {item: item.read_bytes() for item in evidence_paths})

    def test_keeper_http_compatibility_is_ads_only_and_branch_bounded(self) -> None:
        ads = self.workspace / "ads-project"
        (ads / "data").mkdir(parents=True)
        (ads / "project_manifest.json").write_text(
            json.dumps(
                {
                    "project_id": ads.name,
                    "profile": "ads",
                    "root_path": str(ads),
                    "source": "synthetic",
                    "status": "reported",
                    "artifacts": {},
                }
            ),
            encoding="utf-8",
        )
        status, _, body = self._request("/api/session")
        self.assertEqual(200, status)
        token = json.loads(body)["csrf_token"]
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        status, response_headers, _ = self._request(
            "/decision/ads-project",
            method="POST",
            headers=headers,
            body=f"_csrf={token}&keeper_branch=premium_style",
        )
        self.assertEqual(303, status)
        self.assertEqual("/projects/ads-project", response_headers["location"])
        receipt = ads / "data" / "keeper_decision.json"
        self.assertEqual("premium_style", json.loads(receipt.read_bytes())["keeper_branch"])
        accepted = receipt.read_bytes()

        status, _, body = self._request(
            "/decision/ads-project",
            method="POST",
            headers=headers,
            body=f"_csrf={token}&keeper_branch=unknown_branch",
        )
        self.assertEqual(400, status)
        self.assertIn(b"Unsupported keeper branch", body)
        self.assertEqual(accepted, receipt.read_bytes())

        status, _, body = self._request(
            "/decision/blocked-project",
            method="POST",
            headers=headers,
            body=f"_csrf={token}&keeper_branch=safer",
        )
        self.assertEqual(409, status)
        self.assertIn(b"only for ads projects", body)
        self.assertFalse(
            (self.workspace / "blocked-project" / "data" / "keeper_decision.json").exists()
        )

    def test_static_and_workspace_paths_reject_traversal(self) -> None:
        (self.root / "secret.txt").write_text("secret", encoding="utf-8")
        status, _, _ = self._request("/%2e%2e/secret.txt")
        self.assertEqual(404, status)

        outside = self.root / "workspace-escape.txt"
        outside.write_text("outside", encoding="utf-8")
        status, _, _ = self._request("/files/../workspace-escape.txt")
        self.assertEqual(404, status)

        settings = self.workspace / "_settings"
        settings.mkdir()
        (settings / "runtime_config.json").write_text('{"openai_api_key":"must-not-leak"}', encoding="utf-8")
        status, _, body = self._request("/files/_settings/runtime_config.json")
        self.assertEqual(404, status)
        self.assertNotIn(b"must-not-leak", body)

    def test_api_dispatch_and_exact_same_origin_cors(self) -> None:
        status, _, body = self._request("/api/runtime/doctor")
        self.assertEqual(200, status)
        self.assertIn("doctor", json.loads(body))

        status, headers, _ = self._request(
            "/api/not-found", headers={"Origin": "https://example.com"}
        )
        self.assertEqual(403, status)
        self.assertNotIn("access-control-allow-origin", headers)

        sibling_origin = "http://127.0.0.1:5173"
        status, headers, _ = self._request("/api/session", headers={"Origin": sibling_origin})
        self.assertEqual(403, status)
        self.assertNotIn("access-control-allow-origin", headers)

        origin = f"http://127.0.0.1:{self.port}"
        status, headers, _ = self._request("/api/not-found", headers={"Origin": origin})
        self.assertEqual(404, status)
        self.assertEqual(origin, headers["access-control-allow-origin"])
        self.assertEqual("Origin", headers["vary"])

    def test_public_media_endpoint_exposes_annotation_receipts(self) -> None:
        status, _, body = self._request("/api/projects/blocked-project/media")

        self.assertEqual(200, status)
        shot = json.loads(body)["shot_boundaries"][0]
        self.assertEqual("machine", shot["annotation_source"])
        self.assertEqual("unverified", shot["annotation_verification"])
        self.assertIn("verified annotation provenance required", shot["readiness_reasons"])
        self.assertNotIn("missing primary_frame_ref", shot["readiness_reasons"])
        self.assertTrue(shot["keyframes"][0]["present"])

    def test_symlinked_manifest_is_404_for_api_legacy_listing_and_files(self) -> None:
        secret = "manifest-secret-must-not-leak"
        outside = self.root / "outside-manifest.json"
        outside.write_text(
            json.dumps(
                {
                    "project_id": "blocked-project",
                    "profile": "research",
                    "root_path": str(self.workspace / "blocked-project"),
                    "source": secret,
                    "status": "reported",
                    "artifacts": {},
                }
            ),
            encoding="utf-8",
        )
        manifest = self.workspace / "blocked-project" / "project_manifest.json"
        manifest.unlink()
        manifest.symlink_to(outside)

        status, _, body = self._request("/api/projects/blocked-project")
        self.assertEqual(404, status)
        self.assertNotIn(secret.encode(), body)

        status, _, body = self._request("/api/projects")
        self.assertEqual(200, status)
        self.assertEqual([], json.loads(body)["projects"])
        self.assertNotIn(secret.encode(), body)

        status, _, body = self._request("/legacy/projects/blocked-project")
        self.assertEqual(404, status)
        self.assertNotIn(secret.encode(), body)

        status, _, body = self._request("/files/blocked-project/reports/storyboard.html")
        self.assertEqual(404, status)
        self.assertNotIn(secret.encode(), body)

    def test_host_origin_and_csrf_protect_mutations(self) -> None:
        status, _, _ = self._request("/api/session", headers={"Host": "attacker.example"})
        self.assertEqual(403, status)

        status, _, _ = self._request(
            "/api/session", headers={"Sec-Fetch-Site": "cross-site"}
        )
        self.assertEqual(403, status)

        status, _, body = self._request("/api/session")
        self.assertEqual(200, status)
        token = json.loads(body)["csrf_token"]
        self.assertGreater(len(token), 20)

        json_headers = {"Content-Type": "application/json"}
        status, _, _ = self._request(
            "/api/intake/validate", method="POST", headers=json_headers, body="{}"
        )
        self.assertEqual(403, status)

        status, _, _ = self._request(
            "/api/intake/validate",
            method="POST",
            headers={**json_headers, "X-VEW-CSRF": "wrong"},
            body="{}",
        )
        self.assertEqual(403, status)

        status, _, _ = self._request(
            "/api/intake/validate",
            method="POST",
            headers={"Content-Type": "text/plain", "X-VEW-CSRF": token},
            body="{}",
        )
        self.assertEqual(415, status)

        status, _, body = self._request(
            "/api/intake/validate",
            method="POST",
            headers={**json_headers, "X-VEW-CSRF": token},
            body="{}",
        )
        self.assertEqual(200, status)
        self.assertFalse(json.loads(body)["ready"])

        status, _, body = self._request("/legacy")
        self.assertEqual(200, status)
        self.assertIn(f'name="_csrf" value="{token}"'.encode(), body)

    def test_legacy_browser_surface_rejects_url_ingest(self) -> None:
        status, _, body = self._request("/api/session")
        self.assertEqual(200, status)
        token = json.loads(body)["csrf_token"]
        form = (
            f"_csrf={token}&source=https%3A%2F%2Fexample.test%2Fvideo.mp4&profile=research"
        )
        status, _, body = self._request(
            "/analyze",
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=form,
        )
        self.assertEqual(400, status)
        self.assertIn(b"URL ingest is disabled", body)

    def test_mutation_body_caps_and_short_reads_fail_closed(self) -> None:
        status, _, body = self._request("/api/session")
        self.assertEqual(200, status)
        token = json.loads(body)["csrf_token"]
        common = (
            f"Host: 127.0.0.1:{self.port}\r\n"
            "Content-Type: application/json\r\n"
            f"X-VEW-CSRF: {token}\r\n"
            "Connection: close\r\n"
        )

        oversized = self._raw_request(
            (
                "POST /api/intake/validate HTTP/1.1\r\n"
                + common
                + f"Content-Length: {MAX_REQUEST_BODY_BYTES + 1}\r\n\r\n"
            ).encode("ascii")
        )
        self.assertTrue(oversized.startswith(b"HTTP/1.0 413"), oversized[:120])

        incomplete = self._raw_request(
            (
                "POST /api/intake/validate HTTP/1.1\r\n"
                + common
                + "Content-Length: 10\r\n\r\n{}"
            ).encode("ascii")
        )
        self.assertTrue(incomplete.startswith(b"HTTP/1.0 400"), incomplete[:120])
        self.assertIn(b"Incomplete request body", incomplete)

    def test_download_filename_header_blocks_response_splitting(self) -> None:
        reports = self.workspace / "blocked-project" / "reports"
        hostile_name = "evil\r\nX-Injected: yes.bin"
        (reports / hostile_name).write_bytes(b"blocked")
        response = self._raw_request(
            (
                "GET /files/blocked-project/reports/evil%0D%0AX-Injected%3A%20yes.bin HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{self.port}\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii")
        )
        header_block = response.split(b"\r\n\r\n", 1)[0]
        self.assertTrue(response.startswith(b"HTTP/1.0 404"), response[:120])
        self.assertNotIn(b"\r\nX-Injected:", header_block)

        unicode_name = "文件.bin"
        (reports / unicode_name).write_bytes(b"safe")
        status, headers, body = self._request(
            "/files/blocked-project/reports/%E6%96%87%E4%BB%B6.bin"
        )
        self.assertEqual(404, status)
        self.assertNotIn("content-disposition", headers)
        self.assertNotEqual(b"safe", body)

    def test_text_preview_is_stream_bounded(self) -> None:
        report = self.workspace / "blocked-project" / "reports" / "storyboard.html"
        report.write_text("x" * 100_000, encoding="utf-8")
        self._publish_reports()
        status, _, body = self._request(
            "/api/projects/blocked-project/deliverables/storyboard_html/preview"
        )
        self.assertEqual(200, status)
        payload = json.loads(body)
        self.assertEqual(60_000, len(payload["text"]))
        self.assertTrue(payload["truncated"])

    def test_non_loopback_bind_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            serve("0.0.0.0", 0, str(self.workspace))

    def test_loopback_server_bind_does_not_depend_on_reverse_dns(self) -> None:
        server_class = getattr(web_module, "LoopbackThreadingHTTPServer", None)
        self.assertIsNotNone(server_class)
        with patch.object(
            web_module.socket,
            "getfqdn",
            side_effect=AssertionError("loopback bind must not perform reverse DNS"),
        ):
            server = server_class(("127.0.0.1", 0), BaseHTTPRequestHandler)
        try:
            self.assertEqual("127.0.0.1", server.server_name)
        finally:
            server.server_close()

    @unittest.skipUnless(socket.has_ipv6, "Python runtime has no IPv6 support")
    def test_loopback_server_binds_ipv6_when_loopback_is_available(self) -> None:
        with socket.socket(socket.AF_INET6) as probe:
            try:
                probe.bind(("::1", 0))
            except OSError as exc:
                self.skipTest(f"IPv6 loopback is unavailable: {exc}")

        try:
            server = web_module.LoopbackThreadingHTTPServer(
                ("::1", 0), BaseHTTPRequestHandler
            )
        except OSError as exc:
            self.fail(f"validated IPv6 loopback must bind: {exc}")
        try:
            self.assertEqual(socket.AF_INET6, server.address_family)
            self.assertEqual("::1", server.server_name)
        finally:
            server.server_close()

    def test_missing_assets_are_not_spa_fallback(self) -> None:
        status, _, body = self._request("/assets/missing.js")
        self.assertEqual(404, status)
        self.assertNotIn(b"react-contract", body)

    def test_blocked_client_export_is_not_downloadable(self) -> None:
        status, _, body = self._request("/files/blocked-project/reports/storyboard.html")
        self.assertEqual(200, status)
        self.assertEqual(b"primary", body)

        status, headers, _ = self._request("/files/blocked-project/reports/storyboard.html")
        self.assertIn("script-src 'none'", headers["content-security-policy"])
        self.assertIn("sandbox", headers["content-security-policy"])

        for active_document in ("active.svg", "active.xhtml"):
            with self.subTest(active_document=active_document):
                status, _, _ = self._request(f"/files/blocked-project/reports/{active_document}")
                self.assertEqual(404, status)

        status, _, _ = self._request("/files/blocked-project/reports/profile_analysis.html")
        self.assertEqual(403, status)

        status, _, body = self._request(
            "/api/projects/blocked-project/deliverables/profile_analysis_html/preview"
        )
        self.assertEqual(403, status)
        self.assertEqual(403, json.loads(body)["error"]["status"])

        status, _, legacy = self._request("/legacy/projects/blocked-project")
        self.assertEqual(200, status)
        self.assertNotIn(b'href="/files/blocked-project/reports/profile_analysis.html"', legacy)
        self.assertIn(b">blocked</span>", legacy)

    def test_file_ranges_support_suffix_open_ended_and_fail_unsatisfiable(self) -> None:
        target = "/files/blocked-project/reports/storyboard.html"
        status, headers, body = self._request(target, headers={"Range": "bytes=-4"})
        self.assertEqual(206, status)
        self.assertEqual(b"mary", body)
        self.assertEqual("bytes 3-6/7", headers["content-range"])

        status, headers, body = self._request(target, headers={"Range": "bytes=2-"})
        self.assertEqual(206, status)
        self.assertEqual(b"imary", body)
        self.assertEqual("bytes 2-6/7", headers["content-range"])

        for value in ("bytes=7-", "bytes=4-2", "bytes=-0"):
            with self.subTest(value=value):
                status, headers, body = self._request(target, headers={"Range": value})
                self.assertEqual(416, status)
                self.assertEqual("bytes */7", headers["content-range"])
                self.assertEqual(b"", body)

        report = self.workspace / "blocked-project" / "reports" / "storyboard.html"
        report.write_bytes(b"")
        self._publish_reports()
        status, headers, body = self._request(target, headers={"Range": "bytes=0-"})
        self.assertEqual(416, status)
        self.assertEqual("bytes */0", headers["content-range"])
        self.assertEqual(b"", body)

    def test_non_ads_file_routes_hide_old_ads_and_stale_provider_receipts(self) -> None:
        project = self.workspace / "blocked-project"
        old_ads = project / "reports" / "remake_brief.md"
        old_ads.write_text("old ads-only output", encoding="utf-8")
        stale_vision = project / "data" / "vision_annotations.json"
        stale_vision.write_text('{"provider":"openai","stale":true}', encoding="utf-8")
        manifest_path = project / "project_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["artifacts"].update(
            {
                "remake_brief": str(old_ads),
                "vision_annotations": str(stale_vision),
            }
        )
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        for relative in ("reports/remake_brief.md", "data/vision_annotations.json"):
            with self.subTest(relative=relative):
                status, _, _ = self._request(f"/files/blocked-project/{relative}")
                self.assertEqual(404, status)
        status, _, _ = self._request("/files/blocked-project/reports/storyboard.html")
        self.assertEqual(404, status)
        status, _, body = self._request("/files/blocked-project/project_manifest.json")
        self.assertEqual(200, status)
        self.assertIn(b'"profile": "research"', body)


if __name__ == "__main__":
    unittest.main()
