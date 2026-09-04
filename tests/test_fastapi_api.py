from __future__ import annotations

import http.client
import importlib.util
import socket
import subprocess
import sys
import time
import unittest


class OptionalFastApiBoundaryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if importlib.util.find_spec("fastapi") is None or importlib.util.find_spec("uvicorn") is None:
            raise unittest.SkipTest("optional api extra is not installed")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            cls.port = int(sock.getsockname()[1])
        cls.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "video_analysis_mvp.api:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(cls.port),
                "--log-level",
                "warning",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if cls.process.poll() is not None:
                raise RuntimeError(f"optional API server exited with {cls.process.returncode}")
            try:
                status, _ = cls.request("GET", "/health")
                if status == 200:
                    return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("optional API server did not become ready")

    @classmethod
    def tearDownClass(cls) -> None:
        if not hasattr(cls, "process"):
            return
        cls.process.terminate()
        try:
            cls.process.wait(timeout=4)
        except subprocess.TimeoutExpired:
            cls.process.kill()
            cls.process.wait(timeout=4)

    @classmethod
    def request(
        cls,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        body: str | None = None,
    ) -> tuple[int, dict[str, object]]:
        connection = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=8)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            import json

            payload = json.loads(response.read() or b"{}")
            return response.status, payload
        finally:
            connection.close()

    @classmethod
    def chunked_request(
        cls,
        path: str,
        chunks: list[bytes],
        *,
        headers: dict[str, str],
    ) -> tuple[int, dict[str, object]]:
        connection = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=8)
        try:
            connection.putrequest("POST", path)
            for name, value in headers.items():
                connection.putheader(name, value)
            connection.putheader("Transfer-Encoding", "chunked")
            connection.endheaders()
            for chunk in chunks:
                connection.send(f"{len(chunk):X}\r\n".encode("ascii"))
                connection.send(chunk)
                connection.send(b"\r\n")
            connection.send(b"0\r\n\r\n")
            response = connection.getresponse()
            import json

            payload = json.loads(response.read() or b"{}")
            return response.status, payload
        finally:
            connection.close()

    def test_loopback_health_and_session_are_available(self) -> None:
        health_status, health = self.request("GET", "/health")
        session_status, session = self.request("GET", "/session")

        self.assertEqual(200, health_status)
        self.assertEqual({"status": "ok"}, health)
        self.assertEqual(200, session_status)
        self.assertGreater(len(str(session["csrf_token"])), 20)

    def test_export_routes_share_local_security_and_missing_project_boundary(self) -> None:
        missing_status, _payload = self.request(
            "GET",
            "/api/projects/definitely-missing-export-project/exports",
        )
        no_token_status, _payload = self.request(
            "POST",
            "/api/projects/definitely-missing-export-project/exports",
            headers={"Content-Type": "application/json"},
            body='{"formats":["xlsx"],"settings":{},"idempotency_key":"test"}',
        )
        self.assertEqual(404, missing_status)
        self.assertEqual(403, no_token_status)
        state_status, _payload = self.request(
            "GET",
            "/api/projects/definitely-missing-export-project/exports/state",
        )
        self.assertEqual(404, state_status)

    def test_host_and_csrf_boundaries_block_untrusted_mutations(self) -> None:
        bad_host_status, _ = self.request("GET", "/health", headers={"Host": "attacker.example"})
        no_token_status, _ = self.request(
            "POST",
            "/projects",
            headers={"Content-Type": "application/json"},
            body='{"source":"fixture.mp4"}',
        )

        self.assertEqual(403, bad_host_status)
        self.assertEqual(403, no_token_status)

    def test_origin_must_exactly_match_the_request_host(self) -> None:
        matching_status, _ = self.request(
            "GET",
            "/health",
            headers={"Origin": f"http://127.0.0.1:{self.port}"},
        )
        mismatched_status, payload = self.request(
            "GET",
            "/health",
            headers={"Origin": "http://localhost:65530"},
        )

        self.assertEqual(200, matching_status)
        self.assertEqual(403, mismatched_status)
        self.assertEqual("cross-origin request blocked", payload["error"])

    def test_cross_site_fetch_metadata_is_rejected_without_origin(self) -> None:
        cross_site_status, payload = self.request(
            "GET",
            "/health",
            headers={"Sec-Fetch-Site": "CrOsS-SiTe"},
        )
        same_origin_status, _ = self.request(
            "GET",
            "/health",
            headers={"Sec-Fetch-Site": "same-origin"},
        )

        self.assertEqual(403, cross_site_status)
        self.assertEqual("cross-site request blocked", payload["error"])
        self.assertEqual(200, same_origin_status)

    def test_service_endpoints_reject_url_sources_before_dispatch(self) -> None:
        _, session = self.request("GET", "/session")
        headers = {
            "Content-Type": "application/json",
            "X-VEW-CSRF": str(session["csrf_token"]),
        }
        source = f"http://127.0.0.1:{self.port}/health"

        for path in ("/projects", "/projects/ingest"):
            with self.subTest(path=path):
                status, payload = self.request(
                    "POST",
                    path,
                    headers=headers,
                    body=f'{{"source":"{source}"}}',
                )
                self.assertEqual(422, status, payload)
                self.assertIn("URL sources are not accepted", str(payload))

    def test_request_model_preserves_local_file_sources(self) -> None:
        from video_analysis_mvp.api import RunRequest

        request = RunRequest(source="fixtures/local-video.mp4")
        self.assertEqual("fixtures/local-video.mp4", request.source)

    def test_with_vision_accepts_only_a_json_boolean_and_invalid_values_never_dispatch(self) -> None:
        _, session = self.request("GET", "/session")
        _, projects_before = self.request("GET", "/projects")
        headers = {
            "Content-Type": "application/json",
            "X-VEW-CSRF": str(session["csrf_token"]),
        }
        for value in ('"true"', '"false"', "1", "0", "null"):
            with self.subTest(value=value):
                status, payload = self.request(
                    "POST",
                    "/projects",
                    headers=headers,
                    body=f'{{"source":"https://example.test/video","with_vision":{value}}}',
                )
                self.assertEqual(422, status, payload)
        _, projects_after = self.request("GET", "/projects")
        self.assertEqual(projects_before, projects_after)

    def test_explicit_and_chunked_bodies_over_one_mib_are_rejected_before_dispatch(self) -> None:
        _, session = self.request("GET", "/session")
        _, projects_before = self.request("GET", "/projects")
        headers = {
            "Content-Type": "application/json",
            "X-VEW-CSRF": str(session["csrf_token"]),
        }
        explicit_size = 1_048_654
        explicit_status, explicit_payload = self.request(
            "POST",
            "/projects",
            headers=headers,
            body="x" * explicit_size,
        )
        chunked_status, chunked_payload = self.chunked_request(
            "/projects",
            [b"x" * 400_000, b"y" * 400_000, b"z" * 248_654],
            headers=headers,
        )
        _, projects_after = self.request("GET", "/projects")

        self.assertEqual(413, explicit_status, explicit_payload)
        self.assertEqual(413, chunked_status, chunked_payload)
        self.assertEqual(projects_before, projects_after)


if __name__ == "__main__":
    unittest.main()
