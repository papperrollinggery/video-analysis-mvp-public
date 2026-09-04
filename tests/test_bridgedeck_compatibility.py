from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import tempfile
import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from tests import test_readiness as readiness_fixtures
from tests.test_vision_receipts import PNG_1X1, _payload
from video_analysis_mvp.bridge_vision import (
    BRIDGE_PROVIDER_CONTRACT,
    MAX_BRIDGE_RESPONSE_BYTES,
    BridgeDeckError,
    analyze_bridgedeck_image,
    parse_bridgedeck_response,
)
from video_analysis_mvp.cli import main
from video_analysis_mvp.config import (
    RuntimeConfig,
    load_runtime_config,
    resolve_provider_key,
    save_runtime_config,
    validate_bridgedeck_config,
)
from video_analysis_mvp.readiness import (
    evaluate_project_readiness,
    has_vision_key,
    vision_provider_capability,
)
from video_analysis_mvp.schemas import load_json
from video_analysis_mvp.vision import OBSERVATION_FIELDS, annotate_project_with_vision

MODEL = "fixture-model"


def _response(analysis: dict[str, object], *, model: str = MODEL) -> dict[str, object]:
    return {
        "id": "resp_fixture",
        "status": "completed",
        "model": model,
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": json.dumps(analysis)}],
            }
        ],
    }


@contextmanager
def _bridge_server(
    payload: dict[str, object], *, status: int = 200, location: str | None = None
):
    records: list[dict[str, object]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            records.append(
                {
                    "path": self.path,
                    "headers": dict(self.headers),
                    "body": json.loads(raw),
                }
            )
            body = json.dumps(payload).encode()
            self.send_response(status)
            if location:
                self.send_header("Location", location)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield (
            f"http://127.0.0.1:{server.server_port}/accounts/fixture-account/v1",
            records,
        )
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


class BridgeDeckConfigTest(unittest.TestCase):
    def test_explicit_loopback_account_route_and_model_are_required(self) -> None:
        accepted = "http://127.0.0.1:8876/accounts/fixture-account/v1"
        self.assertEqual((accepted, MODEL), validate_bridgedeck_config(accepted, MODEL))
        for endpoint in (
            "http://127.0.0.1:8876/v1",
            "http://localhost:8876/accounts/fixture-account/v1",
            "http://example.test:8876/accounts/fixture-account/v1",
            "https://127.0.0.1:8876/accounts/fixture-account/v1",
            "http://127.0.0.1/accounts/fixture-account/v1",
            "http://127.0.0.1:0/accounts/fixture-account/v1",
            "http://127.0.0.1:8876/accounts/a%2Fb/v1",
            "http://user:password@127.0.0.1:8876/accounts/a/v1",
            accepted + "?token=synthetic",
        ):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                validate_bridgedeck_config(endpoint, MODEL)
        with self.assertRaises(ValueError):
            validate_bridgedeck_config(accepted, "")

    def test_old_config_is_compatible_and_bridge_never_resolves_ambient_keys(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self.assertEqual("", load_runtime_config(root).bridgedeck_base_url)
            with self.assertRaises(ValueError):
                save_runtime_config(root, {"vision_provider": "bridgedeck"})
            config = save_runtime_config(
                root,
                {
                    "vision_provider": "bridgedeck",
                    "bridgedeck_base_url": "http://127.0.0.1:8876/accounts/fixture-account/v1",
                    "bridgedeck_model": MODEL,
                },
            )
            with patch.dict(
                os.environ,
                {
                    "OPENAI_API_KEY": "do-not-forward",
                    "MINIMAX_API_KEY": "do-not-forward",
                },
                clear=True,
            ):
                self.assertEqual("", resolve_provider_key(config, "bridgedeck"))
                self.assertFalse(has_vision_key(root))
                eligible, diagnostic = vision_provider_capability(root)
            self.assertTrue(eligible)
            self.assertIn("live inference", diagnostic)
            self.assertNotIn("fixture-account", diagnostic)
            self.assertEqual(MODEL, load_runtime_config(root).bridgedeck_model)


class BridgeDeckTransportTest(unittest.TestCase):
    def test_invalid_raster_is_rejected_before_network_dispatch(self) -> None:
        with (
            patch(
                "video_analysis_mvp.bridge_vision.urllib.request.build_opener"
            ) as opener,
            self.assertRaisesRegex(BridgeDeckError, "complete valid raster"),
        ):
            analyze_bridgedeck_image(
                base_url="http://127.0.0.1:8876/accounts/fixture-account/v1",
                model=MODEL,
                image_bytes=b"not-an-image",
                media_type="image/png",
                instructions="fixture",
                prompt={},
                required_fields=["confidence"],
            )
        opener.assert_not_called()

    def test_real_loopback_protocol_preserves_images_and_schema_without_credentials(
        self,
    ) -> None:
        expected = _payload("Synthetic transport observation", 0.9)
        with (
            _bridge_server(_response(expected)) as (endpoint, records),
            patch.dict(
                os.environ,
                {
                    "OPENAI_API_KEY": "do-not-forward",
                    "MINIMAX_API_KEY": "do-not-forward",
                    "HTTP_PROXY": "http://127.0.0.1:1",
                    "HTTPS_PROXY": "http://127.0.0.1:1",
                    "NO_PROXY": "",
                },
                clear=True,
            ),
        ):
            result = analyze_bridgedeck_image(
                base_url=endpoint,
                model=MODEL,
                image_bytes=PNG_1X1,
                media_type="image/png",
                instructions="Observe the test image.",
                prompt={"shot_no": 1},
                required_fields=[*OBSERVATION_FIELDS, "confidence"],
            )
        self.assertEqual(expected, result)
        self.assertEqual(1, len(records))
        record = records[0]
        self.assertEqual("/accounts/fixture-account/v1/responses", record["path"])
        headers = {key.lower(): value for key, value in record["headers"].items()}
        self.assertNotIn("authorization", headers)
        self.assertNotIn("x-api-key", headers)
        self.assertNotIn("do-not-forward", json.dumps(record))
        body = record["body"]
        self.assertTrue(body["text"]["format"]["strict"])
        self.assertEqual("json_schema", body["text"]["format"]["type"])
        self.assertEqual("input_image", body["input"][0]["content"][1]["type"])
        self.assertTrue(
            body["input"][0]["content"][1]["image_url"].startswith(
                "data:image/png;base64,"
            )
        )
        self.assertFalse(body["stream"])
        self.assertFalse(body["store"])
        self.assertNotIn("max_output_tokens", body)

    def test_redirect_is_not_followed(self) -> None:
        with (
            _bridge_server({}, status=307, location="http://127.0.0.1:1/leak") as (
                endpoint,
                records,
            ),
            self.assertRaisesRegex(BridgeDeckError, "HTTP 307"),
        ):
            analyze_bridgedeck_image(
                base_url=endpoint,
                model=MODEL,
                image_bytes=PNG_1X1,
                media_type="image/png",
                instructions="fixture",
                prompt={},
                required_fields=["content_summary", "confidence"],
            )
        self.assertEqual(1, len(records))

    def test_incomplete_refusal_wrong_model_and_ambiguous_json_fail_closed(
        self,
    ) -> None:
        base = _response(_payload("fixture", 0.8))
        mutations = [
            lambda value: value.update(status="incomplete"),
            lambda value: value.update(
                incomplete_details={"reason": "max_output_tokens"}
            ),
            lambda value: value.update(model="different-model"),
            lambda value: value["output"][0].update(status="incomplete"),
            lambda value: value["output"][0]["content"][0].update(type="refusal"),
            lambda value: value["output"].append(copy.deepcopy(value["output"][0])),
            lambda value: value["output"][0]["content"][0].update(text='{"a":1,"a":2}'),
            lambda value: value["output"][0]["content"][0].update(
                text='{"confidence":NaN}'
            ),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                value = copy.deepcopy(base)
                mutate(value)
                with self.assertRaises(BridgeDeckError):
                    parse_bridgedeck_response(json.dumps(value).encode(), MODEL)
        with self.assertRaises(BridgeDeckError):
            parse_bridgedeck_response(b"event: response.completed\ndata: {}\n\n", MODEL)

    def test_bridge_annotation_uses_existing_receipt_and_no_other_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            paths, _media, _shot = (
                readiness_fixtures.ProjectReadinessIntegrityTest().project(
                    raw, source="machine"
                )
            )
            (paths.assets / "audio.wav").write_bytes(b"synthetic-audio")
            with _bridge_server(_response(_payload("Bridge observation", 0.9))) as (
                endpoint,
                _records,
            ):
                save_runtime_config(
                    paths.root.parent,
                    {
                        "vision_provider": "bridgedeck",
                        "bridgedeck_base_url": endpoint,
                        "bridgedeck_model": MODEL,
                    },
                )
                with (
                    patch.dict(os.environ, {}, clear=True),
                    patch("video_analysis_mvp.vision.analyze_frame") as openai,
                    patch(
                        "video_analysis_mvp.vision.analyze_frame_with_minimax_mcp"
                    ) as minimax,
                ):
                    result = annotate_project_with_vision(paths)
                openai.assert_not_called()
                minimax.assert_not_called()
            self.assertEqual("success", result.status)
            receipt = load_json(paths.data / "vision_annotations.json")
            self.assertEqual("bridgedeck", receipt["provider"])
            self.assertEqual("bridgedeck_responses", receipt["provider_source"])
            self.assertEqual(BRIDGE_PROVIDER_CONTRACT, receipt["provider_contract"])
            self.assertNotIn("fixture-account", receipt["endpoint_origin"])
            readiness = evaluate_project_readiness(
                paths.root, require_persisted_receipt=False
            )
            self.assertTrue(readiness["shot_results"][0]["provider_receipt_verified"])

    def test_model_mismatch_reports_compatibility_failure_without_changing_shots(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            paths, _media, _shot = (
                readiness_fixtures.ProjectReadinessIntegrityTest().project(
                    raw, source="machine"
                )
            )
            before = (paths.data / "shots.json").read_bytes()
            with _bridge_server(
                _response(_payload("wrong model", 0.9), model="different")
            ) as (endpoint, _records):
                config = RuntimeConfig(
                    bridgedeck_base_url=endpoint, bridgedeck_model=MODEL
                )
                with patch(
                    "video_analysis_mvp.vision.load_runtime_config", return_value=config
                ):
                    result = annotate_project_with_vision(paths, provider="bridgedeck")
            self.assertEqual("warning", result.status)
            self.assertIn("different or unreported model", result.diagnostics[0])
            self.assertEqual(before, (paths.data / "shots.json").read_bytes())

    def test_oversized_response_is_rejected(self) -> None:
        response = _response({"content_summary": "x" * MAX_BRIDGE_RESPONSE_BYTES})
        with (
            _bridge_server(response) as (endpoint, _records),
            self.assertRaisesRegex(BridgeDeckError, "bounded size"),
        ):
            analyze_bridgedeck_image(
                base_url=endpoint,
                model=MODEL,
                image_bytes=PNG_1X1,
                media_type="image/png",
                instructions="fixture",
                prompt={},
                required_fields=["content_summary", "confidence"],
            )

    def test_cli_explicit_bridge_route_requires_no_config_write_or_key(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            paths, _media, _shot = (
                readiness_fixtures.ProjectReadinessIntegrityTest().project(
                    raw, source="machine"
                )
            )
            with _bridge_server(
                _response(_payload("CLI fixture observation", 0.9))
            ) as (endpoint, records):
                output = io.StringIO()
                with (
                    patch.dict(os.environ, {}, clear=True),
                    contextlib.redirect_stdout(output),
                ):
                    code = main(
                        [
                            "--workspace",
                            str(paths.root.parent),
                            "vision",
                            paths.root.name,
                            "--provider",
                            "bridgedeck",
                            "--base-url",
                            endpoint,
                            "--model",
                            MODEL,
                        ]
                    )
            self.assertEqual(0, code)
            self.assertEqual("success", json.loads(output.getvalue())["status"])
            self.assertEqual(1, len(records))
            self.assertFalse(
                (paths.root.parent / "_settings" / "runtime_config.json").exists()
            )
