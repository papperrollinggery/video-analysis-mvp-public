from __future__ import annotations

import http.client
import importlib.util
import json
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from pathlib import Path
from urllib.parse import urlencode

from tests.test_audio_review import audio_review_fixture
from video_analysis_mvp.audio_review import QUERY_KEYS, REQUEST_KEYS, get_audio_event
from video_analysis_mvp.schemas import load_json
from video_analysis_mvp.synthesis import verify_report_generation_manifest


def request(port, method, path, *, body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        body = response.read()
        if response.status < 400 or "application/json" in (
            response.getheader("Content-Type") or ""
        ):
            return response.status, json.loads(body or b"{}")
        return response.status, {
            "transport_error": body.decode("utf-8", errors="replace")
        }
    finally:
        connection.close()


def oversized_request(port, path, headers):
    """Read early 413 without racing a server that correctly closes the body."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.putrequest("PATCH", path)
        for name, value in headers.items():
            connection.putheader(name, value)
        connection.putheader("Content-Length", str(1024 * 1024 + 1))
        connection.endheaders()
        response = connection.getresponse()
        response.read()
        return response.status
    finally:
        connection.close()


@contextmanager
def server(kind, workspace):
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    if kind == "builtin":
        args = [
            sys.executable,
            "-m",
            "video_analysis_mvp.cli",
            "--workspace",
            str(workspace),
            "serve",
            "--port",
            str(port),
        ]
    else:
        args = [
            sys.executable,
            "-m",
            "uvicorn",
            "video_analysis_mvp.api:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ]
    session_path = "/api/session"
    process = subprocess.Popen(
        args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        deadline = time.monotonic() + 10
        session = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"{kind} server exited: {process.returncode}")
            try:
                status, candidate = request(port, "GET", session_path)
            except OSError:
                status, candidate = 0, None
            if status == 200:
                session = candidate
                break
            time.sleep(0.05)
        if session is None:
            raise RuntimeError(f"{kind} server did not become ready")
        yield {
            "kind": kind,
            "port": port,
            "headers": {
                "Content-Type": "application/json",
                "X-VEW-CSRF": session["csrf_token"],
            },
        }
    finally:
        process.terminate()
        try:
            process.wait(timeout=4)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=4)


class AudioReviewHttpTest(unittest.TestCase):
    def test_real_http_boundaries_and_cross_process_compare_and_swap(self):
        with (
            tempfile.TemporaryDirectory(prefix="vew-review-http-") as directory,
            ExitStack() as stack,
        ):
            paths = audio_review_fixture(
                Path(directory), event_id="voice:1", professionally_ready=True
            )
            kinds = ["builtin"]
            if importlib.util.find_spec("fastapi") and importlib.util.find_spec(
                "uvicorn"
            ):
                kinds.append("fastapi")
            servers = [
                stack.enter_context(server(kind, paths.root.parent)) for kind in kinds
            ]

            def url(item, tail="", **query):
                if item["kind"] == "fastapi":
                    query["workspace"] = str(paths.root.parent)
                suffix = "?" + urlencode(query) if query else ""
                return "/api/projects/audio-review/audio" + tail + suffix

            pages = [
                request(item["port"], "GET", url(item, kind="voice", limit=1))
                for item in servers
            ]
            self.assertTrue(all(status == 200 for status, _ in pages))
            self.assertTrue(all(page == pages[0][1] for _, page in pages))
            page = pages[0][1]
            body = {
                "expected_generation_id": page["generation_id"],
                "expected_proposal_sha256": page["events"][0]["proposal_sha256"],
                "status": "reviewed",
                "overrides": {},
                "review_notes": "test-only operator assertion",
                "confirm_operator_review": True,
            }
            original = (paths.data / "audio_intelligence.json").read_bytes()
            for item in servers:
                target = url(item, "/events/voice%3A1/review")
                with self.subTest(engine=item["kind"]):
                    for identifier in ("voice:1", "voice%3A1"):
                        status, event_page = request(
                            item["port"], "GET", url(item, "/events/" + identifier)
                        )
                        self.assertEqual(200, status, event_page)
                        self.assertEqual("voice:1", event_page["events"][0]["event_id"])
                    for identifier in ("voice%253A1", "voice%2F1", "voice%FF1"):
                        self.assertIn(
                            request(
                                item["port"], "GET", url(item, "/events/" + identifier)
                            )[0],
                            (400, 404),
                        )
                    self.assertEqual(
                        403,
                        request(
                            item["port"],
                            "PATCH",
                            target,
                            body=json.dumps(body),
                            headers={"Content-Type": "application/json"},
                        )[0],
                    )
                    self.assertEqual(
                        403,
                        request(
                            item["port"],
                            "PATCH",
                            target,
                            body=json.dumps(body),
                            headers={
                                **item["headers"],
                                "Origin": "https://untrusted.invalid",
                            },
                        )[0],
                    )
                    duplicate = json.dumps(body).replace(
                        '"status": "reviewed"',
                        '"status":"reviewed","status":"reviewed"',
                    )
                    self.assertEqual(
                        400,
                        request(
                            item["port"],
                            "PATCH",
                            target,
                            body=duplicate,
                            headers=item["headers"],
                        )[0],
                    )
                    self.assertEqual(
                        413, oversized_request(item["port"], target, item["headers"])
                    )
                    self.assertIn(
                        request(
                            item["port"], "GET", "/api/projects/../audio-review/audio"
                        )[0],
                        (400, 404),
                    )
            self.assertEqual(
                original, (paths.data / "audio_intelligence.json").read_bytes()
            )

            def apply(item):
                payload = {**body, "overrides": {"text": item["kind"]}}
                return request(
                    item["port"],
                    "PATCH",
                    url(item, "/events/voice%3A1/review"),
                    body=json.dumps(payload),
                    headers=item["headers"],
                )

            if len(servers) == 1:
                # Still test concurrent HTTP writes without the optional adapter.
                contenders = [servers[0], {**servers[0], "kind": "builtin"}]
            else:
                contenders = servers
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(apply, contenders))
            self.assertCountEqual([200, 409], [status for status, _ in results])
            saved = next(payload for status, payload in results if status == 200)
            self.assertTrue(saved["review_saved"])
            self.assertTrue(saved["report_regeneration_required"])
            self.assertEqual("review_pending", load_json(paths.manifest)["status"])
            self.assertFalse(verify_report_generation_manifest(paths)[0])

            builtin = servers[0]
            status, result = request(
                builtin["port"],
                "POST",
                "/api/projects/audio-review/report",
                body="{}",
                headers=builtin["headers"],
            )
            self.assertEqual(200, status, result)
            self.assertTrue(verify_report_generation_manifest(paths)[0])
            current = get_audio_event(paths, "voice:1")
            body.update(
                expected_generation_id=current["generation_id"],
                overrides=current["events"][0]["review"]["overrides"],
            )
            status, noop = request(
                builtin["port"],
                "PATCH",
                url(builtin, "/events/voice%3A1/review"),
                body=json.dumps(body),
                headers=builtin["headers"],
            )
            self.assertEqual(200, status)
            self.assertFalse(noop["changed"])
            self.assertFalse(noop["report_regeneration_required"])
            self.assertFalse(list(paths.root.rglob("*.xlsx")))
            self.assertFalse(list(paths.root.rglob("*.pdf")))

    @unittest.skipUnless(
        importlib.util.find_spec("fastapi") and importlib.util.find_spec("uvicorn"),
        "optional api extra not installed",
    )
    def test_fastapi_openapi_documents_explicit_review_body(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            server("fastapi", Path(directory)) as item,
        ):
            status, document = request(item["port"], "GET", "/openapi.json")
            self.assertEqual(200, status)
            self.assertEqual(
                request(item["port"], "GET", "/session"),
                request(item["port"], "GET", "/api/session"),
            )
            list_parameters = document["paths"]["/api/projects/{project_id}/audio"][
                "get"
            ]["parameters"]
            self.assertEqual(
                QUERY_KEYS | {"workspace"},
                {
                    parameter["name"]
                    for parameter in list_parameters
                    if parameter["in"] == "query"
                },
            )
            self.assertTrue(
                any(
                    parameter["name"] == "project_id" and parameter["in"] == "path"
                    for parameter in list_parameters
                )
            )
            schema = document["paths"][
                "/api/projects/{project_id}/audio/events/{event_id}/review"
            ]["patch"]["requestBody"]["content"]["application/json"]["schema"]
            self.assertEqual(REQUEST_KEYS, set(schema["properties"]))
            self.assertIs(
                schema["properties"]["confirm_operator_review"]["const"], True
            )
