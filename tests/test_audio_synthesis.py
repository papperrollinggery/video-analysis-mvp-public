from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_audio import media_for, write_pcm
from tests.test_audio_intelligence_schema import _dataset, _proposal
from video_analysis_mvp.audio import analyze_audio
from video_analysis_mvp.audio_intelligence import (
    proposal_sha256,
    stage_and_commit_audio_intelligence,
)
from video_analysis_mvp.audio_synthesis import (
    apply_audio_associations,
    associate_audio_events,
    build_project_audio_associations,
)
from video_analysis_mvp.paths import ProjectPaths
from video_analysis_mvp.schemas import Scene, Shot, dump_json, load_json


def shots():
    return [
        Shot(shot_id="s1", shot_no=1, start_time=0, end_time=2, duration=2),
        Shot(shot_id="s2", shot_no=2, start_time=2, end_time=5, duration=3),
    ]


class AudioAssociationsTest(unittest.TestCase):
    def test_cross_shot_text_keeps_original_event_and_half_open_overlap(self):
        source = _dataset()
        before = copy.deepcopy(source)
        result = associate_audio_events(source, shots(), media_duration=10)
        voice = next(event for event in result["events"] if event["kind"] == "voice")
        self.assertEqual((1, 3), (voice["start_time"], voice["end_time"]))
        for shot, expected in zip(result["shots"], ((1, 2), (2, 3))):
            link = next(
                link
                for link in shot["event_links"]
                if link["event_id"] == voice["event_id"]
            )
            self.assertEqual(expected, (link["overlap_start"], link["overlap_end"]))
            self.assertEqual(voice["event_id"], shot["transcript"][0]["event_id"])
            self.assertEqual("Original VO", voice["effective_proposal"]["text"])
            self.assertEqual(
                "whole_event_not_word_aligned", shot["transcript"][0]["text_scope"]
            )
            self.assertEqual(0.5, link["event_fraction"])
        self.assertNotIn(
            "event-mixed-1",
            [link["event_id"] for link in result["shots"][0]["event_links"]],
        )
        self.assertEqual(before, source)

    def test_overlap_coverage_is_union_not_sum_and_scenes_keep_source_ids(self):
        source = _dataset()
        duplicate = copy.deepcopy(source["events"][0])
        duplicate.update(event_id="event-music-2", start_time=1.0, end_time=4.0)
        source["events"].append(duplicate)
        source["events"].sort(
            key=lambda event: (
                event["start_time"],
                event["end_time"],
                event["event_id"],
            )
        )
        scene = Scene(
            scene_id="scene-1",
            start_time=0,
            end_time=5,
            shot_ids=["s1", "s2"],
            scene_function="unverified interpretation",
        )
        result = associate_audio_events(source, shots(), [scene], media_duration=10)
        self.assertEqual(2, result["shots"][0]["event_coverage_seconds"]["music"])
        self.assertEqual(5, result["scenes"][0]["event_coverage_seconds"]["music"])
        self.assertEqual(["s1", "s2"], result["scenes"][0]["shot_ids"])
        self.assertEqual("interpretation", result["scenes"][0]["narrative_claim_type"])
        self.assertEqual(4, len(result["events"]))

    def test_long_cross_shot_transcript_is_stored_once_not_copied_per_link(self):
        import json

        source = _dataset()
        text = "中" * 4500
        source["events"][1]["proposal"]["text"] = text
        ranges = [
            Shot(
                shot_id=f"s{index}",
                start_time=index / 10,
                end_time=(index + 1) / 10,
                duration=0.1,
            )
            for index in range(50)
        ]
        result = associate_audio_events(source, ranges, media_duration=10)
        # Two audit representations (original/effective), never one per shot.
        self.assertEqual(2, json.dumps(result, ensure_ascii=False).count(text))
        apply_audio_associations(ranges, result)
        self.assertTrue(all(len(shot.dialogue) <= 220 for shot in ranges))
        self.assertTrue(
            any("full text in audio timeline" in shot.dialogue for shot in ranges)
        )

    def test_reviews_preserve_proposal_and_explicit_blank_without_legacy_fallback(self):
        source = _dataset()
        voice = source["events"][1]
        voice["review"] = {
            "status": "reviewed",
            "expected_proposal_sha256": proposal_sha256(voice["proposal"]),
            "overrides": {"text": ""},
            "review_notes": "explicit blank",
            "verification": "human_reviewed",
        }
        result = associate_audio_events(source, shots(), media_duration=10)
        event = next(event for event in result["events"] if event["kind"] == "voice")
        self.assertEqual("Original VO", event["proposal"]["text"])
        self.assertEqual("", event["effective_proposal"]["text"])
        self.assertEqual([], result["shots"][0]["transcript"])
        target = shots()
        target[0].dialogue = "old legacy ASR text"
        apply_audio_associations(target, result)
        self.assertEqual("", target[0].dialogue)

    def test_rejected_and_needs_work_are_traceable_but_not_effective(self):
        for status in ("rejected", "needs_work"):
            source = _dataset()
            voice = source["events"][1]
            voice["review"] = {
                "status": status,
                "expected_proposal_sha256": proposal_sha256(voice["proposal"]),
                "overrides": {},
                "review_notes": "review",
                "verification": "human_draft"
                if status == "needs_work"
                else "human_reviewed",
            }
            result = associate_audio_events(source, shots(), media_duration=10)
            self.assertIsNone(
                next(event for event in result["events"] if event["kind"] == "voice")[
                    "effective_proposal"
                ]
            )
            self.assertEqual([], result["shots"][0]["transcript"])
            self.assertEqual(0, result["shots"][0]["event_coverage_seconds"]["voice"])
            self.assertTrue(
                any(
                    link["event_id"] == voice["event_id"]
                    for link in result["shots"][0]["event_links"]
                )
            )

    def test_unknown_is_not_silence_and_protected_shot_annotations_survive(self):
        result = associate_audio_events(None, shots(), media_duration=10)
        self.assertFalse(result["available"])
        self.assertEqual("unknown", result["capabilities"]["asr"]["status"])
        target = shots()
        for source in ("human", "openai", "codex"):
            target[0].annotation_source = source
            target[0].dialogue = ""
            target[0].music_state = "confirmed silence"
            target[0].sound_design = "no soundtrack"
            active = associate_audio_events(_dataset(), target, media_duration=10)
            apply_audio_associations(target, active)
            self.assertEqual("", target[0].dialogue)
            self.assertEqual("confirmed silence", target[0].music_state)
            self.assertEqual("no soundtrack", target[0].sound_design)

    def test_provider_rhythm_note_is_part_of_audit_projection(self):
        target = shots()
        target[0].annotation_source = "human"
        target[0].rhythm_notes = "first review"
        first = associate_audio_events(_dataset(), target, media_duration=10)
        target[0].rhythm_notes = "changed review"
        second = associate_audio_events(_dataset(), target, media_duration=10)
        self.assertNotEqual(first["association_digest"], second["association_digest"])

    def test_html_is_bounded_preview_while_projection_keeps_all_links(self):
        from video_analysis_mvp.synthesis import _render_audio_associations

        source = _dataset()
        source["media_duration_seconds"] = 60
        source["events"] = [
            {
                "event_id": f"energy-{i:04d}",
                "start_time": i / 10,
                "end_time": (i + 1) / 10,
                "kind": "mixed",
                "source_id": "baseline-1",
                "proposal": _proposal(
                    label="synthetic energy", energy=0.2, verification="measured"
                ),
                "review": None,
            }
            for i in range(400)
        ]
        view = associate_audio_events(
            source,
            [Shot(shot_id="long", start_time=0, end_time=60, duration=60)],
            media_duration=60,
        )
        result = _render_audio_associations(view)
        self.assertLessEqual(result.count("<tr>"), 241)
        self.assertIn("400", result)
        self.assertIn("visualization_dataset.json", result)
        self.assertEqual(400, len(view["shots"][0]["event_links"]))

    def test_geometry_and_review_changes_change_digest_but_order_does_not(self):
        source = _dataset()
        first = associate_audio_events(source, shots(), media_duration=10)
        reordered = associate_audio_events(
            source, list(reversed(shots())), media_duration=10
        )
        self.assertEqual(first["association_digest"], reordered["association_digest"])
        changed = shots()
        changed[0].end_time = 1.9
        changed[0].duration = 1.9
        self.assertNotEqual(
            first["association_digest"],
            associate_audio_events(source, changed, media_duration=10)[
                "association_digest"
            ],
        )
        voice = source["events"][1]
        voice["review"] = {
            "status": "reviewed",
            "expected_proposal_sha256": proposal_sha256(voice["proposal"]),
            "overrides": {"text": "corrected"},
            "review_notes": "review",
            "verification": "human_reviewed",
        }
        self.assertNotEqual(
            first["association_digest"],
            associate_audio_events(source, shots(), media_duration=10)[
                "association_digest"
            ],
        )

    def test_invalid_geometry_and_duplicate_ids_are_rejected(self):
        invalid = shots()
        invalid[0].end_time = float("nan")
        with self.assertRaises(ValueError):
            associate_audio_events(_dataset(), invalid, media_duration=10)
        with self.assertRaises(ValueError):
            associate_audio_events(
                _dataset(), [shots()[0], shots()[0]], media_duration=10
            )
        with self.assertRaises(ValueError):
            associate_audio_events(_dataset(), shots(), media_duration=9)

    def test_bound_loader_rejects_stale_audio_and_unsafe_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = ProjectPaths(Path(temporary) / "audio-test")
            paths.ensure()
            media = media_for(paths, 1)
            write_pcm(paths.assets / "audio.wav", 1)
            analyze_audio(media, paths, skip_asr=True)
            target = [Shot(shot_id="s1", start_time=0, end_time=1, duration=1)]
            result = build_project_audio_associations(paths, media, target, [])
            self.assertTrue(result["available"])
            self.assertIsNotNone(result["source_binding"])
            write_pcm(paths.assets / "audio.wav", 1, lambda _t: 0.2)
            with self.assertRaisesRegex(ValueError, "audio"):
                build_project_audio_associations(paths, media, target, [])

    def test_millisecond_rounded_video_tail_is_compatible_without_snapping(self):
        for duration in (25 / 24, 26 / 24, 1.0004, 1.0006):
            end = round(duration, 3)
            shot = Shot(shot_id="tail", start_time=0, end_time=end, duration=end)
            result = associate_audio_events(None, [shot], media_duration=duration)
            self.assertEqual(end, result["shots"][0]["end_time"])
        with self.assertRaises(ValueError):
            associate_audio_events(
                None,
                [Shot(shot_id="bad", start_time=0, end_time=1.002, duration=1.002)],
                media_duration=1.0,
            )


class AudioReportIntegrationTest(unittest.TestCase):
    def setUp(self):
        from tests.test_report_generation import (
            _install_source_generation_receipts,
            _media,
        )

        self.temp = tempfile.TemporaryDirectory(prefix="vew-audio-report-")
        self.addCleanup(self.temp.cleanup)
        self.paths = ProjectPaths(Path(self.temp.name) / "audio-report")
        self.paths.ensure()
        _install_source_generation_receipts(self.paths)
        self.media = _media(self.paths)
        from video_analysis_mvp.synthesis import _normalize_shots
        from video_analysis_mvp.visual import _build_visual_generation_receipt

        current_shots = [
            Shot.model_validate(item)
            for item in load_json(self.paths.data / "shots.json")
        ]
        _normalize_shots(self.media, current_shots)
        dump_json(self.paths.data / "shots.json", current_shots)
        dump_json(
            self.paths.data / "visual_generation.json",
            _build_visual_generation_receipt(self.paths, current_shots, []),
        )
        write_pcm(self.paths.assets / "audio.wav", 2)

    def test_report_v4_binds_timeline_and_human_audio_edit_invalidates_report(self):
        from video_analysis_mvp.synthesis import (
            synthesize,
            verify_report_generation_manifest,
        )

        analyze_audio(self.media, self.paths, skip_asr=True)
        synthesize(self.paths)
        manifest = load_json(self.paths.manifest)
        self.assertEqual(4, manifest["report_generation"]["schema_version"])
        self.assertIsNotNone(
            manifest["report_generation"]["source_receipts"]["audio_intelligence"]
        )
        self.assertTrue(verify_report_generation_manifest(self.paths)[0])
        dataset = load_json(self.paths.data / "audio_intelligence.json")
        event = dataset["events"][0]
        event["review"] = {
            "status": "reviewed",
            "expected_proposal_sha256": proposal_sha256(event["proposal"]),
            "overrides": {"label": "reviewed note"},
            "review_notes": "test-only",
            "verification": "human_reviewed",
        }
        stage_and_commit_audio_intelligence(self.paths, dataset)
        self.assertFalse(verify_report_generation_manifest(self.paths)[0])
        before = (self.paths.data / "audio_intelligence.json").read_bytes()
        synthesize(self.paths)
        self.assertTrue(verify_report_generation_manifest(self.paths)[0])
        self.assertEqual(
            before, (self.paths.data / "audio_intelligence.json").read_bytes()
        )

    def test_chinese_summary_preserves_rhythm_label_and_shows_measured_acoustics(self):
        from video_analysis_mvp.delivery import _rhythm_text
        from video_analysis_mvp.synthesis import synthesize

        analyze_audio(self.media, self.paths, skip_asr=True)
        synthesize(self.paths)
        shot = load_json(self.paths.data / "shots.json")[0]
        self.assertEqual("sparse rhythm activity", shot["rhythm_notes"])
        self.assertIn("关联音频事件", shot["sound_rhythm"])
        self.assertNotIn("require", _rhythm_text(shot["sound_rhythm"], "zh"))
        self.assertIn("RMS", shot["sound_rhythm"])

    def test_legacy_delivery_language_aliases_use_the_existing_renderer_rule(self):
        from video_analysis_mvp.synthesis import synthesize

        for language in (None, "zh-CN", "ZH", "zh", "en", "en-US"):
            with self.subTest(language=language):
                if language is None:
                    self.media.metadata.pop("delivery_language", None)
                else:
                    self.media.metadata["delivery_language"] = language
                dump_json(self.paths.data / "media_package.json", self.media)
                analyze_audio(self.media, self.paths, skip_asr=True)
                synthesize(self.paths)
                shot = load_json(self.paths.data / "shots.json")[0]
                if language in ("en", "en-US"):
                    self.assertIn("linked audio events", shot["sound_rhythm"])
                else:
                    self.assertIn("关联音频事件", shot["sound_rhythm"])
                    self.assertNotIn(
                        "require 待复核",
                        (self.paths.reports / "storyboard.html").read_text(),
                    )

    def test_legacy_v3_without_timeline_is_readable_but_requires_refinalize_when_added(
        self,
    ):
        from video_analysis_mvp.audio_features import baseline_timeline, measure_audio
        from video_analysis_mvp.synthesis import (
            synthesize,
            verify_report_generation_manifest,
        )

        synthesize(self.paths)
        manifest = load_json(self.paths.manifest)
        manifest["report_generation"]["schema_version"] = 3
        manifest["report_generation"]["source_receipts"].pop("audio_intelligence", None)
        dump_json(self.paths.manifest, manifest)
        self.assertTrue(verify_report_generation_manifest(self.paths)[0])
        legacy_before = (self.paths.data / "audio_generation.json").read_bytes()
        stage_and_commit_audio_intelligence(
            self.paths,
            baseline_timeline(measure_audio(self.paths.assets / "audio.wav"), 2),
        )
        self.assertEqual(
            legacy_before, (self.paths.data / "audio_generation.json").read_bytes()
        )
        valid, reasons = verify_report_generation_manifest(self.paths)
        self.assertFalse(valid)
        self.assertTrue(any("audio" in reason.lower() for reason in reasons))

    def test_audio_change_during_report_publication_cannot_bind_new_input_to_old_view(
        self,
    ):
        from video_analysis_mvp.delivery import write_profile_delivery_package
        from video_analysis_mvp.synthesis import (
            synthesize,
            verify_report_generation_manifest,
        )

        analyze_audio(self.media, self.paths, skip_asr=True)

        def mutate_audio(*args, **kwargs):
            result = write_profile_delivery_package(*args, **kwargs)
            dataset = load_json(self.paths.data / "audio_intelligence.json")
            dataset["events"][0]["proposal"]["label"] = "changed after view generation"
            stage_and_commit_audio_intelligence(self.paths, dataset)
            return result

        with (
            patch(
                "video_analysis_mvp.synthesis.write_profile_delivery_package",
                side_effect=mutate_audio,
            ),
            self.assertRaisesRegex(ValueError, "audio intelligence changed"),
        ):
            synthesize(self.paths)
        self.assertEqual(
            "publishing", load_json(self.paths.manifest)["report_generation"]["state"]
        )
        self.assertFalse(verify_report_generation_manifest(self.paths)[0])
        synthesize(self.paths)
        self.assertTrue(verify_report_generation_manifest(self.paths)[0])

    def test_every_report_consumer_uses_effective_text_not_cleared_legacy_asr(self):
        from video_analysis_mvp.audio import _stage_and_commit_audio_generation
        from video_analysis_mvp.audio_features import baseline_timeline, measure_audio
        from video_analysis_mvp.delivery import render_profile_analysis
        from video_analysis_mvp.schemas import TranscriptSegment
        from video_analysis_mvp.synthesis import synthesize

        original = "ORIGINAL_VO_TO_EXCLUDE"
        _stage_and_commit_audio_generation(
            self.paths,
            [
                TranscriptSegment(
                    segment_id="raw-1", start_time=0, end_time=1, text=original
                )
            ],
            [],
            [],
        )
        data = baseline_timeline(measure_audio(self.paths.assets / "audio.wav"), 2)
        asr = copy.deepcopy(_dataset()["sources"][1])
        data["sources"].append(asr)
        data["capabilities"]["asr"] = {
            "status": "produced",
            "source_id": asr["source_id"],
            "reason": None,
        }
        proposal = _proposal(label="", text=original)
        data["events"].append(
            {
                "event_id": "voice-1",
                "start_time": 0,
                "end_time": 1,
                "kind": "voice",
                "source_id": asr["source_id"],
                "proposal": proposal,
                "review": {
                    "status": "reviewed",
                    "expected_proposal_sha256": proposal_sha256(proposal),
                    "overrides": {"text": ""},
                    "review_notes": "explicit blank",
                    "verification": "human_reviewed",
                },
            }
        )
        data["events"].sort(
            key=lambda event: (
                event["start_time"],
                event["end_time"],
                event["event_id"],
            )
        )
        stage_and_commit_audio_intelligence(self.paths, data)
        with patch(
            "video_analysis_mvp.delivery.render_profile_analysis",
            wraps=render_profile_analysis,
        ) as render:
            synthesize(self.paths)
        self.assertEqual([], render.call_args.args[4])
        for filename in ("report.html", "profile_analysis.html", "storyboard.html"):
            self.assertNotIn(original, (self.paths.reports / filename).read_text())
        self.assertIn(original, (self.paths.reports / "transcript.srt").read_text())

    def test_report_and_codex_dataset_share_bound_audio_links_without_prompt_injection(
        self,
    ):
        from video_analysis_mvp.synthesis import synthesize

        analyze_audio(self.media, self.paths, skip_asr=True)
        data = load_json(self.paths.data / "audio_intelligence.json")
        asr = copy.deepcopy(_dataset()["sources"][1])
        data["sources"].append(asr)
        data["capabilities"]["asr"] = {
            "status": "produced",
            "source_id": asr["source_id"],
            "reason": None,
        }
        data["events"].append(
            {
                "event_id": "vo-test",
                "start_time": 0.2,
                "end_time": 1.8,
                "kind": "voice",
                "source_id": asr["source_id"],
                "proposal": _proposal(
                    label="", text="INJECT_VO <script>alert(1)</script>"
                ),
                "review": None,
            }
        )
        data["events"].sort(
            key=lambda event: (
                event["start_time"],
                event["end_time"],
                event["event_id"],
            )
        )
        stage_and_commit_audio_intelligence(self.paths, data)
        synthesize(self.paths)
        visualization = load_json(self.paths.data / "visualization_dataset.json")
        self.assertEqual(
            "shot-audio-associations/v1",
            visualization["audio_associations"]["schema_id"],
        )
        self.assertTrue(visualization["audio_associations"]["source_binding"])
        reference = visualization["shots"][0]["audio"]["association_ref"]
        self.assertEqual("#/audio_associations/shots/0", reference)
        self.assertNotIn("event_links", visualization["shots"][0]["audio"])
        self.assertIn(
            "vo-test",
            [
                link["event_id"]
                for link in visualization["audio_associations"]["shots"][0][
                    "event_links"
                ]
            ],
        )
        report = (self.paths.reports / "report.html").read_text()
        self.assertIn('id="audio-evidence"', report)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", report)
        self.assertNotIn("INJECT_VO <script>", report)
        self.assertNotIn(
            "INJECT_VO", (self.paths.reports / "codex_handoff.md").read_text()
        )
        self.assertFalse(list(self.paths.root.rglob("*.pdf")))
        self.assertFalse(list(self.paths.root.rglob("*.xlsx")))
