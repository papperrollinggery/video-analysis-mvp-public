from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from video_analysis_mvp.audio import _stage_and_commit_audio_generation, analyze_audio, verify_audio_analysis
from video_analysis_mvp.media import ingest_source, verify_media_generation
from video_analysis_mvp.paths import ProjectPaths
from video_analysis_mvp.schemas import AnalysisProfile, BeatEvent, CanonicalMediaPackage, SourceType, dump_json, load_json


HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg and ffprobe are required for media-stream fixtures")
class VideoWithoutAudioTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="video-without-audio-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def _video(self, name: str, *, with_audio: bool) -> Path:
        path = self.root / name
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=black:s=64x64:r=8:d=1",
        ]
        if with_audio:
            command.extend(["-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-shortest"])
        command.extend(["-c:v", "libx264", "-pix_fmt", "yuv420p"])
        if with_audio:
            command.extend(["-c:a", "aac"])
        command.append(str(path))
        subprocess.run(command, check=True, capture_output=True, text=True)
        return path

    def test_real_video_only_ingest_and_audio_stage_keep_no_audio_as_no_audio(self) -> None:
        paths = ProjectPaths(self.root / "video-only-project")
        paths.ensure()
        media = ingest_source(str(self._video("video-only.mp4", with_audio=False)), paths, AnalysisProfile.research)

        self.assertEqual("", media.audio_path)
        self.assertFalse((paths.assets / "audio.wav").exists())
        self.assertNotIn("audio_wav", load_json(paths.manifest)["artifacts"])
        self.assertEqual((True, []), verify_media_generation(paths))

        transcript, beats, music = analyze_audio(media, paths, skip_asr=True)

        self.assertEqual(([], [], []), (transcript, beats, music))
        self.assertEqual([], load_json(paths.data / "transcript.json"))
        self.assertEqual([], load_json(paths.data / "beats.json"))
        self.assertEqual([], load_json(paths.data / "music_profile.json"))
        self.assertFalse((paths.data / "audio_intelligence.json").exists())
        self.assertFalse((paths.data / "audio_intelligence_generation.json").exists())
        self.assertEqual((True, []), verify_audio_analysis(paths))

    def test_audio_present_media_with_a_missing_wav_is_rejected(self) -> None:
        paths = ProjectPaths(self.root / "missing-wav-project")
        paths.ensure()
        media = CanonicalMediaPackage(
            project_id=paths.root.name,
            source_type=SourceType.file,
            source="audio-present.mp4",
            local_master_path=str(paths.ingest / "master.mp4"),
            review_copy_path=str(paths.assets / "review.mp4"),
            audio_path=str(paths.assets / "audio.wav"),
            duration_seconds=1.0,
            frame_rate=8.0,
            resolution="64x64",
            aspect_ratio=1.0,
            status="created",
            analysis_profile=AnalysisProfile.research,
        )
        dump_json(paths.data / "media_package.json", media)
        _stage_and_commit_audio_generation(paths, [], [], [])

        valid, reasons = verify_audio_analysis(paths)

        self.assertFalse(valid)
        self.assertTrue(any("audio WAV" in reason for reason in reasons))

    def test_tampering_no_audio_declaration_cannot_bypass_stream_verification(self) -> None:
        paths = ProjectPaths(self.root / "tampered-declaration-project")
        paths.ensure()
        media = ingest_source(str(self._video("with-audio.mp4", with_audio=True)), paths, AnalysisProfile.research)
        media.audio_path = ""
        dump_json(paths.data / "media_package.json", media)

        valid, reasons = verify_media_generation(paths)

        self.assertFalse(valid)
        self.assertTrue(any("audio stream" in reason for reason in reasons))

    def test_no_audio_argument_cannot_erase_analysis_for_an_audio_present_project(self) -> None:
        paths = ProjectPaths(self.root / "stale-no-audio-argument")
        paths.ensure()
        media = ingest_source(str(self._video("with-audio.mp4", with_audio=True)), paths, AnalysisProfile.research)
        media.audio_path = ""
        with self.assertRaisesRegex(ValueError, "does not match"):
            analyze_audio(media, paths, skip_asr=True)
        self.assertFalse((paths.data / "audio_generation.json").exists())

    def test_verified_no_audio_rejects_invented_beat_records(self) -> None:
        paths = ProjectPaths(self.root / "invented-beat-project")
        paths.ensure()
        media = ingest_source(str(self._video("no-audio.mp4", with_audio=False)), paths, AnalysisProfile.research)
        analyze_audio(media, paths, skip_asr=True)
        _stage_and_commit_audio_generation(paths, [], [BeatEvent(time=0.1, strength=0.5)], [])
        valid, reasons = verify_audio_analysis(paths)
        self.assertFalse(valid)
        self.assertTrue(any("non-empty audio records" in reason for reason in reasons))


if __name__ == "__main__":
    unittest.main()
