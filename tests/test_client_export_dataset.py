from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_audio_review import audio_review_fixture
from video_analysis_mvp.audio_intelligence import stage_and_commit_audio_intelligence
from video_analysis_mvp.audio_review import apply_audio_review, get_audio_event
from video_analysis_mvp.client_export_dataset import (
    ClientExportDatasetError,
    _client_text,
    build_client_export_dataset,
    validate_client_export_dataset,
    write_client_export_dataset,
)
from video_analysis_mvp.schemas import Scene, Shot, dump_json, load_json
from video_analysis_mvp.synthesis import synthesize, verify_report_generation_manifest
from video_analysis_mvp.visual import _build_visual_generation_receipt


class ClientExportDatasetTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="vew-client-dataset-")
        self.addCleanup(self.temp.cleanup)
        self.paths = audio_review_fixture(Path(self.temp.name))

    def test_build_is_deterministic_bound_and_has_no_render_side_effect(self):
        before = {str(path.relative_to(self.paths.root)): path.read_bytes() for path in self.paths.root.rglob("*") if path.is_file()}
        first = build_client_export_dataset(self.paths)
        second = build_client_export_dataset(self.paths)
        self.assertEqual(first, second)
        self.assertEqual(first, validate_client_export_dataset(first))
        self.assertEqual("client-export-dataset/v1", first["schema_id"])
        self.assertEqual(first["dataset_digest"], first["dataset_id"])
        self.assertEqual("draft_only", first["delivery_status"]["state"])
        self.assertFalse(first["delivery_status"]["professional_export_allowed"])
        self.assertEqual(1, len(first["shots"]))
        self.assertTrue({"voice", "mixed"}.issubset({event["kind"] for event in first["audio"]["events"]}))
        self.assertEqual(before, {str(path.relative_to(self.paths.root)): path.read_bytes() for path in self.paths.root.rglob("*") if path.is_file()})
        self.assertFalse(list(self.paths.root.rglob("*.xlsx")))
        self.assertFalse(list(self.paths.root.rglob("*.pdf")))

    def test_explicit_write_uses_one_stable_json_slot_only(self):
        first = write_client_export_dataset(self.paths)
        path = self.paths.data / "client_export_dataset.json"
        first_bytes = path.read_bytes()
        second = write_client_export_dataset(self.paths)
        self.assertEqual(first, second)
        self.assertEqual(first_bytes, path.read_bytes())
        self.assertEqual(first, json.loads(first_bytes))
        self.assertFalse(list(self.paths.root.rglob("*.xlsx")))
        self.assertFalse(list(self.paths.root.rglob("*.pdf")))

    def test_stale_report_fails_closed_without_replacing_current_dataset(self):
        write_client_export_dataset(self.paths)
        path = self.paths.data / "client_export_dataset.json"
        previous = path.read_bytes()
        shots = load_json(self.paths.data / "shots.json")
        shots[0]["content_summary"] = "changed after Finalize"
        dump_json(self.paths.data / "shots.json", shots)
        self.assertFalse(verify_report_generation_manifest(self.paths)[0])
        with self.assertRaisesRegex(ClientExportDatasetError, "current committed report"):
            write_client_export_dataset(self.paths)
        self.assertEqual(previous, path.read_bytes())

    def test_formula_text_is_dual_encoded_and_private_paths_fail(self):
        formula = _client_text("  =SUM(A1:A2)", "formula")
        self.assertEqual("  =SUM(A1:A2)", formula["text"])
        self.assertEqual("'  =SUM(A1:A2)", formula["spreadsheet_text"])
        self.assertTrue(formula["formula_neutralized"])
        with self.assertRaisesRegex(ClientExportDatasetError, "private absolute path"):
            _client_text("see /Users/example/private.mov", "private")
        for safe in ("室内/室外", "日/夜", "A / B", "16:9 / 9:16"):
            with self.subTest(safe=safe):
                self.assertEqual(safe, _client_text(safe, "shot wording")["text"])
        self.assertEqual("Visit /pricing today", _client_text("Visit /pricing today", "screen text")["text"])
        with self.assertRaisesRegex(ClientExportDatasetError, "private absolute path"):
            _client_text("／Users／example／private.mov", "fullwidth private")
        with self.assertRaisesRegex(ClientExportDatasetError, "formula neutralization"):
            _client_text("=" + "x" * (256 * 1024 - 1), "max formula")

        from video_analysis_mvp.client_export_dataset import _source_name
        self.assertEqual("ACME confidential.mp4", _source_name(r"C:\videos\ACME confidential.mp4", "fallback"))

    def test_manifest_bytes_must_remain_the_same_generation_snapshot(self):
        from video_analysis_mvp import client_export_dataset as module

        original_reader = module.read_regular_bytes
        manifest_reads = 0

        def switch_after_snapshot(path, **kwargs):
            nonlocal manifest_reads
            payload = original_reader(path, **kwargs)
            if Path(path) == self.paths.manifest:
                manifest_reads += 1
                if manifest_reads == 2:
                    return payload + b"\n"
            return payload

        with (
            patch.object(module, "read_regular_bytes", side_effect=switch_after_snapshot),
            self.assertRaisesRegex(ClientExportDatasetError, "changed while building"),
        ):
            build_client_export_dataset(self.paths)

    def test_captured_shot_bytes_must_match_the_bound_readiness_digest(self):
        from video_analysis_mvp import client_export_dataset as module

        original_reader = module.read_regular_bytes
        changed = load_json(self.paths.data / "shots.json")
        changed[0]["content_summary"] = "TRANSIENT-NOT-IN-GENERATION"
        changed_bytes = json.dumps(changed, ensure_ascii=False, indent=2).encode("utf-8")

        def transient_shots(path, **kwargs):
            if Path(path) == self.paths.data / "shots.json":
                return changed_bytes
            return original_reader(path, **kwargs)

        with (
            patch.object(module, "read_regular_bytes", side_effect=transient_shots),
            self.assertRaisesRegex(ClientExportDatasetError, "captured shots"),
        ):
            build_client_export_dataset(self.paths)

    def test_proposal_language_is_a_formula_safe_text_cell(self):
        data = load_json(self.paths.data / "audio_intelligence.json")
        voice = next(event for event in data["events"] if event["event_id"] == "voice-1")
        voice["proposal"]["language"] = "=1+1"
        params = load_json(self.paths.data / "audio_intelligence_generation.json")["parameters"]
        stage_and_commit_audio_intelligence(self.paths, data, parameters=params)
        synthesize(self.paths)
        dataset = build_client_export_dataset(self.paths)
        exported = next(event for event in dataset["audio"]["events"] if event["event_id"] == "voice-1")
        self.assertEqual("=1+1", exported["original_proposal"]["language"]["text"])
        self.assertEqual("'=1+1", exported["original_proposal"]["language"]["spreadsheet_text"])

    def test_narrative_scene_membership_is_not_invented_or_lost(self):
        shots = [Shot.model_validate(item) for item in load_json(self.paths.data / "shots.json")]
        scene = Scene(
            scene_id="scene-opening", start_time=0, end_time=2,
            shot_ids=[shots[0].shot_id], scene_function="Opening evidence", pace_label="measured slowly",
        )
        dump_json(self.paths.data / "scenes.json", [scene])
        dump_json(self.paths.data / "visual_generation.json", _build_visual_generation_receipt(self.paths, shots, [scene]))
        synthesize(self.paths)
        dataset = build_client_export_dataset(self.paths)
        self.assertEqual(["scene-opening"], dataset["shots"][0]["scene_ids"])
        self.assertEqual([shots[0].shot_id], dataset["scenes"][0]["shot_ids"])
        self.assertEqual("Opening evidence", dataset["scenes"][0]["function"]["text"])
        dataset["shots"][0]["scene_ids"] = []
        self._redigest(dataset)
        with self.assertRaisesRegex(ClientExportDatasetError, "scene references"):
            validate_client_export_dataset(dataset)
        duplicate = Scene(
            scene_id="scene-duplicate", start_time=0, end_time=2,
            shot_ids=[shots[0].shot_id, shots[0].shot_id],
        )
        dump_json(self.paths.data / "scenes.json", [duplicate])
        dump_json(self.paths.data / "visual_generation.json", _build_visual_generation_receipt(self.paths, shots, [duplicate]))
        synthesize(self.paths)
        with self.assertRaisesRegex(ClientExportDatasetError, "scene coverage"):
            build_client_export_dataset(self.paths)

    def test_long_text_explicit_blank_rejected_voice_and_mixed_are_preserved(self):
        shots = [Shot.model_validate(item) for item in load_json(self.paths.data / "shots.json")]
        shots[0].content_summary = "中" * 20_000
        shots[0].onscreen_text = "=CLIENT_FORMULA"
        shots[0].timecode = "=1+1"
        dump_json(self.paths.data / "shots.json", shots)
        dump_json(self.paths.data / "visual_generation.json", _build_visual_generation_receipt(self.paths, shots, []))
        page = get_audio_event(self.paths, "voice-1")
        voice = page["events"][0]
        apply_audio_review(self.paths, "voice-1", {
            "expected_generation_id": page["generation_id"],
            "expected_proposal_sha256": voice["proposal_sha256"],
            "status": "rejected",
            "overrides": {},
            "review_notes": "Explicitly excluded from client wording.",
            "confirm_operator_review": True,
        })
        synthesize(self.paths)
        dataset = build_client_export_dataset(self.paths)
        shot = dataset["shots"][0]
        self.assertEqual("中" * 20_000, shot["text"]["content_summary"]["text"])
        self.assertTrue(shot["text"]["onscreen_text"]["formula_neutralized"])
        self.assertTrue(shot["timecode"]["formula_neutralized"])
        voice_event = next(event for event in dataset["audio"]["events"] if event["event_id"] == "voice-1")
        self.assertIsNone(voice_event["effective_proposal"])
        self.assertEqual("Original VO", voice_event["original_proposal"]["text"]["text"])
        self.assertTrue(any(event["kind"] == "mixed" for event in dataset["audio"]["events"]))
        for link in shot["audio"]["event_links"]:
            self.assertIn(link["kind"], {"voice", "music", "sfx", "silence", "mixed"})
            self.assertIsInstance(link["continues_to_next"], bool)

    def test_nested_schema_rejects_renderer_breaking_shapes_even_with_new_digest(self):
        baseline = build_client_export_dataset(self.paths)
        mutations = [
            lambda value: value.update(project=None),
            lambda value: value.update(scenes="not-a-list"),
            lambda value: value["shots"][0].pop("text"),
            lambda value: value["audio"]["events"][0].pop("kind"),
            lambda value: value["audio"].update(event_index={}),
            lambda value: value["shots"][0].update(frame={
                "path": "assets/keyframes/frame.jpg", "present": True, "sha256": "x",
                "size_bytes": -1, "media_type": "=FORMULA", "width": "wide", "height": 0,
                "failure": _client_text("", "frame failure"),
            }),
            lambda value: value["shots"][0].update(frame={
                "path": "assets/keyframes/frame.webp", "present": True, "sha256": "a" * 64,
                "size_bytes": 1, "media_type": "image/webp", "width": 1, "height": 1,
                "failure": _client_text("", "frame failure"),
            }),
            lambda value: value["unresolved_items"][0].update(scope="s" * 129),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                value = json.loads(json.dumps(baseline))
                mutate(value)
                self._redigest(value)
                with self.assertRaises(ClientExportDatasetError):
                    validate_client_export_dataset(value)

    def test_validator_accepts_missing_frame_but_rejects_digest_and_unsafe_paths(self):
        dataset = build_client_export_dataset(self.paths)
        dataset["shots"][0]["frame"] = {
            "path": None, "present": False, "sha256": None, "size_bytes": None,
            "media_type": None, "width": None, "height": None,
            "failure": _client_text("Primary frame unavailable", "frame failure"),
        }
        base = dict(dataset)
        base.pop("dataset_digest")
        base.pop("dataset_id")
        from video_analysis_mvp.client_export_dataset import _canonical_digest
        dataset["dataset_digest"] = dataset["dataset_id"] = _canonical_digest(base)
        self.assertFalse(validate_client_export_dataset(dataset)["shots"][0]["frame"]["present"])
        dataset["shots"][0]["frame"]["path"] = "/private/frame.jpg"
        base = {key: value for key, value in dataset.items() if key not in {"dataset_digest", "dataset_id"}}
        dataset["dataset_digest"] = dataset["dataset_id"] = _canonical_digest(base)
        with self.assertRaisesRegex(ClientExportDatasetError, "project-relative"):
            validate_client_export_dataset(dataset)

    @staticmethod
    def _redigest(value):
        from video_analysis_mvp.client_export_dataset import _canonical_digest

        base = {key: item for key, item in value.items() if key not in {"dataset_digest", "dataset_id"}}
        value["dataset_digest"] = value["dataset_id"] = _canonical_digest(base)


if __name__ == "__main__":
    unittest.main()
