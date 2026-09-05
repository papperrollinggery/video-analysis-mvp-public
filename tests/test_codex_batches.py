from __future__ import annotations

import copy
import json
import tempfile
import unittest
from unittest.mock import patch

from tests import test_readiness as readiness_fixtures
from tests.test_vision_receipts import _payload
from video_analysis_mvp.codex_analysis import (
    CodexAnalysisConflict,
    RESPONSE_SCHEMA,
    _build_request,
    apply_codex_analysis,
    codex_analysis_status,
    prepare_codex_analysis,
)
from video_analysis_mvp.codex_batches import next_codex_batch, submit_codex_batch
from video_analysis_mvp.readiness import evaluate_project_readiness
from video_analysis_mvp.schemas import Shot, dump_json, load_json
from video_analysis_mvp.visual import _build_visual_generation_receipt


class CodexBatchesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="codex-batches-")
        self.addCleanup(self.temp.cleanup)
        self.paths, self.media, self.shot = readiness_fixtures.ProjectReadinessIntegrityTest().project(self.temp.name, source="machine")
        (self.paths.assets / "audio.wav").write_bytes(b"synthetic-audio")

    def shots(self, count: int, *, triplets: bool = False) -> list[Shot]:
        shots = []
        for i in range(count):
            shot = self.shot.model_copy(deep=True)
            shot.shot_id = f"shot_{i + 1:04d}"
            shot.start_time = i * 2.0 / count
            shot.end_time = (i + 1) * 2.0 / count
            shot.duration = shot.end_time - shot.start_time
            if triplets:
                shot.frame_refs = [f"{shot.shot_id}_{position}.png" for position in ("start", "mid", "end")]
                shot.frame_ref = shot.primary_frame_ref = shot.frame_refs[1]
            else:
                shot.frame_ref = shot.primary_frame_ref = f"{shot.shot_id}.png"
                shot.frame_refs = [shot.frame_ref]
            for reference in shot.frame_refs:
                (self.paths.keyframes / reference).write_bytes(readiness_fixtures.PNG_1X1)
            shots.append(shot)
        dump_json(self.paths.data / "shots.json", shots)
        dump_json(self.paths.data / "visual_generation.json", _build_visual_generation_receipt(self.paths, shots, []))
        return shots

    def response(self, packet: dict) -> dict:
        template = copy.deepcopy(packet["response_template"])
        for row in template["analyses"]:
            row["analysis"] = _payload(f"Observed {row['shot_id']}", 0.8)
        return template

    def test_batches_resume_and_commit_only_when_complete(self) -> None:
        self.shots(5, triplets=True)
        original = (self.paths.data / "shots.json").read_bytes()
        packet = next_codex_batch(self.paths, batch_size=2)
        self.assertEqual(["shot_0001", "shot_0002"], [row["shot_id"] for row in packet["shots"]])
        self.assertEqual(["shot_0003"], [row["shot_id"] for row in packet["adjacent_context"]])
        self.assertEqual(3, len(packet["shots"][0]["frames"]))
        first = self.response(packet)
        self.assertEqual("checkpointed", submit_codex_batch(self.paths, first)["status"])
        self.assertEqual(original, (self.paths.data / "shots.json").read_bytes())
        self.assertEqual(2, codex_analysis_status(self.paths)["checkpointed_shot_count"])
        self.assertEqual("checkpointed", submit_codex_batch(self.paths, first)["status"])
        # A separate invocation loads only the remaining rows from disk.
        packet = next_codex_batch(self.paths, batch_size=2)
        self.assertEqual("shot_0003", packet["shots"][0]["shot_id"])
        submit_codex_batch(self.paths, self.response(packet))
        packet = next_codex_batch(self.paths, batch_size=2)
        last = self.response(packet)
        result = submit_codex_batch(self.paths, last)
        self.assertEqual("applied", result["status"])
        self.assertEqual(0, result["remaining_shot_count"])
        self.assertEqual("not_verified", result["quality"]["semantic_accuracy"])
        self.assertFalse(result["quality"]["single_frame_shot_ids"])
        self.assertEqual("applied", submit_codex_batch(self.paths, last)["status"])
        self.assertEqual("applied", next_codex_batch(self.paths)["status"])
        self.assertTrue(all(row["annotation_source"] == "codex" for row in load_json(self.paths.data / "shots.json")))
        self.assertFalse(list(self.paths.reports.rglob("*.xlsx")))
        readiness = evaluate_project_readiness(self.paths.root, require_persisted_receipt=False)
        self.assertTrue(all(row["agent_submission_verified"] for row in readiness["shot_results"]))
        self.assertFalse(readiness["professional_export_allowed"])

    def test_prepare_preserves_applied_request_and_legacy_receipt(self) -> None:
        for version in (1, 2):
            with self.subTest(version=version):
                self.shots(1)
                request = _build_request(self.paths, guide_version=version)
                dump_json(self.paths.data / "codex_analysis_request.json", request)
                response = {"schema_id": RESPONSE_SCHEMA, "project_id": self.paths.root.name, "request_id": request["request_id"], "analyses": [{"shot_id": "shot_0001", "analysis": _payload("A verified binding", 0.8)}]}
                apply_codex_analysis(self.paths, response)
                before = (self.paths.data / "codex_analysis_request.json").read_bytes()
                self.assertEqual("applied", prepare_codex_analysis(self.paths)["status"])
                self.assertEqual(before, (self.paths.data / "codex_analysis_request.json").read_bytes())
                self.assertEqual("applied", codex_analysis_status(self.paths)["status"])
                self.assertTrue(evaluate_project_readiness(self.paths.root, require_persisted_receipt=False)["shot_results"][0]["agent_submission_verified"])

    def test_more_than_256_shots_have_bounded_packets(self) -> None:
        self.shots(257)
        packet = next_codex_batch(self.paths, batch_size=12)
        self.assertEqual(257, packet["selected_shot_count"])
        self.assertEqual(12, len(packet["shots"]))
        self.assertLess(len(json.dumps(packet)), len((self.paths.data / "codex_analysis_request.json").read_bytes()) // 4)

    def test_duplicate_correction_requires_replace_and_preserves_other_rows(self) -> None:
        self.shots(2)
        first = self.response(next_codex_batch(self.paths, batch_size=1))
        submit_codex_batch(self.paths, first)
        changed = copy.deepcopy(first)
        changed["analyses"][0]["analysis"]["action"] = "A corrected observation"
        before = (self.paths.data / "codex_analysis_progress.json").read_bytes()
        with self.assertRaises(CodexAnalysisConflict):
            submit_codex_batch(self.paths, changed)
        self.assertEqual(before, (self.paths.data / "codex_analysis_progress.json").read_bytes())
        submit_codex_batch(self.paths, changed, replace=True)
        result = submit_codex_batch(self.paths, self.response(next_codex_batch(self.paths)))
        self.assertEqual("applied", result["status"])
        self.assertEqual("A corrected observation", load_json(self.paths.data / "shots.json")[0]["action"])

    def test_invalid_or_stale_batch_does_not_change_checkpoint_or_shots(self) -> None:
        self.shots(2, triplets=True)
        packet = next_codex_batch(self.paths, batch_size=1)
        response = self.response(packet)
        before = (self.paths.data / "shots.json").read_bytes()
        for mutate in (
            lambda value: value["analyses"][0].update(shot_id="unselected"),
            lambda value: value["analyses"][0]["analysis"].update(confidence=True),
            lambda value: value.update(analyses=[]),
            lambda value: value.update(project_id="other"),
        ):
            invalid = copy.deepcopy(response)
            mutate(invalid)
            with self.assertRaises(ValueError):
                submit_codex_batch(self.paths, invalid)
        (self.paths.keyframes / "shot_0001_end.png").write_bytes(b"changed supporting frame")
        with self.assertRaises(ValueError):
            submit_codex_batch(self.paths, response)
        self.assertEqual(before, (self.paths.data / "shots.json").read_bytes())
        self.assertFalse((self.paths.data / "codex_analysis_progress.json").exists())

    def test_failed_final_commit_can_finish_without_resubmitting_batches(self) -> None:
        packet = next_codex_batch(self.paths)
        with patch("video_analysis_mvp.codex_batches._apply_codex_analysis_locked", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                submit_codex_batch(self.paths, self.response(packet))
        self.assertEqual("ready_to_apply", next_codex_batch(self.paths)["status"])
        self.assertEqual("applied", submit_codex_batch(self.paths, finish=True)["status"])

    def test_receipt_write_failure_rolls_back_actual_shot_and_manifest_mutation(self) -> None:
        packet = next_codex_batch(self.paths)
        old_shots = (self.paths.data / "shots.json").read_bytes()
        old_manifest = self.paths.manifest.read_bytes()

        def fail_receipt(path, value):
            if path.name == "vision_annotations.json":
                raise OSError("receipt disk failure")
            dump_json(path, value)

        with patch("video_analysis_mvp.vision.dump_json", side_effect=fail_receipt):
            with self.assertRaisesRegex(OSError, "receipt disk failure"):
                submit_codex_batch(self.paths, self.response(packet))
        self.assertEqual(old_shots, (self.paths.data / "shots.json").read_bytes())
        self.assertEqual(old_manifest, self.paths.manifest.read_bytes())
        self.assertFalse((self.paths.data / "vision_annotations.json").exists())
        self.assertEqual("ready_to_apply", next_codex_batch(self.paths)["status"])
        self.assertEqual("applied", submit_codex_batch(self.paths, finish=True)["status"])
        self.assertFalse(list(self.paths.root.glob(".codex-analysis-rollback-*")))

    def test_empty_template_and_early_finish_are_not_valid_analysis(self) -> None:
        packet = next_codex_batch(self.paths)
        with self.assertRaises(ValueError):
            submit_codex_batch(self.paths, packet["response_template"])
        with self.assertRaises(ValueError):
            submit_codex_batch(self.paths, finish=True)

    def test_changed_evidence_does_not_adopt_old_checkpoint(self) -> None:
        self.shots(2)
        old = self.response(next_codex_batch(self.paths, batch_size=1))
        submit_codex_batch(self.paths, old)
        (self.paths.assets / "audio.wav").write_bytes(b"a new audio generation")
        packet = next_codex_batch(self.paths, batch_size=1)
        self.assertNotEqual(old["request_id"], packet["request_id"])
        self.assertEqual("shot_0001", packet["shots"][0]["shot_id"])
        with self.assertRaises(CodexAnalysisConflict):
            submit_codex_batch(self.paths, old)

    def test_human_shot_is_excluded_from_packets_and_never_overwritten(self) -> None:
        shots = self.shots(2)
        shots[0].annotation_source = "human"
        dump_json(self.paths.data / "shots.json", shots)
        original = load_json(self.paths.data / "shots.json")[0]
        packet = next_codex_batch(self.paths)
        self.assertEqual(["shot_0002"], [row["shot_id"] for row in packet["shots"]])
        submit_codex_batch(self.paths, self.response(packet))
        self.assertEqual(original, load_json(self.paths.data / "shots.json")[0])
        readiness = evaluate_project_readiness(self.paths.root, require_persisted_receipt=False)
        by_id = {row["shot_id"]: row for row in readiness["shot_results"]}
        self.assertTrue(by_id["shot_0001"]["human_assertion"])
        self.assertTrue(by_id["shot_0002"]["agent_submission_verified"])

    def test_later_human_review_does_not_revoke_other_bound_model_rows(self) -> None:
        self.shots(2)
        packet = next_codex_batch(self.paths)
        submit_codex_batch(self.paths, self.response(packet))
        shots = load_json(self.paths.data / "shots.json")
        shots[0]["annotation_source"] = "human"
        shots[0]["content_summary"] = "An operator correction"
        dump_json(self.paths.data / "shots.json", shots)
        readiness = evaluate_project_readiness(self.paths.root, require_persisted_receipt=False)
        by_id = {row["shot_id"]: row for row in readiness["shot_results"]}
        self.assertTrue(by_id["shot_0002"]["agent_submission_verified"])
        self.assertEqual("applied", prepare_codex_analysis(self.paths)["status"])
        self.assertEqual("applied", codex_analysis_status(self.paths)["status"])


if __name__ == "__main__":
    unittest.main()
