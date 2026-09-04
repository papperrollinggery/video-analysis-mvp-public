from __future__ import annotations

import copy
import hashlib
import io
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from tests.test_audio import write_pcm
from tests.test_audio_intelligence_schema import _dataset, _proposal
from tests.test_report_generation import _install_source_generation_receipts, _media
from video_analysis_mvp.audio import analyze_audio
from video_analysis_mvp.audio_intelligence import (
    audio_intelligence_binding,
    stage_and_commit_audio_intelligence,
)
from video_analysis_mvp.audio_review import (
    apply_audio_review,
    get_audio_event,
    read_audio_review,
    read_review_request,
)
from video_analysis_mvp.paths import ProjectPaths
from video_analysis_mvp.schemas import Shot, dump_json, load_json
from video_analysis_mvp.synthesis import (
    _normalize_shots,
    synthesize,
    verify_report_generation_manifest,
)
from video_analysis_mvp.visual import _build_visual_generation_receipt
from video_analysis_mvp.workspace_api import ApiError, dispatch_api


def audio_review_fixture(
    root: Path,
    *,
    event_id: str = "voice-1",
    professionally_ready: bool = False,
) -> ProjectPaths:
    paths = ProjectPaths(root.resolve() / "audio-review")
    paths.ensure()
    _install_source_generation_receipts(paths)
    media = _media(paths)
    shots = [Shot.model_validate(item) for item in load_json(paths.data / "shots.json")]
    _normalize_shots(media, shots)
    dump_json(paths.data / "shots.json", shots)
    dump_json(
        paths.data / "visual_generation.json",
        _build_visual_generation_receipt(paths, shots, []),
    )
    write_pcm(paths.assets / "audio.wav", 2)
    analyze_audio(media, paths, skip_asr=True)
    data = load_json(paths.data / "audio_intelligence.json")
    source = copy.deepcopy(_dataset()["sources"][1])
    data["sources"].append(source)
    data["capabilities"]["asr"] = {
        "status": "produced",
        "source_id": source["source_id"],
        "reason": None,
    }
    data["events"].append(
        {
            "event_id": event_id,
            "start_time": 0.2,
            "end_time": 1.8,
            "kind": "voice",
            "source_id": source["source_id"],
            "proposal": _proposal(label="", text="Original VO"),
            "review": None,
        }
    )
    data["events"].sort(
        key=lambda event: (event["start_time"], event["end_time"], event["event_id"])
    )
    parameters = load_json(paths.data / "audio_intelligence_generation.json")[
        "parameters"
    ]
    stage_and_commit_audio_intelligence(paths, data, parameters=parameters)
    if professionally_ready:
        _make_audio_review_project_professionally_ready(paths)
    synthesize(paths)
    return paths


def ready_client_export_fixture(root: Path) -> ProjectPaths:
    paths = audio_review_fixture(root, professionally_ready=True)
    page = get_audio_event(paths, "voice-1")
    event = page["events"][0]
    apply_audio_review(
        paths,
        "voice-1",
        {
            "expected_generation_id": page["generation_id"],
            "expected_proposal_sha256": event["proposal_sha256"],
            "status": "reviewed",
            "overrides": {"text": "Reviewed VO"},
            "review_notes": "test-only operator assertion",
            "confirm_operator_review": True,
        },
    )
    synthesize(paths)
    return paths


def _make_audio_review_project_professionally_ready(paths: ProjectPaths) -> None:
    media = load_json(paths.data / "media_package.json")
    master = paths.ingest / "master.mp4"
    review = paths.assets / "review.mp4"
    master.write_bytes(b"test-master-media")
    review.write_bytes(b"test-review-media")

    def receipt(path: Path) -> dict[str, object]:
        return {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size_bytes": path.stat().st_size,
            "duration_seconds": 2.0,
            "frame_rate": 24.0,
            "resolution": "1920x1080",
            "aspect_ratio": 16 / 9,
        }

    media["metadata"]["media_receipt"] = {
        "schema_version": "1.0",
        "master": receipt(master),
        "review": receipt(review),
    }
    dump_json(paths.data / "media_package.json", media)

    shot = load_json(paths.data / "shots.json")[0]
    shot.update(
        {
            "story_beat": "observation",
            "content_summary": "A documented frame",
            "subject": "person",
            "action": "walking",
            "shot_scale": "medium",
            "camera_angle": "eye level",
            "camera_motion": "static",
            "composition": "centered",
            "boundary_confidence": "high",
            "visual_confidence": 0.9,
            "confidence": 0.9,
            "annotation_source": "human",
            "readiness_status": "ready",
        }
    )
    frame = paths.keyframes / shot["frame_ref"]
    Image.new("RGB", (8, 8), (40, 50, 60)).save(frame, format="JPEG")
    validated_shot = Shot.model_validate(shot)
    dump_json(paths.data / "shots.json", [validated_shot])
    dump_json(
        paths.data / "visual_generation.json",
        _build_visual_generation_receipt(paths, [validated_shot], []),
    )
    timeline = load_json(paths.data / "audio_intelligence.json")
    parameters = load_json(paths.data / "audio_intelligence_generation.json")[
        "parameters"
    ]
    stage_and_commit_audio_intelligence(paths, timeline, parameters=parameters)


class AudioReviewServiceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="vew-review-api-")
        self.addCleanup(self.temp.cleanup)
        self.paths = audio_review_fixture(Path(self.temp.name))

    def request(self, text="Reviewed VO"):
        page = get_audio_event(self.paths, "voice-1")
        return {
            "expected_generation_id": page["generation_id"],
            "expected_proposal_sha256": page["events"][0]["proposal_sha256"],
            "status": "reviewed",
            "overrides": {"text": text},
            "review_notes": "test-only operator assertion",
            "confirm_operator_review": True,
        }

    def snapshot(self):
        return {
            str(path.relative_to(self.paths.root)): path.read_bytes()
            for path in self.paths.root.rglob("*")
            if path.is_file()
        }

    def test_get_is_read_only_paged_and_shot_filter_uses_bound_associations(self):
        before = self.snapshot()
        page = read_audio_review(
            self.paths, {"kind": "voice", "limit": "1", "shot_id": "shot_0001"}
        )
        self.assertEqual("audio-review/v1", page["schema_id"])
        self.assertTrue(page["available"])
        self.assertEqual(1, len(page["events"]))
        self.assertEqual("voice-1", page["events"][0]["event_id"])
        self.assertTrue(page["events"][0]["requires_review"])
        self.assertEqual(0.2, page["events"][0]["shot_link"]["overlap_start"])
        self.assertEqual(before, self.snapshot())

    def test_missing_timeline_is_unknown_and_existing_audio_cli_produces_it(self):
        from video_analysis_mvp.cli import main

        for name in ("audio_intelligence.json", "audio_intelligence_generation.json"):
            (self.paths.data / name).unlink()
        before = self.snapshot()
        page = read_audio_review(self.paths)
        self.assertFalse(page["available"])
        self.assertIsNone(page["generation_id"])
        self.assertIsNone(page["requires_review_count"])
        self.assertEqual("all audio events", page["counts_scope"])
        self.assertIn("never instructions", page["data_trust"])
        self.assertTrue(
            all(item["status"] == "unknown" for item in page["capabilities"].values())
        )
        self.assertEqual(before, self.snapshot())
        with self.assertRaises(ApiError) as error:
            get_audio_event(self.paths, "voice-1")
        self.assertEqual(404, error.exception.status)
        output = io.StringIO()
        with redirect_stdout(output):
            status = main(
                [
                    "--workspace",
                    str(self.paths.root.parent),
                    "audio",
                    "audio-review",
                    "--skip-asr",
                ]
            )
        self.assertEqual(0, status, output.getvalue())
        self.assertEqual("success", json.loads(output.getvalue())["status"])
        self.assertTrue(read_audio_review(self.paths)["available"])
        self.assertFalse(list(self.paths.root.rglob("*.xlsx")))
        self.assertFalse(list(self.paths.root.rglob("*.pdf")))

    def test_review_preserves_original_parameters_and_invalidates_without_exporting(
        self,
    ):
        before = load_json(self.paths.data / "audio_intelligence_generation.json")
        original = get_audio_event(self.paths, "voice-1")["events"][0]["proposal"]
        result = apply_audio_review(self.paths, "voice-1", self.request(""))
        self.assertTrue(result["review_saved"])
        self.assertTrue(result["changed"])
        self.assertTrue(result["report_regeneration_required"])
        event = get_audio_event(self.paths, "voice-1")["events"][0]
        self.assertEqual(original, event["proposal"])
        self.assertEqual("", event["effective_proposal"]["text"])
        self.assertEqual("human_reviewed", event["effective_proposal"]["verification"])
        self.assertEqual(
            before["parameters"],
            load_json(self.paths.data / "audio_intelligence_generation.json")[
                "parameters"
            ],
        )
        self.assertEqual("review_pending", load_json(self.paths.manifest)["status"])
        self.assertFalse(verify_report_generation_manifest(self.paths)[0])
        self.assertFalse(list(self.paths.root.rglob("*.xlsx")))
        self.assertFalse(list(self.paths.root.rglob("*.pdf")))

    def test_stale_generation_proposal_and_confirmation_fail_before_writes(self):
        for updates, code in (
            ({"expected_generation_id": "a" * 64}, "stale_generation"),
            ({"expected_proposal_sha256": "b" * 64}, "stale_proposal"),
            ({"confirm_operator_review": False}, "operator_confirmation_required"),
            ({"verification": "measured"}, "invalid_review"),
        ):
            with self.subTest(code=code):
                request = self.request()
                request.update(updates)
                before = self.snapshot()
                with self.assertRaises(ApiError) as error:
                    apply_audio_review(self.paths, "voice-1", request)
                self.assertEqual(code, error.exception.details["code"])
                self.assertEqual(before, self.snapshot())

    def test_concurrent_reviews_are_compare_and_swap_not_last_writer_wins(self):
        request = self.request()

        def attempt(text):
            body = copy.deepcopy(request)
            body["overrides"]["text"] = text
            try:
                return apply_audio_review(self.paths, "voice-1", body)["review_saved"]
            except ApiError as error:
                return error.status

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, ("first", "second")))
        self.assertCountEqual([True, 409], results)
        audio_intelligence_binding(self.paths)

    def test_identical_review_is_noop_and_does_not_invalidate_a_finalized_report(self):
        apply_audio_review(self.paths, "voice-1", self.request())
        synthesize(self.paths)
        before = self.snapshot()
        result = apply_audio_review(self.paths, "voice-1", self.request())
        self.assertFalse(result["changed"])
        self.assertFalse(result["report_regeneration_required"])
        self.assertEqual(before, self.snapshot())

    def test_patch_omission_preserves_operator_corrections_but_explicit_empty_resets(
        self,
    ):
        apply_audio_review(self.paths, "voice-1", self.request("corrected text"))
        request = self.request()
        request.pop("overrides")
        request.pop("review_notes")
        request["status"] = "needs_work"
        apply_audio_review(self.paths, "voice-1", request)
        event = get_audio_event(self.paths, "voice-1")["events"][0]
        self.assertEqual({"text": "corrected text"}, event["review"]["overrides"])
        self.assertEqual(
            "test-only operator assertion", event["review"]["review_notes"]
        )
        request = self.request()
        request.update(overrides={}, review_notes="")
        apply_audio_review(self.paths, "voice-1", request)
        event = get_audio_event(self.paths, "voice-1")["events"][0]
        self.assertEqual("Original VO", event["effective_proposal"]["text"])
        self.assertEqual("", event["review"]["review_notes"])

    def test_failed_commit_does_not_claim_saved_or_publish_old_report(self):
        request = self.request()
        before = (self.paths.data / "audio_intelligence.json").read_bytes()
        with (
            patch(
                "video_analysis_mvp.audio_review.stage_and_commit_audio_intelligence",
                side_effect=OSError("synthetic failure"),
            ),
            self.assertRaises(ApiError) as error,
        ):
            apply_audio_review(self.paths, "voice-1", request)
        self.assertEqual("audio_commit_failed", error.exception.details["code"])
        self.assertEqual(
            before, (self.paths.data / "audio_intelligence.json").read_bytes()
        )
        self.assertEqual("review_pending", load_json(self.paths.manifest)["status"])

    def test_input_change_after_snapshot_is_rejected_not_rebound(self):
        from video_analysis_mvp.workspace_api import _invalidate_report_for_review

        request = self.request()
        before = (self.paths.data / "audio_intelligence.json").read_bytes()

        def change_input(*args, **kwargs):
            _invalidate_report_for_review(*args, **kwargs)
            media = load_json(self.paths.data / "media_package.json")
            media["source"] = "changed-source.mp4"
            dump_json(self.paths.data / "media_package.json", media)

        with (
            patch(
                "video_analysis_mvp.audio_review._invalidate_report_for_review",
                side_effect=change_input,
            ),
            self.assertRaises(ApiError) as error,
        ):
            apply_audio_review(self.paths, "voice-1", request)
        self.assertEqual(409, error.exception.status)
        self.assertEqual(
            before, (self.paths.data / "audio_intelligence.json").read_bytes()
        )

    def test_request_file_rejects_duplicate_keys_symlink_and_excess_bytes(self):
        request = Path(self.temp.name) / "request.json"
        request.write_text('{"status":"reviewed","status":"rejected"}')
        with self.assertRaises(ApiError):
            read_review_request(request)
        link = request.with_name("link.json")
        link.symlink_to(request)
        with self.assertRaises(ApiError):
            read_review_request(link)
        request.write_bytes(b" " * (1024 * 1024 + 1))
        with self.assertRaises(ApiError):
            read_review_request(request)

    def test_workspace_api_and_service_return_the_same_page_and_error(self):
        status, page = dispatch_api(
            self.paths.root.parent,
            "GET",
            "/api/projects/audio-review/audio",
            "kind=voice&limit=1",
            b"",
        )
        self.assertEqual(200, status)
        self.assertEqual(
            read_audio_review(self.paths, {"kind": "voice", "limit": "1"}), page
        )
        bad = self.request()
        bad["expected_generation_id"] = "a" * 64
        with self.assertRaises(ApiError) as error:
            dispatch_api(
                self.paths.root.parent,
                "PATCH",
                "/api/projects/audio-review/audio/events/voice-1/review",
                "",
                json.dumps(bad).encode(),
            )
        self.assertEqual("stale_generation", error.exception.details["code"])

    def test_cli_matches_service_and_returns_stale_review_error(self):
        from video_analysis_mvp.cli import main

        output = io.StringIO()
        with redirect_stdout(output):
            status = main(
                [
                    "--workspace",
                    str(self.paths.root.parent),
                    "audio-review",
                    "list",
                    "audio-review",
                    "--kind",
                    "voice",
                    "--limit",
                    "1",
                ]
            )
        self.assertEqual(0, status)
        self.assertEqual(
            read_audio_review(self.paths, {"kind": "voice", "limit": 1, "offset": 0}),
            json.loads(output.getvalue()),
        )
        request = Path(self.temp.name) / "review.json"
        request.write_text(json.dumps(self.request()))
        with redirect_stdout(io.StringIO()):
            status = main(
                [
                    "--workspace",
                    str(self.paths.root.parent),
                    "audio-review",
                    "apply",
                    "audio-review",
                    "voice-1",
                    "--request",
                    str(request),
                ]
            )
        self.assertEqual(0, status)
        error = io.StringIO()
        with redirect_stderr(error):
            status = main(
                [
                    "--workspace",
                    str(self.paths.root.parent),
                    "audio-review",
                    "apply",
                    "audio-review",
                    "voice-1",
                    "--request",
                    str(request),
                ]
            )
        self.assertEqual(1, status)
        self.assertEqual(
            "stale_generation", json.loads(error.getvalue())["error"]["details"]["code"]
        )

    def test_invalid_filters_and_pagination_revision_are_rejected(self):
        before = self.snapshot()
        for options in (
            {"limit": 0},
            {"limit": 201},
            {"offset": -1},
            {"kind": []},
            {"review_status": {}},
            {"extra": "no"},
        ):
            with self.subTest(options=options), self.assertRaises(ApiError):
                read_audio_review(self.paths, options)
        with self.assertRaises(ApiError) as error:
            read_audio_review(self.paths, {"expected_generation_id": "b" * 64})
        self.assertEqual(409, error.exception.status)
        self.assertEqual(before, self.snapshot())

    def test_rejected_and_needs_work_reviews_do_not_become_effective_text(self):
        for status in ("needs_work", "rejected"):
            request = self.request()
            request.update(status=status, overrides={})
            apply_audio_review(self.paths, "voice-1", request)
            event = get_audio_event(self.paths, "voice-1")["events"][0]
            self.assertIsNone(event["effective_proposal"])
            self.assertEqual(status == "needs_work", event["requires_review"])

    def test_malformed_body_is_rejected_before_review_mutation(self):
        before = self.snapshot()
        path = "/api/projects/audio-review/audio/events/voice-1/review"
        for body in (
            b'{"status":"reviewed","status":"reviewed"}',
            b'{"x":' + b"[" * 1200 + b"0" + b"]" * 1200 + b"}",
            b" " * (1024 * 1024 + 1),
        ):
            with self.subTest(size=len(body)), self.assertRaises(ApiError) as error:
                dispatch_api(self.paths.root.parent, "PATCH", path, "", body)
            self.assertIn(error.exception.status, (400, 413))
        self.assertEqual(before, self.snapshot())

    def test_fastapi_moves_blocking_domain_io_off_event_loop(self):
        import asyncio
        import importlib.util
        from types import SimpleNamespace

        if importlib.util.find_spec("fastapi") is None:
            self.skipTest("optional api extra is not installed")
        from video_analysis_mvp.api import _audio_review_dispatch

        def dispatch(*_args):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return 200, {"ok": True}
            raise AssertionError("blocking domain I/O ran on event loop")

        with patch(
            "video_analysis_mvp.workspace_api.dispatch_api", side_effect=dispatch
        ):
            result = asyncio.run(
                _audio_review_dispatch(
                    SimpleNamespace(method="GET", query_params={}),
                    "audio-review",
                    "",
                    str(self.paths.root.parent),
                )
            )
        self.assertEqual(200, result.status_code)
