from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_audio_review import audio_review_fixture
from video_analysis_mvp.audio_providers import (
    RESPONSE_SCHEMA,
    apply_audio_provider_response,
    prepare_audio_provider_request,
    run_configured_audio_adapter,
)
from video_analysis_mvp.audio_synthesis import audio_timeline_source
from video_analysis_mvp.config import RuntimeConfig, save_runtime_config
from video_analysis_mvp.workspace_api import runtime_settings_payload


class AudioProviderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="audio-provider-")
        self.addCleanup(self.temp.cleanup)
        self.paths = audio_review_fixture(Path(self.temp.name))
        self.before_dataset = (self.paths.data / "audio_intelligence.json").read_bytes()
        self.before_receipt = (self.paths.data / "audio_intelligence_generation.json").read_bytes()
        self.adapter_index = 0

    def adapter(self, body: str, *, name: str | None = None) -> Path:
        self.adapter_index += 1
        path = Path(self.temp.name) / (name or f"fixture-adapter-{self.adapter_index}")
        path.write_text(f"#!{sys.executable}\n{body}\n", encoding="utf-8")
        path.chmod(0o700)
        return path

    def config(self, executable: Path | str, *, timeout: int = 3) -> RuntimeConfig:
        return RuntimeConfig(
            audio_adapter_executable=str(executable),
            audio_adapter_timeout_seconds=timeout,
        )

    def assert_baseline_unchanged(self) -> None:
        self.assertEqual(self.before_dataset, (self.paths.data / "audio_intelligence.json").read_bytes())
        self.assertEqual(self.before_receipt, (self.paths.data / "audio_intelligence_generation.json").read_bytes())

    def test_disabled_missing_timeout_crash_and_invalid_output_preserve_baseline(self) -> None:
        cases = [
            (RuntimeConfig(), "disabled", False),
            (self.config(Path(self.temp.name) / "missing"), "missing_or_unsafe", False),
            (self.config(self.adapter("import time; time.sleep(2)"), timeout=1), "timeout", True),
            (self.config(self.adapter("raise SystemExit(7)")), "crash", True),
            (
                self.config(self.adapter("import sys; sys.stdout.write('x' * (17 * 1024 * 1024))")),
                "output_limit",
                True,
            ),
            (self.config(self.adapter("print('not-json')")), "invalid_response", True),
        ]
        for config, reason, called in cases:
            with self.subTest(reason=reason):
                result = run_configured_audio_adapter(self.paths, config=config)
                self.assertEqual("fallback", result["status"])
                self.assertEqual(reason, result["reason"])
                self.assertIs(called, result["provider_called"])
                self.assertTrue(result["baseline_preserved"])
                self.assert_baseline_unchanged()

    def test_success_adds_validated_adapter_evidence_and_preserves_baseline(self) -> None:
        executable = self.adapter(
            """import json, os, pathlib, sys
request = json.load(sys.stdin)
assert pathlib.Path(sys.argv[1]).is_file()
assert "OPENAI_API_KEY" not in os.environ and "HF_TOKEN" not in os.environ
timeline = request["baseline_timeline"]
source_id = "classification-fixture-1"
timeline["sources"].append({
    "source_id": source_id, "capability": "classification", "source_type": "adapter",
    "adapter": pathlib.Path(sys.argv[0]).name, "adapter_version": "1", "engine": "fixture",
    "engine_version": "1", "model": "fixture-model", "device": "cpu", "status": "produced",
    "diagnostics": []})
timeline["capabilities"]["classification"] = {"status": "produced", "source_id": source_id, "reason": None}
timeline["events"].append({
    "event_id": "classification-fixture-event", "start_time": 0.1, "end_time": 0.2,
    "kind": "sfx", "source_id": source_id,
    "proposal": {"label": "impact candidate", "text": "", "language": "unknown",
        "speaker_id": None, "voice_role": "unknown", "energy": None, "onset_density": None,
        "estimated_bpm": None, "confidence": 0.7, "verification": "model_interpreted"},
    "review": None})
timeline["events"].sort(key=lambda item: (item["start_time"], item["end_time"], item["event_id"]))
print(json.dumps({"schema_id": "audio-adapter-response/v1", "request_id": request["request_id"], "timeline": timeline}))"""
        )

        with patch.dict(
            "os.environ",
            {"OPENAI_API_KEY": "must-not-reach-adapter", "HF_TOKEN": "must-not-reach-adapter"},
            clear=False,
        ):
            result = run_configured_audio_adapter(self.paths, config=self.config(executable))

        self.assertEqual("applied", result["status"])
        self.assertTrue(result["baseline_preserved"])
        self.assertTrue(result["provider_called"])
        self.assertFalse(result["model_identity_verified"])
        timeline, _binding = audio_timeline_source(self.paths)
        self.assertIsNotNone(timeline)
        self.assertEqual("produced", timeline["capabilities"]["classification"]["status"])
        self.assertIn("voice-1", {item["event_id"] for item in timeline["events"]})
        self.assertIn("classification-fixture-event", {item["event_id"] for item in timeline["events"]})

    def test_codex_prepare_apply_uses_same_contract_without_claiming_execution_identity(self) -> None:
        request = prepare_audio_provider_request(self.paths)
        timeline = json.loads(json.dumps(request["baseline_timeline"]))
        source_id = "classification-codex-1"
        timeline["sources"].append({
            "source_id": source_id,
            "capability": "classification",
            "source_type": "adapter",
            "adapter": "codex-current-task",
            "adapter_version": "1",
            "engine": "host-managed-unverified",
            "engine_version": None,
            "model": "host-managed-unverified",
            "device": None,
            "status": "produced",
            "diagnostics": [],
        })
        timeline["capabilities"]["classification"] = {"status": "produced", "source_id": source_id, "reason": None}
        timeline["events"].append({
            "event_id": "classification-codex-event",
            "start_time": 0.2,
            "end_time": 0.3,
            "kind": "music",
            "source_id": source_id,
            "proposal": {
                "label": "music candidate",
                "text": "",
                "language": "unknown",
                "speaker_id": None,
                "voice_role": "unknown",
                "energy": None,
                "onset_density": None,
                "estimated_bpm": None,
                "confidence": 0.6,
                "verification": "model_interpreted",
            },
            "review": None,
        })
        timeline["events"].sort(key=lambda item: (item["start_time"], item["end_time"], item["event_id"]))
        response = {"schema_id": RESPONSE_SCHEMA, "request_id": request["request_id"], "timeline": timeline}

        result = apply_audio_provider_response(self.paths, request, response, adapter="codex-current-task")

        self.assertEqual("applied", result["status"])
        self.assertFalse(result["provider_called"])
        self.assertFalse(result["model_identity_verified"])

    def test_invalid_or_destructive_response_never_commits(self) -> None:
        request = prepare_audio_provider_request(self.paths)
        for mutate in (
            lambda timeline: timeline["events"].pop(0),
            lambda timeline: timeline.update(media_duration_seconds=999),
            lambda timeline: timeline["events"][0].update(review={}),
        ):
            with self.subTest(mutate=mutate):
                timeline = json.loads(json.dumps(request["baseline_timeline"]))
                mutate(timeline)
                response = {"schema_id": RESPONSE_SCHEMA, "request_id": request["request_id"], "timeline": timeline}
                with self.assertRaises(ValueError):
                    apply_audio_provider_response(self.paths, request, response, adapter="codex-current-task")
                self.assert_baseline_unchanged()

    def test_tampered_request_body_with_a_current_id_is_rejected(self) -> None:
        request = prepare_audio_provider_request(self.paths)
        forged = json.loads(json.dumps(request))
        forged["baseline_timeline"]["events"].pop(0)
        response = {
            "schema_id": RESPONSE_SCHEMA,
            "request_id": request["request_id"],
            "timeline": forged["baseline_timeline"],
        }
        with self.assertRaisesRegex(ValueError, "stale"):
            apply_audio_provider_response(
                self.paths,
                forged,
                response,
                adapter="codex-current-task",
            )
        self.assert_baseline_unchanged()

    def test_external_executable_named_codex_current_task_still_reports_provider_call(self) -> None:
        executable = self.adapter(
            """import json, pathlib, sys
request = json.load(sys.stdin)
timeline = request["baseline_timeline"]
source_id = "classification-name-collision"
timeline["sources"].append({"source_id": source_id, "capability": "classification", "source_type": "adapter", "adapter": pathlib.Path(sys.argv[0]).name, "adapter_version": "1", "engine": "fixture", "engine_version": "1", "model": "fixture", "device": "cpu", "status": "produced", "diagnostics": []})
timeline["capabilities"]["classification"] = {"status": "produced", "source_id": source_id, "reason": None}
timeline["events"].append({"event_id": "classification-name-event", "start_time": 0.3, "end_time": 0.4, "kind": "sfx", "source_id": source_id, "proposal": {"label": "candidate", "text": "", "language": "unknown", "speaker_id": None, "voice_role": "unknown", "energy": None, "onset_density": None, "estimated_bpm": None, "confidence": 0.5, "verification": "model_interpreted"}, "review": None})
timeline["events"].sort(key=lambda item: (item["start_time"], item["end_time"], item["event_id"]))
print(json.dumps({"schema_id": "audio-adapter-response/v1", "request_id": request["request_id"], "timeline": timeline}))""",
            name="codex-current-task",
        )
        result = run_configured_audio_adapter(self.paths, config=self.config(executable))
        self.assertEqual("applied", result["status"])
        self.assertTrue(result["provider_called"])

    def test_codex_cannot_claim_a_model_identity_inside_the_timeline(self) -> None:
        request = prepare_audio_provider_request(self.paths)
        timeline = json.loads(json.dumps(request["baseline_timeline"]))
        source_id = "classification-forged-model"
        timeline["sources"].append({
            "source_id": source_id, "capability": "classification", "source_type": "adapter",
            "adapter": "codex-current-task", "adapter_version": "1", "engine": "host-managed-unverified",
            "engine_version": "claimed-verified-build-2026", "model": "host-managed-unverified", "device": None,
            "status": "produced", "diagnostics": []})
        timeline["capabilities"]["classification"] = {"status": "produced", "source_id": source_id, "reason": None}
        timeline["events"].append({
            "event_id": "classification-forged-event", "start_time": 0.4, "end_time": 0.5,
            "kind": "music", "source_id": source_id,
            "proposal": {"label": "candidate", "text": "", "language": "unknown", "speaker_id": None,
                "voice_role": "unknown", "energy": None, "onset_density": None, "estimated_bpm": None,
                "confidence": 0.5, "verification": "model_interpreted"}, "review": None})
        timeline["events"].sort(key=lambda item: (item["start_time"], item["end_time"], item["event_id"]))
        response = {"schema_id": RESPONSE_SCHEMA, "request_id": request["request_id"], "timeline": timeline}
        with self.assertRaisesRegex(ValueError, "model identity"):
            apply_audio_provider_response(self.paths, request, response, adapter="codex-current-task")
        self.assert_baseline_unchanged()

    def test_direct_apply_cannot_claim_a_different_adapter_identity(self) -> None:
        request = prepare_audio_provider_request(self.paths)
        response = {
            "schema_id": RESPONSE_SCHEMA,
            "request_id": request["request_id"],
            "timeline": request["baseline_timeline"],
        }
        with self.assertRaisesRegex(ValueError, "reserved"):
            apply_audio_provider_response(
                self.paths,
                request,
                response,
                adapter="gpt-claimed-verified",
            )
        self.assert_baseline_unchanged()

    def test_direct_apply_enforces_response_size_cap(self) -> None:
        request = prepare_audio_provider_request(self.paths)
        response = {
            "schema_id": RESPONSE_SCHEMA,
            "request_id": request["request_id"],
            "timeline": json.loads(json.dumps(request["baseline_timeline"])),
        }
        with (
            patch("video_analysis_mvp.audio_providers.MAX_ADAPTER_RESPONSE_BYTES", 100),
            self.assertRaisesRegex(ValueError, "bounded size"),
        ):
            apply_audio_provider_response(
                self.paths,
                request,
                response,
                adapter="codex-current-task",
            )
        self.assert_baseline_unchanged()

    def test_executable_disappearing_after_validation_returns_fallback(self) -> None:
        executable = self.adapter("raise SystemExit(0)")
        with patch("video_analysis_mvp.audio_providers.subprocess.Popen", side_effect=FileNotFoundError):
            result = run_configured_audio_adapter(self.paths, config=self.config(executable))
        self.assertEqual("fallback", result["status"])
        self.assertEqual("missing_or_unsafe", result["reason"])
        self.assertFalse(result["provider_called"])
        self.assert_baseline_unchanged()

    def test_runtime_settings_disclose_capability_not_private_executable_path(self) -> None:
        executable = self.adapter("raise SystemExit(0)")
        workspace = self.paths.root.parent
        save_runtime_config(
            workspace,
            {
                "audio_adapter_executable": str(executable),
                "audio_adapter_timeout_seconds": "45",
            },
        )

        payload = runtime_settings_payload(workspace)

        self.assertEqual(
            {
                "configured": True,
                "timeout_seconds": 45,
                "live_inference_verified": False,
                "baseline_fallback": True,
            },
            payload["audio_adapter"],
        )
        self.assertNotIn(str(executable), json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
