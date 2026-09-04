from __future__ import annotations

import copy
import errno
import hashlib
import os
import subprocess
import sys
import tempfile
import tracemalloc
import unittest
from pathlib import Path
from unittest.mock import patch

from video_analysis_mvp import _audio_intelligence_storage as storage_module
from video_analysis_mvp import audio_intelligence as audio_intelligence_module
from video_analysis_mvp.artifacts import artifact_path
from video_analysis_mvp.audio import (
    AUDIO_ARTIFACT_RELATIVE_PATHS,
    _stage_and_commit_audio_generation,
)
from video_analysis_mvp.audio_intelligence import (
    AUDIO_TIMELINE_SCHEMA_ID,
    audio_intelligence_binding,
    audio_intelligence_status,
    cleanup_audio_intelligence_recovery,
    proposal_sha256,
    resolve_effective_proposal,
    stage_and_commit_audio_intelligence,
    validate_audio_timeline,
)
from video_analysis_mvp.paths import ProjectPaths
from video_analysis_mvp.schemas import (
    AnalysisProfile,
    BeatEvent,
    CanonicalMediaPackage,
    SourceType,
    dump_json,
    load_json,
)


def _proposal(
    *,
    label: str,
    text: str = "",
    speaker_id: str | None = None,
    voice_role: str = "unknown",
    verification: str = "machine_estimated",
    energy: float | None = None,
    onset_density: float | None = None,
    estimated_bpm: float | None = None,
) -> dict[str, object]:
    return {
        "label": label,
        "text": text,
        "language": "en" if text else "unknown",
        "speaker_id": speaker_id,
        "voice_role": voice_role,
        "energy": energy,
        "onset_density": onset_density,
        "estimated_bpm": estimated_bpm,
        "confidence": 0.7,
        "verification": verification,
    }


def _dataset() -> dict[str, object]:
    return {
        "schema_id": AUDIO_TIMELINE_SCHEMA_ID,
        "time_range_semantics": "[start,end)",
        "media_duration_seconds": 10.0,
        "sources": [
            {
                "source_id": "baseline-1",
                "capability": "baseline_features",
                "source_type": "deterministic_detector",
                "adapter": "vew.baseline",
                "adapter_version": "1",
                "engine": "ffmpeg",
                "engine_version": "8.1",
                "model": None,
                "device": "cpu",
                "status": "produced",
                "diagnostics": [],
            },
            {
                "source_id": "asr-1",
                "capability": "asr",
                "source_type": "adapter",
                "adapter": "legacy.transcript",
                "adapter_version": "1",
                "engine": "whisper",
                "engine_version": None,
                "model": "turbo",
                "device": "cpu",
                "status": "produced",
                "diagnostics": [],
            },
        ],
        "capabilities": {
            "baseline_features": {
                "status": "produced",
                "source_id": "baseline-1",
                "reason": None,
            },
            "asr": {"status": "produced", "source_id": "asr-1", "reason": None},
            "diarization": {
                "status": "unknown",
                "source_id": None,
                "reason": "adapter not configured",
            },
            "separation": {
                "status": "skipped",
                "source_id": None,
                "reason": "not requested",
            },
            "classification": {
                "status": "unknown",
                "source_id": None,
                "reason": "adapter not configured",
            },
        },
        "events": [
            {
                "event_id": "event-music-1",
                "start_time": 0.0,
                "end_time": 5.0,
                "kind": "music",
                "source_id": "baseline-1",
                "proposal": _proposal(label="music bed", verification="measured"),
                "review": None,
            },
            {
                "event_id": "event-voice-1",
                "start_time": 1.0,
                "end_time": 3.0,
                "kind": "voice",
                "source_id": "asr-1",
                "proposal": _proposal(
                    label="",
                    text="Original VO",
                ),
                "review": None,
            },
            {
                "event_id": "event-mixed-1",
                "start_time": 2.0,
                "end_time": 4.0,
                "kind": "mixed",
                "source_id": "baseline-1",
                "proposal": _proposal(
                    label="mixed foreground and bed",
                    energy=0.5,
                    onset_density=1.25,
                    estimated_bpm=120.0,
                ),
                "review": None,
            },
        ],
    }


class AudioTimelineSchemaTest(unittest.TestCase):
    def test_half_open_overlap_unknown_and_mixed_are_valid(self) -> None:
        result = validate_audio_timeline(_dataset())

        self.assertEqual("[start,end)", result["time_range_semantics"])
        self.assertEqual(
            ["music", "voice", "mixed"], [item["kind"] for item in result["events"]]
        )
        self.assertEqual("unknown", result["capabilities"]["diarization"]["status"])

    def test_explicit_human_blank_survives_effective_resolution(self) -> None:
        dataset = _dataset()
        voice = dataset["events"][1]
        proposal = voice["proposal"]
        voice["review"] = {
            "status": "reviewed",
            "expected_proposal_sha256": proposal_sha256(proposal),
            "overrides": {"text": ""},
            "review_notes": "Confirmed that the source contains no usable VO text.",
            "verification": "human_reviewed",
        }

        result = validate_audio_timeline(dataset)
        effective = resolve_effective_proposal(result["events"][1])

        self.assertIsNotNone(effective)
        self.assertEqual("", effective["text"])
        self.assertEqual("human_reviewed", effective["verification"])

    def test_rejected_review_resolves_to_none(self) -> None:
        dataset = _dataset()
        voice = dataset["events"][1]
        voice["review"] = {
            "status": "rejected",
            "expected_proposal_sha256": proposal_sha256(voice["proposal"]),
            "overrides": {},
            "review_notes": "False positive.",
            "verification": "human_reviewed",
        }
        result = validate_audio_timeline(dataset)
        self.assertIsNone(resolve_effective_proposal(result["events"][1]))

    def test_needs_work_review_is_a_draft_and_has_no_effective_proposal(self) -> None:
        dataset = _dataset()
        voice = dataset["events"][1]
        voice["review"] = {
            "status": "needs_work",
            "expected_proposal_sha256": proposal_sha256(voice["proposal"]),
            "overrides": {"text": "Draft correction"},
            "review_notes": "The operator has not confirmed this text.",
            "verification": "human_draft",
        }

        result = validate_audio_timeline(dataset)

        self.assertEqual("human_draft", result["events"][1]["review"]["verification"])
        self.assertIsNone(resolve_effective_proposal(result["events"][1]))

        voice["review"]["verification"] = "human_reviewed"
        with self.assertRaisesRegex(ValueError, "human_draft"):
            validate_audio_timeline(dataset)

    def test_illegal_values_fail_closed(self) -> None:
        cases: list[tuple[str, callable]] = [
            (
                "non-finite",
                lambda value: value["events"][0].update(start_time=float("nan")),
            ),
            ("out of range", lambda value: value["events"][0].update(end_time=11.0)),
            ("zero duration", lambda value: value["events"][0].update(end_time=0.0)),
            (
                "unknown source",
                lambda value: value["events"][0].update(source_id="missing-source"),
            ),
            (
                "duplicate event",
                lambda value: value["events"].append(copy.deepcopy(value["events"][0])),
            ),
            ("unknown root", lambda value: value.update(extra=True)),
            ("unsorted", lambda value: value["events"].reverse()),
        ]
        for label, mutate in cases:
            with self.subTest(label=label):
                dataset = _dataset()
                mutate(dataset)
                with self.assertRaises(ValueError):
                    validate_audio_timeline(dataset)

    def test_unknown_capability_cannot_fabricate_events(self) -> None:
        dataset = _dataset()
        dataset["capabilities"]["baseline_features"] = {
            "status": "unknown",
            "source_id": None,
            "reason": "not available",
        }
        with self.assertRaisesRegex(ValueError, "selected capability source"):
            validate_audio_timeline(dataset)

    def test_review_overrides_preserve_event_kind_constraints(self) -> None:
        dataset = _dataset()
        music = dataset["events"][0]
        music["review"] = {
            "status": "reviewed",
            "expected_proposal_sha256": proposal_sha256(music["proposal"]),
            "overrides": {"speaker_id": "speaker-1"},
            "review_notes": "Invalid for a music event.",
            "verification": "human_reviewed",
        }
        with self.assertRaisesRegex(ValueError, "speaker_id"):
            validate_audio_timeline(dataset)

    def test_user_evidence_text_is_not_mistaken_for_private_metadata(self) -> None:
        dataset = _dataset()
        dataset["events"][1]["proposal"]["text"] = (
            "The speaker literally says /Users on screen."
        )
        result = validate_audio_timeline(dataset)
        self.assertIn("/Users", result["events"][1]["proposal"]["text"])

    def test_event_provenance_cross_field_contracts_fail_closed(self) -> None:
        cases: list[tuple[str, callable, str]] = [
            (
                "non-selected produced source",
                lambda value: (
                    value["sources"].append(
                        {
                            **copy.deepcopy(value["sources"][0]),
                            "source_id": "baseline-2",
                        }
                    ),
                    value["events"][0].update(source_id="baseline-2"),
                ),
                "selected capability source",
            ),
            (
                "music from asr",
                lambda value: value["events"][0].update(source_id="asr-1"),
                "incompatible with source capability",
            ),
            (
                "transcript on music",
                lambda value: value["events"][0]["proposal"].update(
                    text="Not a music field",
                    language="en",
                    voice_role="voice_over",
                ),
                "transcript text",
            ),
            (
                "human proposal source",
                lambda value: value["sources"][0].update(source_type="human"),
                "source_type is unsupported",
            ),
            (
                "measured adapter proposal",
                lambda value: value["events"][1]["proposal"].update(
                    verification="measured"
                ),
                "incompatible with its source type",
            ),
            (
                "asr owns no acoustic fields",
                lambda value: value["events"][1]["proposal"].update(energy=0.5),
                "acoustic fields are not owned by asr",
            ),
            (
                "asr owns no speaker clusters",
                lambda value: value["events"][1]["proposal"].update(
                    speaker_id="speaker_1"
                ),
                "speaker_id is not owned by asr",
            ),
            (
                "asr cannot assert voice-over role",
                lambda value: value["events"][1]["proposal"].update(
                    voice_role="voice_over"
                ),
                "voice_role is not owned by asr",
            ),
        ]
        for label, mutate, message in cases:
            with self.subTest(label=label):
                dataset = _dataset()
                mutate(dataset)
                with self.assertRaisesRegex(ValueError, message):
                    validate_audio_timeline(dataset)

    def test_speaker_ids_are_anonymous_clusters_only(self) -> None:
        dataset = _dataset()
        dataset["sources"].append(
            {
                "source_id": "diarization-1",
                "capability": "diarization",
                "source_type": "adapter",
                "adapter": "example.diarization",
                "adapter_version": "1",
                "engine": "example",
                "engine_version": "1",
                "model": None,
                "device": "cpu",
                "status": "produced",
                "diagnostics": [],
            }
        )
        dataset["capabilities"]["diarization"] = {
            "status": "produced",
            "source_id": "diarization-1",
            "reason": None,
        }
        dataset["events"].append(
            {
                "event_id": "event-speaker-1",
                "start_time": 4.0,
                "end_time": 5.0,
                "kind": "voice",
                "source_id": "diarization-1",
                "proposal": _proposal(label="", speaker_id="john-smith"),
                "review": None,
            }
        )
        with self.assertRaisesRegex(ValueError, "anonymous cluster"):
            validate_audio_timeline(dataset)

        dataset["events"][-1]["proposal"]["speaker_id"] = "speaker_0007"
        result = validate_audio_timeline(dataset)
        self.assertEqual("speaker_0007", result["events"][-1]["proposal"]["speaker_id"])

    def test_empty_label_is_valid_when_a_label_cannot_be_inferred(self) -> None:
        dataset = _dataset()
        dataset["events"][0]["proposal"]["label"] = ""

        result = validate_audio_timeline(dataset)

        self.assertEqual("", result["events"][0]["proposal"]["label"])

    def test_unreviewed_voice_roles_cannot_be_measured(self) -> None:
        for source_type in ("deterministic_detector", "imported"):
            with self.subTest(source_type=source_type):
                dataset = _dataset()
                source = dataset["sources"][0]
                source.update(capability="classification", source_type=source_type)
                dataset["capabilities"]["baseline_features"] = {
                    "status": "unknown",
                    "source_id": None,
                    "reason": "not run",
                }
                dataset["capabilities"]["classification"] = {
                    "status": "produced",
                    "source_id": source["source_id"],
                    "reason": None,
                }
                event = dataset["events"][0]
                event["kind"] = "voice"
                event["proposal"]["voice_role"] = "voice_over"
                dataset["events"] = [event]
                with self.assertRaisesRegex(ValueError, "remain an estimate"):
                    validate_audio_timeline(dataset)

    def test_human_review_may_confirm_role_on_a_measured_voice_event(self) -> None:
        dataset = _dataset()
        event = dataset["events"][0]
        event["kind"] = "voice"
        event["review"] = {
            "status": "reviewed",
            "expected_proposal_sha256": proposal_sha256(event["proposal"]),
            "overrides": {"voice_role": "voice_over"},
            "review_notes": "Role confirmed against source.",
            "verification": "human_reviewed",
        }
        dataset["events"] = [event]
        result = validate_audio_timeline(dataset)
        effective = resolve_effective_proposal(result["events"][0])
        self.assertEqual("voice_over", effective["voice_role"])
        self.assertEqual("human_reviewed", effective["verification"])

    def test_every_produced_source_must_be_selected_by_its_capability(self) -> None:
        dataset = _dataset()
        dataset["sources"].append(
            {**copy.deepcopy(dataset["sources"][0]), "source_id": "baseline-unused"}
        )

        with self.assertRaisesRegex(ValueError, "not the selected capability source"):
            validate_audio_timeline(dataset)

    def test_metadata_rejects_embedded_private_paths_and_credentials(self) -> None:
        for diagnostic in (
            "path=/Users/alice/private/model.bin",
            "cache=/root/private/model.bin",
            "cache=/Volumes/ClientDrive/model.bin",
            "cache=file:///Users/alice/private/model.bin",
            "Failed to open '/Users/example/private/model.bin'",
            '{"password":"synthetic-value-only"}',
            "auth: Bearer abcdefghijklmnop",
            "token=ghp_abcdefghijklmnopqrstuvwxyz123456",
            "token=sk-proj-abcdefghijklmnopqrstuvwxyz123456",
        ):
            with self.subTest(diagnostic=diagnostic):
                dataset = _dataset()
                dataset["sources"][0]["diagnostics"] = [diagnostic]
                with self.assertRaises(ValueError):
                    validate_audio_timeline(dataset)


class AudioIntelligenceReceiptTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="audio-intelligence-")
        self.paths = self._project(Path(self.temporary.name))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _project(root: Path) -> ProjectPaths:
        paths = ProjectPaths(root / "audio-project")
        paths.ensure()
        (paths.assets / "audio.wav").write_bytes(b"synthetic-wav-evidence")
        media = CanonicalMediaPackage(
            project_id=paths.root.name,
            source_type=SourceType.file,
            source="synthetic.mp4",
            local_master_path=str(paths.ingest / "master.mp4"),
            review_copy_path=str(paths.assets / "review.mp4"),
            audio_path=str(paths.assets / "audio.wav"),
            duration_seconds=10.0,
            frame_rate=24.0,
            resolution="1920x1080",
            aspect_ratio=16 / 9,
            status="analyzed",
            analysis_profile=AnalysisProfile.research,
            metadata={},
        )
        dump_json(paths.data / "media_package.json", media)
        _stage_and_commit_audio_generation(paths, [], [], [])
        return paths

    def test_old_project_without_new_files_is_valid_but_unavailable(self) -> None:
        status = audio_intelligence_status(self.paths)
        self.assertEqual(
            {"available": False, "valid": True, "binding": None, "reasons": []},
            status,
        )

    def test_receipt_binds_legacy_audio_media_wav_and_dataset(self) -> None:
        legacy_before = {
            relative: (self.paths.root / relative).read_bytes()
            for relative in (
                *AUDIO_ARTIFACT_RELATIVE_PATHS,
                "data/audio_generation.json",
            )
        }

        binding = stage_and_commit_audio_intelligence(
            self.paths,
            _dataset(),
            parameters={"window_seconds": 0.25, "adapter": "baseline-v1"},
        )

        receipt = load_json(
            artifact_path(self.paths.root, "audio_intelligence_generation")
        )
        self.assertEqual(AUDIO_TIMELINE_SCHEMA_ID, binding["dataset_schema"])
        self.assertEqual("committed", receipt["state"])
        self.assertEqual(
            {"audio_generation", "media_package", "audio_wav"},
            set(receipt["inputs"]),
        )
        self.assertEqual({"audio_intelligence"}, set(receipt["artifacts"]))
        self.assertEqual(
            legacy_before,
            {
                relative: (self.paths.root / relative).read_bytes()
                for relative in (
                    *AUDIO_ARTIFACT_RELATIVE_PATHS,
                    "data/audio_generation.json",
                )
            },
        )
        self.assertEqual(binding, audio_intelligence_binding(self.paths))

    def test_each_bound_input_or_output_mutation_fails_closed(self) -> None:
        mutations = {
            "audio_wav": lambda paths: (paths.assets / "audio.wav").write_bytes(
                b"changed-wav"
            ),
            "media_package": lambda paths: (
                paths.data / "media_package.json"
            ).write_text(
                (paths.data / "media_package.json").read_text(encoding="utf-8") + " ",
                encoding="utf-8",
            ),
            "audio_generation": lambda paths: _stage_and_commit_audio_generation(
                paths,
                [],
                [BeatEvent(time=1.0, strength=0.8)],
                [],
            ),
            "dataset": lambda paths: (
                paths.data / "audio_intelligence.json"
            ).write_text(
                (paths.data / "audio_intelligence.json").read_text(encoding="utf-8")
                + " ",
                encoding="utf-8",
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                paths = self._project(Path(directory))
                stage_and_commit_audio_intelligence(paths, _dataset())
                mutate(paths)
                status = audio_intelligence_status(paths)
                self.assertTrue(status["available"])
                self.assertFalse(status["valid"], status)

    def test_forged_generation_id_and_extra_fields_are_rejected(self) -> None:
        stage_and_commit_audio_intelligence(self.paths, _dataset())
        receipt_path = artifact_path(self.paths.root, "audio_intelligence_generation")
        receipt = load_json(receipt_path)
        receipt["generation_id"] = "0" * 64
        dump_json(receipt_path, receipt)
        with self.assertRaisesRegex(ValueError, "generation id"):
            audio_intelligence_binding(self.paths)

        stage_and_commit_audio_intelligence(self.paths, _dataset())
        receipt = load_json(receipt_path)
        receipt["extra"] = True
        dump_json(receipt_path, receipt)
        with self.assertRaisesRegex(ValueError, "fields"):
            audio_intelligence_binding(self.paths)

    def test_parameters_reject_secrets_nonfinite_and_private_paths(self) -> None:
        for parameters in (
            {"api_key": "secret"},
            {"apiKey": "secret"},
            {"apikey": "secret"},
            {"APIKEY": "secret"},
            {"accessToken": "secret"},
            {"accesskey": "secret"},
            {"secretkey": "secret"},
            {"clientsecret": "secret"},
            {"authheader": "secret"},
            {"provider_token": "secret"},
            {"auth": "Bearer abcdefghijklmnop"},
            {"threshold": float("nan")},
            {"model_path": "/Users/private/model.bin"},
            {"cache": "model=/Users/alice/private/model.bin"},
            {"cache": "model=/root/private/model.bin"},
            {"cache": "model=/Volumes/ClientDrive/model.bin"},
            {"cache": "file:///Users/alice/private/model.bin"},
            {"note": "Failed to open '/Users/example/private/model.bin'"},
            {"note": '{"password":"synthetic-value-only"}'},
            {"note": "authorization=abcdef123456"},
            {"note": "ghp_abcdefghijklmnopqrstuvwxyz123456"},
            {"note": "sk-proj-abcdefghijklmnopqrstuvwxyz123456"},
            {1: "invalid-key"},
            {"bad\nkey": "invalid-control"},
        ):
            with self.subTest(parameters=parameters), self.assertRaises(ValueError):
                stage_and_commit_audio_intelligence(
                    self.paths, _dataset(), parameters=parameters
                )

        binding = stage_and_commit_audio_intelligence(
            self.paths,
            _dataset(),
            parameters={"tokenizer": "relative/model", "windowSeconds": 0.25},
        )
        self.assertEqual(AUDIO_TIMELINE_SCHEMA_ID, binding["dataset_schema"])

    def test_quoted_sensitive_diagnostics_cannot_be_persisted(self) -> None:
        for diagnostic in (
            "Failed to open '/Users/example/private/model.bin'",
            '{"password":"synthetic-value-only"}',
        ):
            with self.subTest(diagnostic=diagnostic):
                dataset = _dataset()
                dataset["sources"][0]["diagnostics"] = [diagnostic]
                with self.assertRaises(ValueError):
                    stage_and_commit_audio_intelligence(self.paths, dataset)
                self.assertFalse((self.paths.data / "audio_intelligence.json").exists())
                self.assertFalse(
                    (self.paths.data / "audio_intelligence_generation.json").exists()
                )

    def test_receipt_marker_commits_last(self) -> None:
        committed: list[str] = []
        real_replace = os.replace

        def record_replace(
            source: os.PathLike[str] | str,
            destination: os.PathLike[str] | str,
            *args: object,
            **kwargs: object,
        ) -> None:
            if kwargs.get("dst_dir_fd") is not None and str(destination) in {
                "audio_intelligence.json",
                "audio_intelligence_generation.json",
            }:
                committed.append(str(destination))
            real_replace(source, destination, *args, **kwargs)

        with patch(
            "video_analysis_mvp._audio_intelligence_storage.os.replace",
            side_effect=record_replace,
        ):
            stage_and_commit_audio_intelligence(self.paths, _dataset())

        self.assertEqual(
            "audio_intelligence_generation.json",
            committed[-1],
        )

    def test_commit_failure_restores_previous_dataset_and_marker(self) -> None:
        stage_and_commit_audio_intelligence(self.paths, _dataset())
        dataset_path = artifact_path(self.paths.root, "audio_intelligence")
        receipt_path = artifact_path(self.paths.root, "audio_intelligence_generation")
        before = {
            dataset_path: dataset_path.read_bytes(),
            receipt_path: receipt_path.read_bytes(),
        }
        changed = _dataset()
        changed["events"][1]["proposal"]["text"] = "Changed VO"
        real_replace = os.replace
        receipt_replace_count = 0

        def fail_marker(
            source: os.PathLike[str] | str,
            destination: os.PathLike[str] | str,
            *args: object,
            **kwargs: object,
        ) -> None:
            nonlocal receipt_replace_count
            if str(source) == str(destination) == "audio_intelligence_generation.json":
                receipt_replace_count += 1
            if receipt_replace_count == 2:
                raise OSError("injected audio intelligence marker failure")
            real_replace(source, destination, *args, **kwargs)

        with (
            patch(
                "video_analysis_mvp._audio_intelligence_storage.os.replace",
                side_effect=fail_marker,
            ),
            self.assertRaisesRegex(
                OSError, "injected audio intelligence marker failure"
            ),
        ):
            stage_and_commit_audio_intelligence(self.paths, changed)

        self.assertEqual(
            before,
            {
                dataset_path: dataset_path.read_bytes(),
                receipt_path: receipt_path.read_bytes(),
            },
        )
        self.assertEqual([], list(self.paths.root.glob(".audio-intelligence-stage-*")))
        self.assertEqual(
            [], list(self.paths.root.glob(".audio-intelligence-recovery-*"))
        )
        self.assertTrue(audio_intelligence_status(self.paths)["valid"])

    def test_each_forward_replace_failure_preserves_the_previous_generation(
        self,
    ) -> None:
        for has_previous in (False, True):
            for failure_index in range(1, (4 if has_previous else 2) + 1):
                with (
                    self.subTest(
                        has_previous=has_previous, failure_index=failure_index
                    ),
                    tempfile.TemporaryDirectory() as raw,
                ):
                    paths = self._project(Path(raw))
                    if has_previous:
                        stage_and_commit_audio_intelligence(paths, _dataset())
                    targets = [
                        paths.data / name for name in storage_module.STAGED_FILES
                    ]
                    before = [
                        path.read_bytes() if path.exists() else None for path in targets
                    ]
                    real_replace = os.replace
                    calls = 0

                    def fail_one_replace(
                        *args: object,
                        _fail_at: int = failure_index,
                        _replace=real_replace,
                        **kwargs: object,
                    ) -> None:
                        nonlocal calls
                        calls += 1
                        if calls == _fail_at:
                            raise OSError("injected forward replacement failure")
                        _replace(*args, **kwargs)

                    changed = _dataset()
                    changed["events"][1]["proposal"]["text"] = (
                        "Must not survive a failed commit"
                    )
                    with (
                        patch.object(
                            storage_module.os, "replace", side_effect=fail_one_replace
                        ),
                        self.assertRaisesRegex(OSError, "forward replacement failure"),
                    ):
                        stage_and_commit_audio_intelligence(paths, changed)
                    self.assertEqual(
                        before,
                        [
                            path.read_bytes() if path.exists() else None
                            for path in targets
                        ],
                    )
                    self.assertEqual(
                        [], list(paths.root.glob(".audio-intelligence-recovery-*"))
                    )
                    self.assertEqual(
                        [], list(paths.root.glob(".audio-intelligence-stage-*"))
                    )
                    self.assertTrue(audio_intelligence_status(paths)["valid"])

    def test_double_failure_retains_old_marker_in_durable_recovery_directory(
        self,
    ) -> None:
        stage_and_commit_audio_intelligence(self.paths, _dataset())
        dataset_path = artifact_path(self.paths.root, "audio_intelligence")
        receipt_path = artifact_path(self.paths.root, "audio_intelligence_generation")
        old_dataset = dataset_path.read_bytes()
        old_receipt = receipt_path.read_bytes()
        changed = _dataset()
        changed["events"][1]["proposal"]["text"] = "Changed VO"
        real_replace = os.replace
        receipt_replace_count = 0

        def fail_commit_and_receipt_restore(
            source: os.PathLike[str] | str,
            destination: os.PathLike[str] | str,
            *args: object,
            **kwargs: object,
        ) -> None:
            nonlocal receipt_replace_count
            if str(source) == str(destination) == "audio_intelligence_generation.json":
                receipt_replace_count += 1
                if receipt_replace_count in {2, 3}:
                    raise OSError("injected receipt replace failure")
            real_replace(source, destination, *args, **kwargs)

        with (
            patch(
                "video_analysis_mvp._audio_intelligence_storage.os.replace",
                side_effect=fail_commit_and_receipt_restore,
            ),
            self.assertRaisesRegex(RuntimeError, "previous bytes are retained"),
        ):
            stage_and_commit_audio_intelligence(self.paths, changed)

        self.assertEqual(old_dataset, dataset_path.read_bytes())
        self.assertFalse(receipt_path.exists())
        recovery = list(self.paths.root.glob(".audio-intelligence-recovery-*"))
        self.assertEqual(1, len(recovery))
        self.assertEqual(
            old_receipt,
            (recovery[0] / "audio_intelligence_generation.json").read_bytes(),
        )
        self.assertFalse(audio_intelligence_status(self.paths)["valid"])

    def test_post_commit_binding_failure_rolls_back_before_recovery_cleanup(
        self,
    ) -> None:
        stage_and_commit_audio_intelligence(self.paths, _dataset())
        dataset_path = artifact_path(self.paths.root, "audio_intelligence")
        receipt_path = artifact_path(self.paths.root, "audio_intelligence_generation")
        before = {
            dataset_path: dataset_path.read_bytes(),
            receipt_path: receipt_path.read_bytes(),
        }
        changed = _dataset()
        changed["events"][1]["proposal"]["text"] = "Changed but rejected after commit"
        real_binding = audio_intelligence_module._audio_intelligence_binding_locked

        def fail_descriptor_bound_validation(
            *args: object, **kwargs: object
        ) -> dict[str, object]:
            if kwargs.get("data_fd") is not None:
                raise ValueError("injected post-commit binding failure")
            return real_binding(*args, **kwargs)

        with (
            patch.object(
                audio_intelligence_module,
                "_audio_intelligence_binding_locked",
                side_effect=fail_descriptor_bound_validation,
            ),
            self.assertRaisesRegex(ValueError, "post-commit binding failure"),
        ):
            stage_and_commit_audio_intelligence(self.paths, changed)

        self.assertEqual(
            before,
            {
                dataset_path: dataset_path.read_bytes(),
                receipt_path: receipt_path.read_bytes(),
            },
        )
        self.assertEqual(
            [], list(self.paths.root.glob(".audio-intelligence-recovery-*"))
        )
        self.assertTrue(audio_intelligence_status(self.paths)["valid"])

    def test_cleanup_failure_is_visible_and_can_be_retried_safely(self) -> None:
        stage_and_commit_audio_intelligence(self.paths, _dataset())
        changed = _dataset()
        changed["events"][1]["proposal"]["text"] = "Current generation remains valid"
        real_remove = storage_module._remove_recovery_directory
        injected = False

        def fail_old_generation_cleanup(*args: object, **kwargs: object) -> None:
            nonlocal injected
            if kwargs.get("names") and not injected:
                injected = True
                raise OSError("injected recovery cleanup failure")
            real_remove(*args, **kwargs)

        with patch.object(
            storage_module,
            "_remove_recovery_directory",
            side_effect=fail_old_generation_cleanup,
        ):
            binding = stage_and_commit_audio_intelligence(self.paths, changed)

        self.assertTrue(injected)
        self.assertTrue(binding["cleanup_required"])
        self.assertEqual(1, len(binding["recovery_directories"]))
        self.assertTrue(audio_intelligence_status(self.paths)["valid"])

        cleanup = cleanup_audio_intelligence_recovery(self.paths)

        self.assertFalse(cleanup["cleanup_required"])
        self.assertEqual([], cleanup["recovery_directories"])
        self.assertFalse(audio_intelligence_binding(self.paths)["cleanup_required"])

    def test_recovery_cleanup_refuses_to_delete_backups_for_an_invalid_current_generation(
        self,
    ) -> None:
        stage_and_commit_audio_intelligence(self.paths, _dataset())
        changed = _dataset()
        changed["events"][1]["proposal"]["text"] = "Current before corruption"
        real_remove = storage_module._remove_recovery_directory

        def fail_old_generation_cleanup(*args: object, **kwargs: object) -> None:
            if kwargs.get("names"):
                raise OSError("injected recovery cleanup failure")
            real_remove(*args, **kwargs)

        with patch.object(
            storage_module,
            "_remove_recovery_directory",
            side_effect=fail_old_generation_cleanup,
        ):
            binding = stage_and_commit_audio_intelligence(self.paths, changed)
        self.assertTrue(binding["cleanup_required"])
        recovery_before = list(self.paths.root.glob(".audio-intelligence-recovery-*"))

        dataset_path = artifact_path(self.paths.root, "audio_intelligence")
        dataset_path.write_bytes(dataset_path.read_bytes() + b" ")

        with self.assertRaisesRegex(ValueError, "dataset digest mismatch"):
            cleanup_audio_intelligence_recovery(self.paths)
        self.assertEqual(
            recovery_before,
            list(self.paths.root.glob(".audio-intelligence-recovery-*")),
        )

    def test_recovery_open_failure_removes_the_new_empty_directory(self) -> None:
        real_open = os.open

        def fail_recovery_open(
            path: os.PathLike[str] | str, *args: object, **kwargs: object
        ) -> int:
            if str(path).startswith(".audio-intelligence-recovery-"):
                raise OSError("injected recovery open failure")
            return real_open(path, *args, **kwargs)

        with (
            patch(
                "video_analysis_mvp._audio_intelligence_storage.os.open",
                side_effect=fail_recovery_open,
            ),
            self.assertRaisesRegex(OSError, "injected recovery open failure"),
        ):
            stage_and_commit_audio_intelligence(self.paths, _dataset())

        self.assertEqual(
            [], list(self.paths.root.glob(".audio-intelligence-recovery-*"))
        )

    def test_stage_cleanup_sync_failure_returns_committed_warning_and_closes_fds(
        self,
    ) -> None:
        stage_and_commit_audio_intelligence(self.paths, _dataset())
        changed = _dataset()
        changed["events"][1]["proposal"]["text"] = (
            "Committed despite cleanup sync warning"
        )
        real_cleanup = storage_module._cleanup_staging_descriptors
        real_sync = storage_module._fsync_directory_fd
        descriptors: list[int] = []
        cleanup_root = -1
        injected = False

        def record_cleanup(
            root_fd: int, stage_root_fd: int, data_fd: int, name: str
        ) -> list[str]:
            nonlocal cleanup_root
            cleanup_root = root_fd
            descriptors.extend((root_fd, stage_root_fd, data_fd))
            return real_cleanup(root_fd, stage_root_fd, data_fd, name)

        def fail_final_root_sync(descriptor: int) -> None:
            nonlocal injected
            if descriptor == cleanup_root and not injected:
                injected = True
                raise OSError(errno.EIO, "synthetic stage cleanup sync failure")
            real_sync(descriptor)

        with (
            patch.object(
                storage_module,
                "_cleanup_staging_descriptors",
                side_effect=record_cleanup,
            ),
            patch.object(
                storage_module, "_fsync_directory_fd", side_effect=fail_final_root_sync
            ),
        ):
            result = stage_and_commit_audio_intelligence(self.paths, changed)
        self.assertTrue(injected)
        self.assertTrue(result["cleanup_required"])
        self.assertIn("stage_root_sync:OSError", result["cleanup_warnings"])
        self.assertTrue(audio_intelligence_status(self.paths)["valid"])
        for descriptor in descriptors:
            with self.assertRaises(OSError) as caught:
                os.fstat(descriptor)
            self.assertEqual(errno.EBADF, caught.exception.errno)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO is POSIX-specific")
    def test_special_files_fail_closed_without_blocking_status(self) -> None:
        for relative in (
            "data/audio_intelligence_generation.json",
            "data/audio_intelligence.json",
            "data/media_package.json",
            "assets/audio.wav",
        ):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as raw:
                paths = self._project(Path(raw))
                stage_and_commit_audio_intelligence(paths, _dataset())
                target = paths.root / relative
                target.unlink()
                os.mkfifo(target)
                code = (
                    "from pathlib import Path; "
                    "from video_analysis_mvp.paths import ProjectPaths; "
                    "from video_analysis_mvp.audio_intelligence import audio_intelligence_status; "
                    "import sys; "
                    "assert audio_intelligence_status(ProjectPaths(Path(sys.argv[1])))['valid'] is False"
                )
                subprocess.run(
                    [sys.executable, "-c", code, str(paths.root)],
                    check=True,
                    capture_output=True,
                    timeout=3,
                )

    def test_stage_path_swap_cannot_change_the_committed_bytes(self) -> None:
        stage_and_commit_audio_intelligence(self.paths, _dataset())
        changed = _dataset()
        changed["events"][1]["proposal"]["text"] = "Descriptor-bound stage bytes"
        real_write = audio_intelligence_module.write_staged_file
        real_replace = os.replace
        state: dict[str, object] = {}

        def swap_stage_path_after_dataset_write(
            area: object, name: str, payload: bytes
        ) -> None:
            real_write(area, name, payload)
            if name != "audio_intelligence.json" or state:
                return
            stage_name = area.stage_name
            stage_path = self.paths.root / stage_name
            held_path = self.paths.root / f"{stage_name}-held"
            os.rename(stage_path, held_path)
            attacker_data = stage_path / "data"
            attacker_data.mkdir(parents=True)
            (attacker_data / "audio_intelligence.json").write_bytes(b"not-json")
            (attacker_data / "audio_intelligence_generation.json").write_bytes(
                b"not-json"
            )
            state.update(area=area, stage_path=stage_path, held_path=held_path)

        def restore_stage_path_after_marker_commit(
            source: os.PathLike[str] | str,
            destination: os.PathLike[str] | str,
            *args: object,
            **kwargs: object,
        ) -> None:
            real_replace(source, destination, *args, **kwargs)
            area = state.get("area")
            if (
                area is not None
                and kwargs.get("src_dir_fd") == area.data_fd
                and str(destination) == "audio_intelligence_generation.json"
            ):
                stage_path = state["stage_path"]
                held_path = state["held_path"]
                for candidate in (stage_path / "data").iterdir():
                    candidate.unlink()
                (stage_path / "data").rmdir()
                stage_path.rmdir()
                os.rename(held_path, stage_path)

        with (
            patch.object(
                audio_intelligence_module,
                "write_staged_file",
                side_effect=swap_stage_path_after_dataset_write,
            ),
            patch(
                "video_analysis_mvp._audio_intelligence_storage.os.replace",
                side_effect=restore_stage_path_after_marker_commit,
            ),
        ):
            binding = stage_and_commit_audio_intelligence(self.paths, changed)

        stored = load_json(artifact_path(self.paths.root, "audio_intelligence"))
        self.assertEqual(
            "Descriptor-bound stage bytes", stored["events"][1]["proposal"]["text"]
        )
        self.assertEqual(binding, audio_intelligence_binding(self.paths))

    def test_parent_directory_swap_cannot_redirect_commit(self) -> None:
        outside = Path(self.temporary.name) / "outside-data"
        outside.mkdir()
        held_data = self.paths.root / "data-held-during-commit"
        real_replace = os.replace
        swapped = False

        def swap_parent_between_marker_replacements(
            source: os.PathLike[str] | str,
            destination: os.PathLike[str] | str,
            *args: object,
            **kwargs: object,
        ) -> None:
            nonlocal swapped
            real_replace(source, destination, *args, **kwargs)
            if (
                str(source) == str(destination) == "audio_intelligence.json"
                and not swapped
            ):
                os.rename(self.paths.data, held_data)
                self.paths.data.symlink_to(outside, target_is_directory=True)
                swapped = True
            elif (
                str(source) == str(destination) == "audio_intelligence_generation.json"
                and swapped
            ):
                self.paths.data.unlink()
                os.rename(held_data, self.paths.data)

        with patch(
            "video_analysis_mvp._audio_intelligence_storage.os.replace",
            side_effect=swap_parent_between_marker_replacements,
        ):
            binding = stage_and_commit_audio_intelligence(self.paths, _dataset())

        self.assertTrue(swapped)
        self.assertEqual([], list(outside.iterdir()))
        self.assertEqual(binding, audio_intelligence_binding(self.paths))

    def test_dataset_digest_and_schema_are_read_from_the_same_bytes(self) -> None:
        stage_and_commit_audio_intelligence(self.paths, _dataset())
        dataset_path = artifact_path(self.paths.root, "audio_intelligence")
        replacement = _dataset()
        replacement["events"][1]["proposal"]["text"] = "Replacement VO"
        attempted_dataset_mutation = False

        def replacing_receipt_reader(path: Path, maximum: int) -> dict[str, object]:
            nonlocal attempted_dataset_mutation
            receipt = audio_intelligence_module._file_receipt(path, maximum)
            if path == dataset_path:
                attempted_dataset_mutation = True
                dump_json(path, replacement)
            return receipt

        binding = audio_intelligence_binding(
            self.paths,
            file_receipt_reader=replacing_receipt_reader,
        )

        self.assertFalse(attempted_dataset_mutation)
        self.assertEqual(
            hashlib.sha256(dataset_path.read_bytes()).hexdigest(),
            binding["dataset_sha256"],
        )

    def test_audio_file_receipt_hashes_without_retaining_the_full_audio(self) -> None:
        wav = self.paths.assets / "large-test.wav"
        size = 8 * 1024 * 1024
        with wav.open("wb") as stream:
            stream.truncate(size)
        tracemalloc.start()
        try:
            receipt = storage_module.file_receipt(wav, size)
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(size, receipt["size_bytes"])
        self.assertLess(peak, 4 * 1024 * 1024)

    def test_duplicate_json_keys_are_rejected_in_receipt_and_dataset(self) -> None:
        stage_and_commit_audio_intelligence(self.paths, _dataset())
        receipt_path = artifact_path(self.paths.root, "audio_intelligence_generation")
        receipt_text = receipt_path.read_text(encoding="utf-8")
        receipt_path.write_text(
            receipt_text.replace(
                '"state": "committed"',
                '"state": "forged",\n  "state": "committed"',
                1,
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
            audio_intelligence_binding(self.paths)

        stage_and_commit_audio_intelligence(self.paths, _dataset())
        dataset_path = artifact_path(self.paths.root, "audio_intelligence")
        dataset_text = dataset_path.read_text(encoding="utf-8")
        dataset_path.write_text(
            dataset_text.replace(
                '"schema_id": "audio-timeline/v1"',
                '"schema_id": "forged",\n  "schema_id": "audio-timeline/v1"',
                1,
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
            audio_intelligence_binding(self.paths)

    def test_dataset_without_receipt_is_present_but_invalid(self) -> None:
        dump_json(artifact_path(self.paths.root, "audio_intelligence"), _dataset())
        status = audio_intelligence_status(self.paths)
        self.assertTrue(status["available"])
        self.assertFalse(status["valid"])
        self.assertIn("receipt is missing", status["reasons"][0])


if __name__ == "__main__":
    unittest.main()
