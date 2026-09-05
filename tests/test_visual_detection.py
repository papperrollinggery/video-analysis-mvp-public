from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from video_analysis_mvp.paths import ProjectPaths
from video_analysis_mvp.schemas import AnalysisProfile, CanonicalMediaPackage, Shot, SourceType
from video_analysis_mvp.visual import _detect_shot_segments, _extract_shot_frames, analyze_visual


FFMPEG = shutil.which("ffmpeg")


class VisualDetectionTest(unittest.TestCase):
    def _media(self, video: Path, duration: float, *, frame_rate: float = 25.0) -> CanonicalMediaPackage:
        return CanonicalMediaPackage(
            project_id="visual-detection-test",
            source_type=SourceType.file,
            source=str(video),
            local_master_path=str(video),
            review_copy_path=str(video),
            audio_path="",
            duration_seconds=duration,
            frame_rate=frame_rate,
            resolution="64x64",
            aspect_ratio=1.0,
            status="created",
            analysis_profile=AnalysisProfile.research,
        )

    def _ffmpeg_executable(self) -> str:
        candidates = (
            FFMPEG,
            shutil.which("ffmpeg"),
            str(Path(sys.executable).with_name("ffmpeg")),
            "/opt/homebrew/bin/ffmpeg",
            "/usr/local/bin/ffmpeg",
            "/usr/bin/ffmpeg",
        )
        for executable in candidates:
            if executable and Path(executable).is_file() and os.access(executable, os.X_OK):
                return executable
        self.fail("ffmpeg must be installed for visual detection regression tests")

    def _ffmpeg(self, *args: str) -> None:
        executable = self._ffmpeg_executable()
        subprocess.run(
            [executable, "-hide_banner", "-loglevel", "error", "-y", *args],
            check=True,
            capture_output=True,
            text=True,
        )

    def _ffmpeg_on_path(self) -> object:
        executable = self._ffmpeg_executable()
        path = f"{Path(executable).parent}{os.pathsep}{os.environ.get('PATH', '')}"
        return patch.dict(os.environ, {"PATH": path})

    def test_single_evidenced_cut_is_not_replaced_by_fixed_cadence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "cut-at-four-seconds.mp4"
            self._ffmpeg(
                "-f",
                "lavfi",
                "-i",
                "color=c=red:s=64x64:r=25:d=4",
                "-f",
                "lavfi",
                "-i",
                "color=c=blue:s=64x64:r=25:d=6",
                "-filter_complex",
                "[0:v][1:v]concat=n=2:v=1:a=0",
                "-c:v",
                "mpeg4",
                str(video),
            )

            with self._ffmpeg_on_path():
                segments, method = _detect_shot_segments(video, self._media(video, 10.0))

        self.assertEqual("scene_detection", method)
        self.assertEqual(2, len(segments))
        self.assertEqual((0.0, 4.0, "high"), segments[0])
        self.assertEqual((4.0, 10.0, "high"), segments[1])

    def test_extremely_short_shot_samples_stay_ordered_inside_half_open_interval(self) -> None:
        shot = Shot(
            shot_id="shot_0001",
            start_time=0.0,
            end_time=0.04,
            duration=0.04,
            frame_refs=["shot_0001_start.jpg", "shot_0001_mid.jpg", "shot_0001_end.jpg"],
            primary_frame_ref="shot_0001_mid.jpg",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "forty-milliseconds.mp4"
            frames = root / "frames"
            self._ffmpeg(
                "-f",
                "lavfi",
                "-i",
                "testsrc2=s=64x64:r=100:d=0.04",
                "-c:v",
                "mpeg4",
                str(video),
            )

            with patch("video_analysis_mvp.visual._extract_frame_at") as extract:
                _extract_shot_frames(video, frames, [shot])

            sample_times = [call.args[2] for call in extract.call_args_list]
            self.assertEqual([0.01, 0.02, 0.03], sample_times)
            self.assertTrue(all(shot.start_time <= value < shot.end_time for value in sample_times))
            self.assertEqual(sample_times, sorted(sample_times))

            with self._ffmpeg_on_path():
                _extract_shot_frames(video, frames, [shot])
            for filename in shot.frame_refs:
                frame = frames / filename
                self.assertGreater(frame.stat().st_size, 0)
                with Image.open(frame) as image:
                    self.assertEqual("JPEG", image.format)

    def test_extremely_short_clip_completes_the_visual_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "forty-milliseconds.mp4"
            self._ffmpeg(
                "-f",
                "lavfi",
                "-i",
                "testsrc2=s=64x64:r=100:d=0.04",
                "-c:v",
                "mpeg4",
                str(video),
            )
            paths = ProjectPaths(root / "project")
            paths.ensure()

            with self._ffmpeg_on_path():
                shots, scenes = analyze_visual(self._media(video, 0.04, frame_rate=100.0), paths)

            self.assertEqual(1, len(shots))
            self.assertEqual(1, len(scenes))
            self.assertEqual((0.0, 0.04), (shots[0].start_time, shots[0].end_time))
            self.assertEqual(0.04, shots[0].duration)
            for filename in shots[0].frame_refs:
                frame = paths.keyframes / filename
                self.assertGreater(frame.stat().st_size, 0)
                with Image.open(frame) as image:
                    self.assertEqual("JPEG", image.format)
            self.assertGreater((paths.assets / "contact_sheet.jpg").stat().st_size, 0)

    def test_single_and_double_frame_25fps_clips_complete_the_visual_pipeline(self) -> None:
        for frame_count in (1, 2):
            with self.subTest(frame_count=frame_count), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                duration = frame_count / 25
                video = root / f"{frame_count}-frame.mp4"
                self._ffmpeg(
                    "-f",
                    "lavfi",
                    "-i",
                    f"testsrc2=s=64x64:r=25:d={duration}",
                    "-c:v",
                    "mpeg4",
                    str(video),
                )
                paths = ProjectPaths(root / "project")
                paths.ensure()

                with self._ffmpeg_on_path():
                    shots, _scenes = analyze_visual(self._media(video, duration, frame_rate=25.0), paths)

                self.assertEqual(1, len(shots))
                self.assertEqual((0.0, duration), (shots[0].start_time, shots[0].end_time))
                for filename in shots[0].frame_refs:
                    frame = paths.keyframes / filename
                    self.assertGreater(frame.stat().st_size, 0)
                    with Image.open(frame) as image:
                        self.assertEqual("JPEG", image.format)
