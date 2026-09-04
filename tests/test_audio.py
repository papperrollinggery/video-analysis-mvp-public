from __future__ import annotations

import hashlib
import math
import os
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from video_analysis_mvp.audio import (
    _stage_and_commit_audio_generation,
    analyze_audio,
    verify_audio_analysis,
    verify_audio_generation,
)
from video_analysis_mvp.audio_features import (
    baseline_timeline,
    measure_audio,
    snapshot_audio,
)
from video_analysis_mvp.audio_intelligence import (
    audio_intelligence_binding,
    proposal_sha256,
    stage_and_commit_audio_intelligence,
    validate_audio_timeline,
)
from video_analysis_mvp.paths import ProjectPaths
from video_analysis_mvp.schemas import (
    CanonicalMediaPackage,
    SourceType,
    dump_json,
    load_json,
)


def write_pcm(
    path: Path, duration: float, sample=None, *, width=2, channels=1, rate=8000
):
    sample = sample or (lambda _t: 0.0)
    data = bytearray()
    for index in range(round(duration * rate)):
        values = sample(index / rate)
        if not isinstance(values, tuple):
            values = (values,) * channels
        for value in values:
            maximum = 2 ** (width * 8 - 1)
            integer = max(-maximum, min(maximum - 1, round(value * maximum)))
            data.extend(
                (integer + 128).to_bytes(1, "little")
                if width == 1
                else integer.to_bytes(width, "little", signed=True)
            )
    with wave.open(str(path), "wb") as output:
        output.setparams((channels, width, rate, 0, "NONE", "not compressed"))
        output.writeframes(data)


def media_for(paths, duration):
    media = CanonicalMediaPackage(
        project_id="audio-test",
        source_type=SourceType.file,
        source="synthetic.mp4",
        local_master_path=str(paths.ingest / "master.mp4"),
        review_copy_path=str(paths.assets / "review.mp4"),
        audio_path=str(paths.assets / "audio.wav"),
        duration_seconds=duration,
        frame_rate=25,
        resolution="320x180",
        aspect_ratio=16 / 9,
        status="created",
        analysis_profile="research",
    )
    dump_json(paths.data / "media_package.json", media)
    return media


class AudioFeaturesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="vew-audio-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "audio.wav"

    def test_silence_has_measured_range_but_no_voice_music_or_bpm(self):
        write_pcm(self.path, 1.13)
        features = measure_audio(self.path)
        self.assertEqual(0.0, features.rms)
        self.assertEqual(((0.0, 1.13),), features.silence_ranges)
        self.assertFalse(features.onsets)
        self.assertIsNone(features.estimated_bpm)
        timeline = validate_audio_timeline(baseline_timeline(features, 1.13))
        self.assertEqual(
            "produced", timeline["capabilities"]["baseline_features"]["status"]
        )
        self.assertEqual("unknown", timeline["capabilities"]["asr"]["status"])
        self.assertEqual(
            {"silence", "mixed"}, {event["kind"] for event in timeline["events"]}
        )
        self.assertTrue(all(event["end_time"] <= 1.13 for event in timeline["events"]))

    def test_rms_is_not_mean_absolute_amplitude_and_no_sine_beat_claim(self):
        write_pcm(self.path, 1, lambda t: 0.5 * math.sin(2 * math.pi * 400 * t))
        result = measure_audio(self.path)
        self.assertAlmostEqual(0.5 / math.sqrt(2), result.rms, delta=0.0001)
        self.assertIsNone(result.estimated_bpm)
        self.assertFalse(result.silence_ranges)

    def test_pcm_widths_and_out_of_phase_stereo_keep_channel_energy(self):
        for width in (1, 2, 3, 4):
            with self.subTest(width=width):
                write_pcm(
                    self.path, 0.1, lambda _t: (0.5, -0.5), width=width, channels=2
                )
                result = measure_audio(self.path)
                self.assertAlmostEqual(0.5, result.rms, places=5)
                self.assertEqual(2, result.channels)

    def test_regular_pulses_estimate_tempo_with_explicit_uncertainty(self):
        times = [0.3 + 0.5 * index for index in range(9)]
        write_pcm(
            self.path,
            5,
            lambda t: 0.8 if any(0 <= t - start < 0.04 for start in times) else 0,
        )
        result = measure_audio(self.path)
        self.assertEqual(len(times), len(result.onsets))
        for expected, onset in zip(times, result.onsets):
            self.assertAlmostEqual(expected, onset.time, delta=0.021)
        self.assertAlmostEqual(120, result.estimated_bpm, delta=1)
        timeline = baseline_timeline(result, 5)
        pulse = [
            event
            for event in timeline["events"]
            if event["proposal"]["estimated_bpm"] is not None
        ]
        self.assertTrue(pulse)
        self.assertTrue(
            all(
                event["proposal"]["verification"] == "machine_estimated"
                for event in pulse
            )
        )
        self.assertFalse(
            any(
                event["kind"] in {"voice", "music", "sfx"}
                for event in timeline["events"]
            )
        )

    def test_irregular_transients_do_not_produce_confident_tempo(self):
        times = [0.1, 0.5, 1.3, 1.56, 2.4, 2.8]
        write_pcm(
            self.path,
            3,
            lambda t: 0.7 if any(0 <= t - start < 0.03 for start in times) else 0,
        )
        self.assertIsNone(measure_audio(self.path).estimated_bpm)

    def test_half_open_windows_cover_partial_final_window(self):
        write_pcm(self.path, 0.113, lambda _t: 0.4)
        result = measure_audio(self.path)
        self.assertEqual(0, result.windows[0].start)
        self.assertEqual(0.113, result.windows[-1].end)
        self.assertTrue(
            all(
                left.end == right.start
                for left, right in zip(result.windows, result.windows[1:])
            )
        )

    def test_duration_mismatch_does_not_invent_missing_tail(self):
        write_pcm(self.path, 1)
        features = measure_audio(self.path)
        with self.assertRaisesRegex(ValueError, "duration"):
            baseline_timeline(features, 3)
        # Codec padding can be clipped, not described as unheard silence.
        timeline = baseline_timeline(features, 0.97)
        self.assertTrue(all(event["end_time"] <= 0.97 for event in timeline["events"]))

    def test_corrupt_empty_and_truncated_pcm_fail_closed(self):
        for content in (b"not-wave", b"RIFF\x00\x00\x00\x00WAVE"):
            self.path.write_bytes(content)
            with self.assertRaises(ValueError):
                measure_audio(self.path)
        write_pcm(self.path, 0)
        with self.assertRaises(ValueError):
            measure_audio(self.path)
        write_pcm(self.path, 1)
        self.path.write_bytes(self.path.read_bytes()[:-10])
        with self.assertRaisesRegex(ValueError, "truncated"):
            measure_audio(self.path)

    def test_limits_and_unsafe_input_are_rejected(self):
        write_pcm(self.path, 1)
        with self.assertRaisesRegex(ValueError, "limit"):
            measure_audio(self.path, max_duration_seconds=0.5)
        alias = self.root / "alias.wav"
        alias.symlink_to(self.path)
        with self.assertRaises((ValueError, OSError)):
            measure_audio(alias)
        if hasattr(os, "mkfifo"):
            fifo = self.root / "fifo.wav"
            os.mkfifo(fifo)
            with self.assertRaises((ValueError, OSError)):
                measure_audio(fifo)

    def test_private_snapshot_is_stable_and_removed_after_use(self):
        write_pcm(self.path, 0.2)
        original = self.path.read_bytes()
        with snapshot_audio(self.path) as snapshot:
            self.assertEqual(original, snapshot.read_bytes())
            self.path.write_bytes(b"changed")
            self.assertEqual(
                hashlib.sha256(original).hexdigest(),
                measure_audio(snapshot).input_sha256,
            )
        self.assertFalse(snapshot.exists())


class AudioPipelineTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="vew-audio-pipeline-")
        self.addCleanup(self.temp.cleanup)
        self.paths = ProjectPaths(Path(self.temp.name) / "audio-test")
        self.paths.ensure()
        self.media = media_for(self.paths, 1)
        write_pcm(self.paths.assets / "audio.wav", 1)

    def test_default_pipeline_is_offline_produces_bound_timeline_and_no_exports(self):
        with patch(
            "subprocess.Popen", side_effect=AssertionError("no implicit model process")
        ):
            transcript, beats, music = analyze_audio(self.media, self.paths)
        self.assertEqual([], transcript)
        self.assertEqual([], beats)
        self.assertEqual([], music[0].style_tags)
        self.assertEqual([], music[0].mood_tags)
        self.assertEqual("unknown", music[0].tempo_bucket)
        self.assertEqual((True, []), verify_audio_generation(self.paths))
        binding = audio_intelligence_binding(self.paths)
        self.assertEqual("unknown", binding["capabilities"]["asr"]["status"])
        self.assertFalse(list(self.paths.root.rglob("*.pdf")))
        self.assertFalse(list(self.paths.root.rglob("*.xlsx")))

    def test_skip_asr_is_distinct_from_missing_capability(self):
        analyze_audio(self.media, self.paths, skip_asr=True)
        timeline = load_json(self.paths.data / "audio_intelligence.json")
        self.assertEqual("skipped", timeline["capabilities"]["asr"]["status"])

    def test_invalid_source_preserves_existing_generation(self):
        analyze_audio(self.media, self.paths, skip_asr=True)
        before = (self.paths.data / "audio_generation.json").read_bytes()
        (self.paths.assets / "audio.wav").write_bytes(b"corrupt")
        with self.assertRaises(ValueError):
            analyze_audio(self.media, self.paths, skip_asr=True)
        self.assertEqual(
            before, (self.paths.data / "audio_generation.json").read_bytes()
        )

    def test_recovery_requires_timeline_and_current_input_not_only_legacy_outputs(self):
        _stage_and_commit_audio_generation(self.paths, [], [], [])
        self.assertTrue(verify_audio_generation(self.paths)[0])
        self.assertFalse(verify_audio_analysis(self.paths)[0])
        analyze_audio(self.media, self.paths, skip_asr=True)
        self.assertTrue(verify_audio_analysis(self.paths)[0])
        write_pcm(self.paths.assets / "audio.wav", 1, lambda _t: 0.1)
        self.assertTrue(verify_audio_generation(self.paths)[0])
        self.assertFalse(verify_audio_analysis(self.paths)[0])

    def test_rerun_does_not_discard_human_audio_decisions(self):
        analyze_audio(self.media, self.paths, skip_asr=True)
        dataset = load_json(self.paths.data / "audio_intelligence.json")
        event = dataset["events"][0]
        event["review"] = {
            "status": "reviewed",
            "expected_proposal_sha256": proposal_sha256(event["proposal"]),
            "overrides": {},
            "review_notes": "Test-only review",
            "verification": "human_reviewed",
        }
        stage_and_commit_audio_intelligence(self.paths, dataset)
        before = (self.paths.data / "audio_intelligence.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "human"):
            analyze_audio(self.media, self.paths, skip_asr=True)
        self.assertEqual(
            before, (self.paths.data / "audio_intelligence.json").read_bytes()
        )

    def test_timeline_guard_rejects_input_change_after_legacy_commit(self):
        analyze_audio(self.media, self.paths, skip_asr=True)
        before = (self.paths.data / "audio_intelligence.json").read_bytes()
        real_commit = _stage_and_commit_audio_generation

        def mutate_after_commit(*args):
            real_commit(*args)
            write_pcm(self.paths.assets / "audio.wav", 1, lambda _t: 0.2)

        with (
            patch(
                "video_analysis_mvp.audio._stage_and_commit_audio_generation",
                side_effect=mutate_after_commit,
            ),
            self.assertRaisesRegex(ValueError, "changed"),
        ):
            analyze_audio(self.media, self.paths, skip_asr=True)
        self.assertEqual(
            before, (self.paths.data / "audio_intelligence.json").read_bytes()
        )
        self.assertFalse(verify_audio_analysis(self.paths)[0])

    def test_asr_produced_text_is_estimated_not_a_vo_identity_claim(self):
        from video_analysis_mvp.audio_transcription import TranscriptionResult
        from video_analysis_mvp.schemas import TranscriptSegment

        segment = TranscriptSegment(
            segment_id="tr_0001",
            start_time=0.1,
            end_time=0.7,
            text="Test speech",
            language="en",
            confidence=0.55,
        )
        result = TranscriptionResult("produced", None, [segment], "b" * 64)
        with patch("video_analysis_mvp.audio.transcribe_local", return_value=result):
            analyze_audio(self.media, self.paths, asr_model="synthetic-checkpoint")
        timeline = load_json(self.paths.data / "audio_intelligence.json")
        voice = next(event for event in timeline["events"] if event["kind"] == "voice")
        self.assertEqual("unknown", voice["proposal"]["voice_role"])
        self.assertEqual("machine_estimated", voice["proposal"]["verification"])
        self.assertIsNone(voice["proposal"]["speaker_id"])
        self.assertTrue(verify_audio_analysis(self.paths)[0])

    def test_legacy_report_never_calls_silence_music_led_or_double_counts_boundary(
        self,
    ):
        from video_analysis_mvp.schemas import BeatEvent, Shot
        from video_analysis_mvp.synthesis import _attach_audio_to_shots, build_report

        transcript, beats, music = analyze_audio(self.media, self.paths, skip_asr=True)
        shot = Shot(shot_id="shot-1", start_time=0, end_time=1, duration=1)
        boundary = BeatEvent(time=1, strength=0.5, source="pcm_energy_onset_candidate")
        _attach_audio_to_shots([shot], transcript, [boundary], music)
        self.assertEqual("unknown", shot.music_state)
        self.assertNotEqual("music-led", shot.sound_design)
        self.assertEqual(0, shot.beat_density)
        report = build_report(
            self.media, [shot], [], transcript, beats, music, self.paths
        )
        self.assertNotIn("music reads as low", " ".join(report.audio_observations))
        shot.annotation_source = "human"
        shot.music_state = "confirmed silence"
        shot.sound_design = "intentional pause"
        shot.sound_rhythm = "no beat"
        _attach_audio_to_shots([shot], transcript, [boundary], music)
        self.assertEqual("confirmed silence", shot.music_state)
        self.assertEqual("intentional pause", shot.sound_design)
        self.assertEqual("no beat", shot.sound_rhythm)

    def test_full_pipeline_propagates_requested_asr_failure(self):
        from types import SimpleNamespace

        from video_analysis_mvp.audio_transcription import TranscriptionResult
        from video_analysis_mvp.pipeline import run_full_pipeline

        with (
            patch(
                "video_analysis_mvp.pipeline.new_project_paths", return_value=self.paths
            ),
            patch("video_analysis_mvp.pipeline.ingest_source", return_value=self.media),
            patch("video_analysis_mvp.pipeline.set_delivery_language"),
            patch("video_analysis_mvp.pipeline.analyze_visual"),
            patch(
                "video_analysis_mvp.pipeline.synthesize",
                return_value=SimpleNamespace(artifacts={}),
            ),
            patch(
                "video_analysis_mvp.audio.transcribe_local",
                return_value=TranscriptionResult(
                    "failed", "local whisper execution failed", []
                ),
            ),
        ):
            result = run_full_pipeline("synthetic.mp4", asr_model="/explicit/model.pt")
        self.assertEqual("warning", result.status)
        self.assertIn("ASR: failed", result.summary)

    def test_oversized_asr_integer_fails_adapter_but_keeps_baseline(self):
        model = self.paths.root / "test-model.pt"
        model.write_bytes(b"synthetic checkpoint")
        for digits in (400, 5000):

            def invalid_output(args, digits=digits, **_kwargs):
                output = Path(args[args.index("--output_dir") + 1]) / "audio.json"
                output.write_text(
                    '{"segments":[{"start":' + "9" * digits + ',"end":1,"text":"bad"}]}'
                )

            with (
                self.subTest(digits=digits),
                patch(
                    "video_analysis_mvp.audio_transcription.shutil.which",
                    return_value="/test-only/whisper",
                ),
                patch(
                    "video_analysis_mvp.audio_transcription.run_command",
                    side_effect=invalid_output,
                ),
            ):
                transcript, _, _ = analyze_audio(
                    self.media, self.paths, asr_model=str(model)
                )
                self.assertEqual([], transcript)
                capabilities = audio_intelligence_binding(self.paths)["capabilities"]
                self.assertEqual(
                    "produced", capabilities["baseline_features"]["status"]
                )
                self.assertEqual("failed", capabilities["asr"]["status"])

    def test_invalid_asr_utf8_text_does_not_break_baseline_publication(self):
        import json

        model = self.paths.root / "test-model.pt"
        model.write_bytes(b"synthetic checkpoint")
        for text in ("中" * 6000, "bad\x00text", "bad\ud800text"):

            def invalid_output(args, text=text, **_kwargs):
                output = Path(args[args.index("--output_dir") + 1]) / "audio.json"
                output.write_text(
                    json.dumps({"segments": [{"start": 0, "end": 1, "text": text}]})
                )

            with (
                self.subTest(case=repr(text[:8])),
                patch(
                    "video_analysis_mvp.audio_transcription.shutil.which",
                    return_value="/test-only/whisper",
                ),
                patch(
                    "video_analysis_mvp.audio_transcription.run_command",
                    side_effect=invalid_output,
                ),
            ):
                transcript, _, _ = analyze_audio(
                    self.media, self.paths, asr_model=str(model)
                )
                self.assertEqual([], transcript)
                capabilities = audio_intelligence_binding(self.paths)["capabilities"]
                self.assertEqual(
                    "produced", capabilities["baseline_features"]["status"]
                )
                self.assertEqual("failed", capabilities["asr"]["status"])

    def test_source_change_during_measurement_never_binds_old_analysis_to_new_wav(self):
        from video_analysis_mvp.audio_transcription import TranscriptionResult

        def mutate(*_args, **_kwargs):
            write_pcm(self.paths.assets / "audio.wav", 1, lambda _t: 0.5)
            return TranscriptionResult("skipped", "not requested", [])

        with (
            patch("video_analysis_mvp.audio.transcribe_local", side_effect=mutate),
            self.assertRaisesRegex(ValueError, "changed"),
        ):
            analyze_audio(self.media, self.paths, skip_asr=True)
        self.assertFalse(
            (self.paths.data / "audio_intelligence_generation.json").exists()
        )
