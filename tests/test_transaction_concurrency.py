from __future__ import annotations

import base64
import hashlib
import json
import multiprocessing
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from video_analysis_mvp.audio import (
    _stage_and_commit_audio_generation,
    analyze_audio,
    verify_audio_generation,
)
from video_analysis_mvp.config import load_runtime_config, save_runtime_config
from video_analysis_mvp.media import ingest_source
from video_analysis_mvp.paths import ProjectPaths, new_project_paths
from video_analysis_mvp.pipeline import run_ingest_only
from video_analysis_mvp.schemas import (
    AnalysisProfile,
    CanonicalMediaPackage,
    Shot,
    SourceType,
    dump_json,
    load_json,
)
from video_analysis_mvp.vision import OBSERVATION_FIELDS, annotate_project_with_vision
from video_analysis_mvp.visual import analyze_visual, verify_visual_generation


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _provider_payload(summary: str) -> dict[str, object]:
    result: dict[str, object] = {field: "none" for field in OBSERVATION_FIELDS}
    result["content_summary"] = summary
    result["confidence"] = 0.8
    return result


def _shot(number: int = 1) -> Shot:
    name = f"shot_{number:04d}.png"
    return Shot(
        shot_id=f"shot_{number:04d}",
        shot_no=number,
        start_time=float(number - 1),
        end_time=float(number),
        duration=1.0,
        frame_ref=name,
        primary_frame_ref=name,
        frame_refs=[name],
        content_summary="original",
        annotation_source="machine",
        readiness_status="blocked",
    )


def _media(paths: ProjectPaths, profile: AnalysisProfile = AnalysisProfile.research) -> CanonicalMediaPackage:
    return CanonicalMediaPackage(
        project_id=paths.root.name,
        source_type=SourceType.file,
        source="fixture.mp4",
        local_master_path=str(paths.ingest / "master.mp4"),
        review_copy_path=str(paths.assets / "review.mp4"),
        audio_path=str(paths.assets / "audio.wav"),
        duration_seconds=2.0,
        frame_rate=24.0,
        resolution="320x180",
        aspect_ratio=16 / 9,
        status="created",
        analysis_profile=profile,
    )


def _save_config_process(
    workspace: str,
    updates: dict[str, str],
    start: multiprocessing.synchronize.Event,
    results: multiprocessing.queues.Queue,
) -> None:
    try:
        start.wait(10)
        save_runtime_config(Path(workspace), updates)
        results.put(None)
    except Exception as exc:  # pragma: no cover - asserted in parent process
        results.put(f"{type(exc).__name__}: {exc}")


class ExclusiveProjectCreationTest(unittest.TestCase):
    def test_duplicate_pipeline_id_preserves_every_existing_ingest_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            paths = ProjectPaths(workspace / "existing-project")
            paths.ensure()
            artifacts = {
                paths.ingest / "master.mp4": b"master-before",
                paths.assets / "review.mp4": b"review-before",
                paths.assets / "audio.wav": b"audio-before",
                paths.data / "media_package.json": b"media-before",
                paths.manifest: b"manifest-before",
            }
            for path, payload in artifacts.items():
                path.write_bytes(payload)

            with (
                patch("video_analysis_mvp.pipeline.ingest_source") as ingest,
                self.assertRaisesRegex(FileExistsError, "already exists"),
            ):
                run_ingest_only(
                    "missing.mp4",
                    workspace=str(workspace),
                    project_id="existing-project",
                )

            ingest.assert_not_called()
            self.assertEqual({path: path.read_bytes() for path in artifacts}, artifacts)

    def test_concurrent_same_id_has_exactly_one_creator(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            barrier = threading.Barrier(2)
            created: list[ProjectPaths] = []
            failures: list[Exception] = []

            def create() -> None:
                barrier.wait()
                try:
                    created.append(new_project_paths("same-id", workspace))
                except Exception as exc:
                    failures.append(exc)

            threads = [threading.Thread(target=create) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)

            self.assertEqual(1, len(created))
            self.assertEqual(1, len(failures))
            self.assertIsInstance(failures[0], FileExistsError)
            self.assertTrue((workspace / "same-id" / "data").is_dir())

    def test_direct_failed_reingest_refuses_old_artifacts_without_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory) / "workspace" / "existing")
            paths.ensure()
            artifacts = {
                paths.ingest / "master.mp4": b"master-before",
                paths.assets / "review.mp4": b"review-before",
                paths.assets / "audio.wav": b"audio-before",
                paths.data / "media_package.json": b"media-before",
                paths.manifest: b"manifest-before",
            }
            for path, payload in artifacts.items():
                path.write_bytes(payload)

            with self.assertRaisesRegex(FileExistsError, "already contains"):
                ingest_source("unused.mp4", paths, AnalysisProfile.research)

            self.assertEqual({path: path.read_bytes() for path in artifacts}, artifacts)


class VisionCompareAndSwapTest(unittest.TestCase):
    def project(self, directory: str) -> ProjectPaths:
        paths = ProjectPaths(Path(directory) / "workspace" / "project")
        paths.ensure()
        shot = _shot()
        dump_json(paths.data / "shots.json", [shot])
        (paths.keyframes / shot.frame_ref).write_bytes(PNG_1X1)
        return paths

    def run_with_provider(self, paths: ProjectPaths, analyzer: object):
        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}, clear=True),
            patch("video_analysis_mvp.vision.analyze_frame", analyzer),
        ):
            return annotate_project_with_vision(paths, provider="openai")

    def test_human_edit_during_successful_provider_call_wins_compare_and_swap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = self.project(directory)
            entered = threading.Event()
            release = threading.Event()
            results: list[object] = []

            def analyze(*_args: object, **_kwargs: object) -> dict[str, object]:
                entered.set()
                self.assertTrue(release.wait(5))
                return _provider_payload("provider")

            thread = threading.Thread(target=lambda: results.append(self.run_with_provider(paths, analyze)))
            thread.start()
            self.assertTrue(entered.wait(5))
            current = [Shot.model_validate(item) for item in load_json(paths.data / "shots.json")]
            current[0].content_summary = "human edit"
            current[0].annotation_source = "human"
            dump_json(paths.data / "shots.json", current)
            expected = (paths.data / "shots.json").read_bytes()
            release.set()
            thread.join(5)

            self.assertFalse(thread.is_alive())
            self.assertEqual(expected, (paths.data / "shots.json").read_bytes())
            final = Shot.model_validate(load_json(paths.data / "shots.json")[0])
            self.assertEqual("human edit", final.content_summary)
            self.assertEqual("human", final.annotation_source)
            self.assertEqual("warning", results[0].status)
            receipt = load_json(paths.data / "vision_annotations.json")
            self.assertEqual([], receipt["annotated_shot_ids"])
            self.assertEqual(["shot_0001"], receipt["skipped_shot_ids"])

    def test_human_edit_during_failed_provider_call_is_never_rewritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = self.project(directory)
            entered = threading.Event()
            release = threading.Event()

            def analyze(*_args: object, **_kwargs: object) -> dict[str, object]:
                entered.set()
                self.assertTrue(release.wait(5))
                raise RuntimeError("provider failed")

            thread = threading.Thread(target=lambda: self.run_with_provider(paths, analyze))
            thread.start()
            self.assertTrue(entered.wait(5))
            current = [Shot.model_validate(item) for item in load_json(paths.data / "shots.json")]
            current[0].content_summary = "human survives failure"
            dump_json(paths.data / "shots.json", current)
            expected = (paths.data / "shots.json").read_bytes()
            release.set()
            thread.join(5)

            self.assertFalse(thread.is_alive())
            self.assertEqual(expected, (paths.data / "shots.json").read_bytes())

    def test_invalid_provider_payload_does_not_replace_shots_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = self.project(directory)
            shots_path = paths.data / "shots.json"
            before = shots_path.stat()
            expected = shots_path.read_bytes()

            result = self.run_with_provider(paths, lambda *_args, **_kwargs: {"content_summary": "partial"})

            after = shots_path.stat()
            self.assertEqual("warning", result.status)
            self.assertEqual(expected, shots_path.read_bytes())
            self.assertEqual((before.st_dev, before.st_ino, before.st_mtime_ns), (after.st_dev, after.st_ino, after.st_mtime_ns))

    def test_two_concurrent_vision_runs_allow_only_one_stale_snapshot_to_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = self.project(directory)
            provider_barrier = threading.Barrier(2)
            results: list[object] = []

            def analyze(*_args: object, **_kwargs: object) -> dict[str, object]:
                provider_barrier.wait(5)
                return _provider_payload(threading.current_thread().name)

            def run() -> None:
                results.append(self.run_with_provider(paths, analyze))

            threads = [threading.Thread(target=run, name=f"provider-{index}") for index in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(8)

            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(["success", "warning"], sorted(result.status for result in results))
            final = Shot.model_validate(load_json(paths.data / "shots.json")[0])
            self.assertIn(final.content_summary, {"provider-0", "provider-1"})
            self.assertEqual("openai", final.annotation_source)


class VisualAssetTransactionTest(unittest.TestCase):
    def project(self, directory: str) -> tuple[ProjectPaths, CanonicalMediaPackage]:
        paths = ProjectPaths(Path(directory) / "project")
        paths.ensure()
        (paths.keyframes / "shot_0001_mid.jpg").write_bytes(b"old-current")
        (paths.keyframes / "shot_9999_mid.jpg").write_bytes(b"old-stale")
        (paths.keyframes / "operator-reference.jpg").write_bytes(b"operator")
        (paths.assets / "contact_sheet.jpg").write_bytes(b"old-contact")
        (paths.data / "shots.json").write_bytes(b"old-shots")
        (paths.data / "scenes.json").write_bytes(b"old-scenes")
        (paths.data / "visual_generation.json").write_bytes(b"old-generation")
        (paths.reports / "shot_breakdown.csv").write_bytes(b"old-csv")
        return paths, _media(paths)

    @staticmethod
    def snapshot(paths: ProjectPaths) -> dict[str, bytes]:
        candidates = [
            *paths.keyframes.iterdir(),
            paths.assets / "contact_sheet.jpg",
            paths.data / "shots.json",
            paths.data / "scenes.json",
            paths.data / "visual_generation.json",
            paths.reports / "shot_breakdown.csv",
        ]
        return {
            candidate.relative_to(paths.root).as_posix(): candidate.read_bytes()
            for candidate in candidates
            if candidate.is_file()
        }

    def test_frame_or_contact_failure_preserves_all_old_visual_assets(self) -> None:
        for failure_point in (1, 4, "contact"):
            with self.subTest(failure_point=failure_point), tempfile.TemporaryDirectory() as directory:
                paths, media = self.project(directory)
                before = self.snapshot(paths)
                calls = 0

                def extract(_video: Path, output: Path, _seconds: float) -> None:
                    nonlocal calls
                    calls += 1
                    if calls == failure_point:
                        raise RuntimeError("frame extraction failed")
                    output.write_bytes(f"new-{calls}".encode())

                def contact(_video: Path, output: Path, _interval: float) -> None:
                    if failure_point == "contact":
                        raise RuntimeError("contact failed")
                    output.write_bytes(b"new-contact")

                with (
                    patch("video_analysis_mvp.visual._detect_shot_segments", return_value=([(0.0, 1.0, "high"), (1.0, 2.0, "high")], "test")),
                    patch("video_analysis_mvp.visual._extract_frame_at", side_effect=extract),
                    patch("video_analysis_mvp.visual._build_contact_sheet", side_effect=contact),
                    self.assertRaises(RuntimeError),
                ):
                    analyze_visual(media, paths)

                self.assertEqual(before, self.snapshot(paths))
                self.assertEqual([], list(paths.assets.glob(".visual-stage-*")))

    def test_commit_failure_rolls_back_assets_metadata_and_generation_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, media = self.project(directory)
            before = self.snapshot(paths)

            def extract(_video: Path, output: Path, _seconds: float) -> None:
                output.write_bytes(f"new-{output.name}".encode())

            def contact(_video: Path, output: Path, _interval: float) -> None:
                output.write_bytes(b"new-contact")

            real_replace = os.replace
            injected = False

            def fail_scenes_commit(
                source: str | os.PathLike[str],
                destination: str | os.PathLike[str],
                *args: object,
                **kwargs: object,
            ) -> None:
                nonlocal injected
                source_path = Path(source)
                destination_path = Path(destination)
                if (
                    not injected
                    and not args
                    and not kwargs
                    and source_path.name == "scenes.json"
                    and destination_path == paths.data / "scenes.json"
                ):
                    injected = True
                    raise OSError("injected metadata commit failure")
                real_replace(source, destination, *args, **kwargs)

            with (
                patch("video_analysis_mvp.visual._detect_shot_segments", return_value=([(0.0, 2.0, "high")], "test")),
                patch("video_analysis_mvp.visual._extract_frame_at", side_effect=extract),
                patch("video_analysis_mvp.visual._build_contact_sheet", side_effect=contact),
                patch("video_analysis_mvp.visual.os.replace", side_effect=fail_scenes_commit),
                self.assertRaisesRegex(OSError, "injected metadata commit failure"),
            ):
                analyze_visual(media, paths)

            self.assertEqual(before, self.snapshot(paths))
            self.assertEqual([], list(paths.assets.glob(".visual-stage-*")))

    def test_success_replaces_managed_set_and_preserves_manual_reference(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, media = self.project(directory)

            def extract(_video: Path, output: Path, _seconds: float) -> None:
                output.write_bytes(f"new-{output.name}".encode())

            def contact(_video: Path, output: Path, _interval: float) -> None:
                output.write_bytes(b"new-contact")

            with (
                patch("video_analysis_mvp.visual._detect_shot_segments", return_value=([(0.0, 2.0, "high")], "test")),
                patch("video_analysis_mvp.visual._extract_frame_at", side_effect=extract),
                patch("video_analysis_mvp.visual._build_contact_sheet", side_effect=contact),
            ):
                shots, _scenes = analyze_visual(media, paths)

            expected = set(shots[0].frame_refs)
            managed = {candidate.name for candidate in paths.keyframes.glob("shot_*.jpg")}
            self.assertEqual(expected, managed)
            self.assertFalse((paths.keyframes / "shot_9999_mid.jpg").exists())
            self.assertEqual(b"operator", (paths.keyframes / "operator-reference.jpg").read_bytes())
            self.assertEqual(b"new-contact", (paths.assets / "contact_sheet.jpg").read_bytes())
            receipt = load_json(paths.data / "visual_generation.json")
            self.assertEqual(2, receipt["schema_version"])
            self.assertEqual(["shot_0001"], receipt["shot_ids"])
            self.assertEqual(
                {"contact_sheet", "keyframes", "scenes", "shot_structure"},
                set(receipt["artifacts"]),
            )
            self.assertEqual((True, []), verify_visual_generation(paths))

            forged = dict(receipt)
            forged["artifacts"] = dict(receipt["artifacts"])
            forged["artifacts"]["shot_structure"] = dict(receipt["artifacts"]["shot_structure"])
            forged["artifacts"]["shot_structure"]["shot_count"] = True
            core = {
                "digest_algorithm": forged["digest_algorithm"],
                "shot_ids": forged["shot_ids"],
                "scene_ids": forged["scene_ids"],
                "artifacts": forged["artifacts"],
            }
            forged["generation_id"] = hashlib.sha256(
                json.dumps(
                    core,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            dump_json(paths.data / "visual_generation.json", forged)
            valid, reasons = verify_visual_generation(paths)
            self.assertFalse(valid)
            self.assertIn("visual shot structure receipt is invalid", reasons)
            dump_json(paths.data / "visual_generation.json", receipt)

            raw = load_json(paths.data / "shots.json")
            raw[0]["dialogue"] = "legal synthesis update"
            raw[0]["beat_density"] = 0.75
            dump_json(paths.data / "shots.json", raw)
            self.assertEqual((True, []), verify_visual_generation(paths))

            raw[0]["end_time"] = 1.5
            dump_json(paths.data / "shots.json", raw)
            valid, reasons = verify_visual_generation(paths)
            self.assertFalse(valid)
            self.assertIn("visual shot structure digest mismatch", reasons)


class AudioAssetTransactionTest(unittest.TestCase):
    @staticmethod
    def project(directory: str) -> tuple[ProjectPaths, CanonicalMediaPackage]:
        paths = ProjectPaths(Path(directory) / "project")
        paths.ensure()
        media = _media(paths)
        from tests.test_audio import write_pcm

        write_pcm(paths.assets / "audio.wav", media.duration_seconds)
        existing = {
            paths.data / "transcript.json": b"old-transcript",
            paths.data / "beats.json": b"old-beats",
            paths.data / "music_profile.json": b"old-music",
            paths.reports / "transcript.srt": b"old-srt",
            paths.reports / "music_rhythm_summary.json": b"old-summary",
            paths.data / "audio_generation.json": b"old-generation",
        }
        for path, payload in existing.items():
            path.write_bytes(payload)
        return paths, media

    @staticmethod
    def snapshot(paths: ProjectPaths) -> dict[Path, bytes]:
        return {
            path: path.read_bytes()
            for path in (
                paths.data / "transcript.json",
                paths.data / "beats.json",
                paths.data / "music_profile.json",
                paths.reports / "transcript.srt",
                paths.reports / "music_rhythm_summary.json",
                paths.data / "audio_generation.json",
            )
        }

    def test_commit_failure_rolls_back_all_five_outputs_and_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, media = self.project(directory)
            before = self.snapshot(paths)
            real_replace = os.replace
            injected = False

            def fail_beats_commit(
                source: str | os.PathLike[str],
                destination: str | os.PathLike[str],
                *args: object,
                **kwargs: object,
            ) -> None:
                nonlocal injected
                source_path = Path(source)
                destination_path = Path(destination)
                if (
                    not injected
                    and not args
                    and not kwargs
                    and source_path.name == "beats.json"
                    and destination_path == paths.data / "beats.json"
                ):
                    injected = True
                    raise OSError("injected audio commit failure")
                real_replace(source, destination, *args, **kwargs)

            with (
                patch("video_analysis_mvp.audio.transcribe_audio", return_value=[]),
                patch("video_analysis_mvp.audio.detect_beats", return_value=[]),
                patch("video_analysis_mvp.audio.profile_music", return_value=[]),
                patch("video_analysis_mvp.audio.os.replace", side_effect=fail_beats_commit),
                self.assertRaisesRegex(OSError, "injected audio commit failure"),
            ):
                analyze_audio(media, paths)

            self.assertEqual(before, self.snapshot(paths))
            self.assertEqual([], list(paths.root.glob(".audio-stage-*")))

    def test_receipt_marker_is_committed_last_and_missing_or_mismatched_marker_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory) / "project")
            paths.ensure()
            committed: list[Path] = []
            real_replace = os.replace

            def record_replace(
                source: str | os.PathLike[str],
                destination: str | os.PathLike[str],
                *args: object,
                **kwargs: object,
            ) -> None:
                destination_path = Path(destination)
                if destination_path.is_relative_to(paths.root) and ".audio-stage-" not in str(destination_path):
                    committed.append(destination_path)
                real_replace(source, destination, *args, **kwargs)

            with patch("video_analysis_mvp.audio.os.replace", side_effect=record_replace):
                _stage_and_commit_audio_generation(paths, [], [], [])

            self.assertEqual(paths.data / "audio_generation.json", committed[-1])
            self.assertEqual((True, []), verify_audio_generation(paths))

            marker = paths.data / "audio_generation.json"
            marker.unlink()
            valid, reasons = verify_audio_generation(paths)
            self.assertFalse(valid)
            self.assertIn("audio generation receipt is missing", reasons)

            _stage_and_commit_audio_generation(paths, [], [], [])
            (paths.data / "transcript.json").write_text('[{"forged":true}]', encoding="utf-8")
            valid, reasons = verify_audio_generation(paths)
            self.assertFalse(valid)
            self.assertTrue(any("audio artifact digest mismatch" in reason for reason in reasons), reasons)


class RuntimeConfigTransactionTest(unittest.TestCase):
    def test_thread_writers_serialize_and_merge_distinct_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            barrier = threading.Barrier(2)
            failures: list[Exception] = []

            def update(values: dict[str, str]) -> None:
                barrier.wait()
                try:
                    save_runtime_config(workspace, values)
                except Exception as exc:
                    failures.append(exc)

            threads = [
                threading.Thread(target=update, args=({"openai_model": "thread-model"},)),
                threading.Thread(target=update, args=({"minimax_api_host": "https://api.minimax.io"},)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)

            self.assertEqual([], failures)
            final = load_runtime_config(workspace)
            self.assertEqual("thread-model", final.openai_model)
            self.assertEqual("https://api.minimax.io", final.minimax_api_host)

    def test_process_writers_serialize_and_merge_distinct_fields(self) -> None:
        if os.name != "posix":
            self.skipTest("flock is POSIX-specific")
        with tempfile.TemporaryDirectory() as directory:
            context = multiprocessing.get_context("spawn")
            start = context.Event()
            results = context.Queue()
            updates = [
                {"openai_model": "process-model"},
                {"minimax_api_host": "https://api.minimax.io"},
            ] * 3
            processes = [
                context.Process(
                    target=_save_config_process,
                    args=(directory, values, start, results),
                )
                for values in updates
            ]
            for process in processes:
                process.start()
            start.set()
            for process in processes:
                process.join(10)

            self.assertTrue(all(process.exitcode == 0 for process in processes))
            self.assertEqual(
                [None] * len(processes),
                sorted([results.get(timeout=2) for _process in processes], key=str),
            )
            final = load_runtime_config(Path(directory))
            self.assertEqual("process-model", final.openai_model)
            self.assertEqual("https://api.minimax.io", final.minimax_api_host)


if __name__ == "__main__":
    unittest.main()
