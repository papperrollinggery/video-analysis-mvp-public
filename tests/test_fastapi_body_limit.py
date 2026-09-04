from __future__ import annotations

import asyncio
import importlib.util
import json
import unittest
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import patch

if importlib.util.find_spec("fastapi") is None:
    raise unittest.SkipTest("optional api extra is not installed")

from video_analysis_mvp.api import MAX_REQUEST_BODY_BYTES, RequestBodyLimitMiddleware


class RequestBodyLimitTest(unittest.TestCase):
    @staticmethod
    def run_request(
        chunks: list[bytes],
        *,
        content_length: int | None,
        application: Callable[[dict[str, Any], Any, Any], Awaitable[None]],
    ) -> tuple[int, dict[str, object]]:
        headers: list[tuple[bytes, bytes]] = [(b"content-type", b"application/json")]
        if content_length is not None:
            headers.append((b"content-length", str(content_length).encode("ascii")))
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/bounded",
            "raw_path": b"/bounded",
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 8787),
        }
        queue = [
            {
                "type": "http.request",
                "body": chunk,
                "more_body": index < len(chunks) - 1,
            }
            for index, chunk in enumerate(chunks)
        ] or [{"type": "http.request", "body": b"", "more_body": False}]
        sent: list[dict[str, Any]] = []

        async def receive() -> dict[str, Any]:
            return queue.pop(0)

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        asyncio.run(RequestBodyLimitMiddleware(application)(scope, receive, send))
        start = next(message for message in sent if message["type"] == "http.response.start")
        body = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
        return int(start["status"]), json.loads(body)

    def test_explicit_1048654_byte_body_is_rejected_before_endpoint(self) -> None:
        called = False

        async def endpoint(_scope: dict[str, Any], _receive: Any, _send: Any) -> None:
            nonlocal called
            called = True

        size = 1_048_654
        status, payload = self.run_request(
            [b"x" * size],
            content_length=size,
            application=endpoint,
        )

        self.assertEqual(413, status)
        self.assertFalse(called)
        self.assertIn(str(MAX_REQUEST_BODY_BYTES), str(payload["error"]))

    def test_chunked_1048654_byte_body_is_rejected_without_buffering_all_chunks(self) -> None:
        called = False
        chunks_consumed = 0

        async def endpoint(scope: dict[str, Any], receive: Any, send: Any) -> None:
            nonlocal called, chunks_consumed
            more = True
            while more:
                message = await receive()
                chunks_consumed += 1
                more = bool(message.get("more_body"))
            called = True
            response = json.dumps({"ok": True}).encode()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": response})

        chunks = [b"x" * 400_000, b"y" * 400_000, b"z" * 248_654]
        status, payload = self.run_request(chunks, content_length=None, application=endpoint)

        self.assertEqual(413, status)
        self.assertFalse(called)
        # The middleware preflights at most one MiB and does not invoke the
        # downstream application when a later chunk crosses the cap.
        self.assertEqual(0, chunks_consumed)
        self.assertIn(str(MAX_REQUEST_BODY_BYTES), str(payload["error"]))

    def test_body_at_limit_reaches_endpoint(self) -> None:
        called = False

        async def endpoint(scope: dict[str, Any], receive: Any, send: Any) -> None:
            nonlocal called
            body = bytearray()
            more = True
            while more:
                message = await receive()
                body.extend(message.get("body", b""))
                more = bool(message.get("more_body"))
            called = True
            response = json.dumps({"size": len(body)}).encode()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": response})

        status, payload = self.run_request(
            [b"x" * (MAX_REQUEST_BODY_BYTES // 2), b"y" * (MAX_REQUEST_BODY_BYTES // 2)],
            content_length=None,
            application=endpoint,
        )

        self.assertEqual(200, status)
        self.assertTrue(called)
        self.assertEqual(MAX_REQUEST_BODY_BYTES, payload["size"])

    def test_slow_body_returns_408_before_endpoint(self) -> None:
        called = False
        sent: list[dict[str, Any]] = []
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/bounded",
            "headers": [],
        }

        async def endpoint(_scope: dict[str, Any], receive: Any, _send: Any) -> None:
            nonlocal called
            await receive()
            called = True

        async def receive() -> dict[str, Any]:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        with patch("video_analysis_mvp.api.REQUEST_BODY_TIMEOUT_SECONDS", 0.01):
            asyncio.run(RequestBodyLimitMiddleware(endpoint)(scope, receive, send))

        start = next(message for message in sent if message["type"] == "http.response.start")
        body = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
        self.assertEqual(408, start["status"])
        self.assertFalse(called)
        self.assertEqual({"error": "request body read timed out"}, json.loads(body))


if __name__ == "__main__":
    unittest.main()
