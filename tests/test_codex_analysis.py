from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from tests import test_readiness as readiness_fixtures
from tests import test_web as web_fixtures
from tests.test_vision_receipts import _payload
from video_analysis_mvp.cli import main
from video_analysis_mvp.codex_analysis import (
    MAX_RESPONSE_BYTES,
    RESPONSE_SCHEMA,
    CodexAnalysisConflict,
    apply_codex_analysis,
    codex_analysis_status,
    prepare_codex_analysis,
)
from video_analysis_mvp.readiness import evaluate_project_readiness
from video_analysis_mvp.schemas import dump_json, load_json
from video_analysis_mvp.web import MAX_REQUEST_BODY_BYTES
from video_analysis_mvp.workspace_api import ApiError, dispatch_api


class CodexAnalysisTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="codex-analysis-")
        self.paths, self.media, self.shot = (
            readiness_fixtures.ProjectReadinessIntegrityTest().project(
                self.temp.name, source="machine"
            )
        )
        (self.paths.assets / "audio.wav").write_bytes(b"synthetic-audio")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def response(self, request: dict[str, object]) -> dict[str, object]:
        return {
            "schema_id": RESPONSE_SCHEMA,
            "project_id": self.paths.root.name,
            "request_id": request["request_id"],
            "analyses": [
                {
                    "shot_id": self.shot.shot_id,
                    "analysis": _payload("Codex observed a frame", 0.8),
                }
            ],
        }

    def test_prepare_and_apply_need_no_keys_or_provider_calls(self) -> None:
        before_shots = (self.paths.data / "shots.json").read_bytes()
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("video_analysis_mvp.vision.analyze_frame") as external,
            patch(
                "video_analysis_mvp.vision.analyze_frame_with_minimax_mcp"
            ) as minimax,
        ):
            first = prepare_codex_analysis(self.paths)
            second = prepare_codex_analysis(self.paths)
            self.assertEqual(first, second)
            self.assertEqual(
                before_shots, (self.paths.data / "shots.json").read_bytes()
            )
            self.assertEqual("prepared", codex_analysis_status(self.paths)["status"])
            result = apply_codex_analysis(self.paths, self.response(first["request"]))
            external.assert_not_called()
            minimax.assert_not_called()
        self.assertEqual("applied", result["status"])
        self.assertTrue(result["review_required"])
        self.assertFalse(result["model_identity_verified"])
        self.assertEqual("applied", codex_analysis_status(self.paths)["status"])
        shot = load_json(self.paths.data / "shots.json")[0]
        self.assertEqual("codex", shot["annotation_source"])
        self.assertEqual("blocked", shot["readiness_status"])
        self.assertEqual("Codex observed a frame", shot["content_summary"])
        manifest = load_json(self.paths.manifest)
        self.assertEqual("review_pending", manifest["status"])
        self.assertEqual(
            "codex_analysis_applied", manifest["report_invalidation"]["reason"]
        )
        self.assertNotIn("report_generation", manifest)
        self.assertFalse(list(self.paths.root.rglob("*.xlsx")))
        self.assertFalse(list(self.paths.root.rglob("*.pdf")))

    def test_codex_submission_is_not_human_or_verified_provider_execution(self) -> None:
        request = prepare_codex_analysis(self.paths)["request"]
        apply_codex_analysis(self.paths, self.response(request))
        readiness = evaluate_project_readiness(
            self.paths.root, require_persisted_receipt=False
        )
        self.assertFalse(readiness["professional_export_allowed"])
        result = readiness["shot_results"][0]
        self.assertFalse(result["human_assertion"])
        self.assertFalse(result["provider_receipt_verified"])
        self.assertTrue(result["agent_submission_verified"])
        self.assertEqual("agent_submission_bound", result["annotation_state"])

    def test_stale_or_incomplete_submission_is_never_reported_as_bound(self) -> None:
        request = prepare_codex_analysis(self.paths)["request"]
        apply_codex_analysis(self.paths, self.response(request))
        receipt_path = self.paths.data / "vision_annotations.json"
        original = load_json(receipt_path)
        for mutate in (
            lambda value: value.update(
                agent_submission={"schema_version": 1, "model_identity_verified": False}
            ),
            lambda value: value["agent_submission"].update(result_sha256="0" * 64),
            lambda value: value["agent_submission"].update(request_id="0" * 64),
        ):
            with self.subTest(mutate=mutate):
                receipt = copy.deepcopy(original)
                mutate(receipt)
                dump_json(receipt_path, receipt)
                readiness = evaluate_project_readiness(
                    self.paths.root, require_persisted_receipt=False
                )
                self.assertFalse(
                    readiness["shot_results"][0]["agent_submission_verified"]
                )
                self.assertEqual("stale", codex_analysis_status(self.paths)["status"])
        dump_json(receipt_path, original)
        (self.paths.assets / "audio.wav").write_bytes(b"changed-audio-evidence")
        readiness = evaluate_project_readiness(
            self.paths.root, require_persisted_receipt=False
        )
        self.assertFalse(readiness["shot_results"][0]["agent_submission_verified"])
        self.assertEqual("stale", codex_analysis_status(self.paths)["status"])

    def test_invalid_submissions_never_mutate_shots(self) -> None:
        request = prepare_codex_analysis(self.paths)["request"]
        before = (self.paths.data / "shots.json").read_bytes()
        mutations = [
            lambda value: value.update(model="a-made-up-verified-model"),
            lambda value: value["analyses"].append(copy.deepcopy(value["analyses"][0])),
            lambda value: value.update(analyses=[]),
            lambda value: value["analyses"][0]["analysis"].update(confidence=True),
            lambda value: value["analyses"][0]["analysis"].update(
                annotation_source="human"
            ),
            lambda value: value.update(project_id="other-project"),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                response = self.response(request)
                mutate(response)
                with self.assertRaises(ValueError):
                    apply_codex_analysis(self.paths, response)
                self.assertEqual(before, (self.paths.data / "shots.json").read_bytes())

    def test_stale_shot_or_audio_binding_is_rejected(self) -> None:
        for target in ("shot", "audio"):
            with self.subTest(target=target):
                request = prepare_codex_analysis(self.paths)["request"]
                if target == "shot":
                    rows = load_json(self.paths.data / "shots.json")
                    rows[0]["content_summary"] = "Concurrent edit"
                    dump_json(self.paths.data / "shots.json", rows)
                else:
                    (self.paths.assets / "audio.wav").write_bytes(b"changed-audio")
                before = (self.paths.data / "shots.json").read_bytes()
                with self.assertRaises(CodexAnalysisConflict):
                    apply_codex_analysis(self.paths, self.response(request))
                self.assertEqual(before, (self.paths.data / "shots.json").read_bytes())

    def test_human_records_remain_protected(self) -> None:
        request = prepare_codex_analysis(self.paths)["request"]
        rows = load_json(self.paths.data / "shots.json")
        rows[0]["annotation_source"] = "human"
        rows[0]["dialogue"] = ""
        dump_json(self.paths.data / "shots.json", rows)
        before = (self.paths.data / "shots.json").read_bytes()
        with self.assertRaises(ValueError):
            apply_codex_analysis(self.paths, self.response(request))
        self.assertEqual(before, (self.paths.data / "shots.json").read_bytes())

    def test_cli_and_api_use_the_same_request_contract(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(
                [
                    "--workspace",
                    str(self.paths.root.parent),
                    "codex",
                    "prepare",
                    self.paths.root.name,
                ]
            )
        self.assertEqual(0, result)
        prepared = json.loads(output.getvalue())
        status, api = dispatch_api(
            self.paths.root.parent,
            "GET",
            f"/api/projects/{self.paths.root.name}/codex",
            "",
            b"{}",
        )
        self.assertEqual(200, status)
        self.assertEqual(prepared["request"]["request_id"], api["request_id"])
        status, applied = dispatch_api(
            self.paths.root.parent,
            "POST",
            f"/api/projects/{self.paths.root.name}/codex/apply",
            "",
            json.dumps(self.response(prepared["request"])).encode(),
        )
        self.assertEqual(200, status)
        self.assertEqual("applied", applied["status"])

    def test_api_prepare_rejects_credential_or_configuration_fields(self) -> None:
        with self.assertRaises(ApiError) as caught:
            dispatch_api(
                self.paths.root.parent,
                "POST",
                f"/api/projects/{self.paths.root.name}/codex/prepare",
                "",
                b'{"api_key":"synthetic-not-a-real-key"}',
            )
        self.assertEqual(400, caught.exception.status)

    def test_duplicate_response_keys_are_rejected_at_api_boundary(self) -> None:
        with self.assertRaises(ApiError) as caught:
            dispatch_api(
                self.paths.root.parent,
                "POST",
                f"/api/projects/{self.paths.root.name}/codex/apply",
                "",
                b'{"request_id":"first","request_id":"second"}',
            )
        self.assertEqual(400, caught.exception.status)

    def test_real_http_prepare_apply_and_body_limit_match_cli_contract(self) -> None:
        self.assertEqual(MAX_REQUEST_BODY_BYTES, MAX_RESPONSE_BYTES)
        server = web_fixtures.WebContractTest()
        server.setUp()
        try:
            paths, _media, shot = (
                readiness_fixtures.ProjectReadinessIntegrityTest().project(
                    str(server.workspace), source="machine"
                )
            )
            (paths.assets / "audio.wav").write_bytes(b"synthetic-audio")
            status, _headers, raw = server._request("/api/session")
            self.assertEqual(200, status)
            headers = {
                "Content-Type": "application/json",
                "X-VEW-CSRF": json.loads(raw)["csrf_token"],
            }
            prefix = f"/api/projects/{paths.root.name}/codex"
            status, _headers, raw = server._request(
                prefix + "/prepare", method="POST", headers=headers, body=b"{}"
            )
            self.assertEqual(200, status)
            prepared = json.loads(raw)["request"]
            boundary_body = b"{}" + b" " * (MAX_RESPONSE_BYTES - 2)
            status, _headers, _raw = server._request(
                prefix + "/apply", method="POST", headers=headers, body=boundary_body
            )
            self.assertEqual(400, status)
            status, _headers, _raw = server._request(
                prefix + "/apply",
                method="POST",
                headers={**headers, "Content-Length": str(MAX_RESPONSE_BYTES + 1)},
                body=None,
            )
            self.assertEqual(413, status)
            response = {
                "schema_id": RESPONSE_SCHEMA,
                "project_id": paths.root.name,
                "request_id": prepared["request_id"],
                "analyses": [
                    {
                        "shot_id": shot.shot_id,
                        "analysis": _payload("HTTP submitted observation", 0.8),
                    }
                ],
            }
            status, _headers, raw = server._request(
                prefix + "/apply",
                method="POST",
                headers=headers,
                body=json.dumps(response).encode(),
            )
            self.assertEqual(200, status)
            self.assertEqual("applied", json.loads(raw)["status"])
        finally:
            server.tearDown()
