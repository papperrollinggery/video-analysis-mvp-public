from __future__ import annotations

import json
import base64
import re
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from tests.test_audio import write_pcm

from video_analysis_mvp.audio import _stage_and_commit_audio_generation, _style_tags, analyze_audio, profile_music
from video_analysis_mvp.delivery import enforce_profile_output_boundary, write_profile_delivery_package
from video_analysis_mvp.evidence_handoff import build_visualization_dataset, render_codex_handoff
from video_analysis_mvp.paths import ProjectPaths
from video_analysis_mvp.schemas import AnalysisProfile, BeatEvent, CanonicalMediaPackage, Shot, SourceType, dump_json, load_json
from video_analysis_mvp.synthesis import _commit_report_generation, _normalize_shots, build_report, render_html_report
from video_analysis_mvp.visual import (
    _build_scenes,
    _build_shots,
    _build_visual_generation_receipt,
    analyze_visual,
    write_shots_csv,
)
from video_analysis_mvp.vision import (
    ADS_INTERPRETATION_FIELDS,
    MINIMAX_MCP_VERSION,
    OBSERVATION_FIELDS,
    _call_minimax_understand_image,
    _minimax_mcp_command,
    _profile_notes,
    _required_fields,
    annotate_project_with_minimax_mcp,
    analyze_frame,
    analyze_frame_with_minimax_mcp,
    apply_vision_data,
)
from video_analysis_mvp.workspace_api import deliverables_payload, derive_canvas_graph, derive_media_timeline


ADS_ONLY_ARTIFACTS = {
    "remake_brief",
    "branch_board_html",
    "prompt_reverse_engineering",
    "model_prompt_pack",
    "revision_plan",
}

FORBIDDEN_NON_AD_OUTPUT_TERMS = re.compile(
    r"\b(?:ad|ads|advertising|cta|hook|prompt)\b",
    re.IGNORECASE,
)

ADS_ONLY_CSV_FIELDS = (
    "prompt_en",
    "prompt_zh",
    "remake_notes",
    "remake_notes_zh",
)

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _publish_existing_artifacts(
    paths: ProjectPaths,
    media: CanonicalMediaPackage,
    artifacts: dict[str, str],
) -> None:
    dump_json(paths.data / "media_package.json", media)
    shots_path = paths.data / "shots.json"
    shots = (
        [Shot.model_validate(item) for item in load_json(shots_path)]
        if shots_path.exists()
        else [_shot()]
    )
    dump_json(shots_path, shots)
    dump_json(paths.data / "scenes.json", [])
    contact = paths.assets / "contact_sheet.jpg"
    if not contact.exists():
        contact.write_bytes(PNG_1X1)
    dump_json(
        paths.data / "visual_generation.json",
        _build_visual_generation_receipt(paths, shots, []),
    )
    _stage_and_commit_audio_generation(paths, [], [], [])
    current = {
        key: value
        for key, value in artifacts.items()
        if key == "project_manifest" or Path(value).exists()
    }
    current["project_manifest"] = str(paths.manifest)
    _commit_report_generation(paths, media, str(uuid.uuid4()), current)


def _vision_payload(profile: str = "research", **overrides: object) -> dict[str, object]:
    fields = [*OBSERVATION_FIELDS, *(ADS_INTERPRETATION_FIELDS if profile == "ads" else [])]
    payload: dict[str, object] = {field: "none" for field in fields}
    payload.update({"content_summary": "A person enters a room.", "confidence": 0.8})
    payload.update(overrides)
    return payload


def _media(paths: ProjectPaths, profile: AnalysisProfile) -> CanonicalMediaPackage:
    return CanonicalMediaPackage(
        project_id=paths.root.name,
        source_type=SourceType.file,
        source="source.mp4",
        local_master_path=str(paths.ingest / "master.mp4"),
        review_copy_path=str(paths.assets / "review.mp4"),
        audio_path=str(paths.assets / "audio.wav"),
        duration_seconds=3.0,
        frame_rate=24.0,
        resolution="1920x1080",
        aspect_ratio=16 / 9,
        status="analyzed",
        analysis_profile=profile,
        metadata={"delivery_language": "en"},
    )


def _shot(*, annotation_source: str = "human", readiness_status: str = "ready") -> Shot:
    return Shot(
        shot_id="shot_0001",
        shot_no=1,
        start_time=0.0,
        end_time=3.0,
        duration=3.0,
        timecode="00:00-00:03",
        frame_ref="shot_0001_mid.jpg",
        primary_frame_ref="shot_0001_mid.jpg",
        frame_refs=["shot_0001_mid.jpg"],
        boundary_confidence="high",
        story_beat="hook",
        scene_type="hook",
        content_summary="A person enters a room.",
        subject="person",
        action="enters a room",
        shot_scale="wide",
        camera_angle="eye-level",
        camera_motion="static",
        composition="centered",
        remake_notes="Hold the framing.",
        annotation_source=annotation_source,
        visual_confidence=0.9,
        readiness_status=readiness_status,
        confidence=0.9,
    )


class ProfileOutputBoundaryTest(unittest.TestCase):
    def test_minimax_never_reuses_an_unrelated_openclaw_credential(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory) / "project")
            with (
                patch.dict("os.environ", {}, clear=True),
                patch("video_analysis_mvp.vision._load_minimax_config_key", return_value="legacy-key"),
                patch("video_analysis_mvp.vision.analyze_frame_with_minimax_mcp") as analyze,
            ):
                result = annotate_project_with_minimax_mcp(paths)

        self.assertEqual("error", result.status)
        self.assertEqual("No MiniMax key is bound to the selected endpoint", result.error)
        analyze.assert_not_called()

    def test_minimax_transport_pins_the_mcp_package_and_validates_host(self) -> None:
        with patch(
            "video_analysis_mvp.vision._prepare_minimax_mcp",
            return_value=("/tools/minimax-coding-plan-mcp", MINIMAX_MCP_VERSION),
        ):
            self.assertEqual(
                ["/tools/minimax-coding-plan-mcp"],
                _minimax_mcp_command(),
            )

        with patch("video_analysis_mvp.vision.subprocess.Popen") as popen:
            with self.assertRaises(ValueError):
                _call_minimax_understand_image(
                    "/tmp/frame.jpg",
                    "inspect",
                    "test-key",
                    api_host="file:///tmp/provider",
                )
        popen.assert_not_called()

    def test_openai_transport_revalidates_endpoint_at_the_network_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            frame = Path(directory) / "frame.jpg"
            frame.write_bytes(PNG_1X1)
            with patch("video_analysis_mvp.vision.urllib.request.build_opener") as build_opener:
                with self.assertRaises(ValueError):
                    analyze_frame(
                        frame,
                        _shot(annotation_source="machine", readiness_status="blocked"),
                        "test-key",
                        None,
                        "file:///tmp/provider",
                        profile="research",
                    )

        build_opener.assert_not_called()

    def test_both_provider_prompts_exclude_ad_fields_for_non_ads_profiles(self) -> None:
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_: object) -> None:
                return None

            def read(self, _maximum: int = -1) -> bytes:
                return json.dumps(
                    {"choices": [{"message": {"content": json.dumps(_vision_payload())}}]}
                ).encode("utf-8")

        class Opener:
            def __init__(self, response: Response) -> None:
                self.response = response

            def open(self, *_: object, **__: object) -> Response:
                return self.response

        with tempfile.TemporaryDirectory() as directory:
            frame = Path(directory) / "frame.jpg"
            frame.write_bytes(PNG_1X1)
            shot = _shot(annotation_source="machine", readiness_status="blocked")
            opener = Opener(Response())
            with patch("video_analysis_mvp.vision.urllib.request.build_opener", return_value=opener):
                analyze_frame(frame, shot, "test-key", None, None, profile="research")
            # The opener receives the request object; inspect the object captured by a thin wrapper.
            request_payload_holder: list[object] = []
            original_open = opener.open

            def capture(request: object, **kwargs: object) -> Response:
                request_payload_holder.append(request)
                return original_open(request, **kwargs)

            opener.open = capture  # type: ignore[method-assign]
            with patch("video_analysis_mvp.vision.urllib.request.build_opener", return_value=opener):
                analyze_frame(frame, shot, "test-key", None, None, profile="research")
            request_payload = json.loads(request_payload_holder[0].data.decode("utf-8"))  # type: ignore[attr-defined]
            openai_prompt = json.loads(request_payload["messages"][1]["content"][0]["text"])

            with patch(
                "video_analysis_mvp.vision._call_minimax_understand_image",
                return_value=json.dumps(_vision_payload()),
            ) as minimax:
                analyze_frame_with_minimax_mcp(frame, shot, "test-key", profile="festival")
            minimax_prompt = json.loads(minimax.call_args.args[1])

        for prompt in (openai_prompt, minimax_prompt):
            self.assertNotIn("story_beat", prompt["required_json_fields"])
            self.assertNotIn("remake_notes", prompt["required_json_fields"])
            self.assertNotIn("prompt_en", prompt["required_json_fields"])
            self.assertIn("content_summary", prompt["required_json_fields"])

    def test_non_ads_vision_payload_cannot_reintroduce_ad_semantics(self) -> None:
        shot = _shot(annotation_source="machine", readiness_status="blocked")
        with self.assertRaisesRegex(ValueError, "unexpected fields"):
            apply_vision_data(
                shot,
                {
                    **_vision_payload(confidence=0.9),
                    "story_beat": "cta",
                    "scene_type": "product_reveal",
                    "remake_notes": "Add a logo.",
                    "prompt_en": "Generate a product ad.",
                },
                profile="research",
            )

        self.assertEqual("machine", shot.annotation_source)
        self.assertEqual("hook", shot.story_beat)
        self.assertEqual("hook", shot.scene_type)
        self.assertEqual("Hold the framing.", shot.remake_notes)
        self.assertNotIn("story_beat", _required_fields("research"))
        self.assertNotIn("remake_notes", _required_fields("research"))
        self.assertNotIn("prompt_en", _required_fields("research"))
        self.assertIn("Do not use marketing roles", _profile_notes("research"))

    def test_ads_vision_payload_keeps_creative_interpretation_fields(self) -> None:
        shot = _shot(annotation_source="machine", readiness_status="blocked")
        apply_vision_data(
            shot,
            _vision_payload(
                "ads",
                story_beat="hook",
                scene_type="product_reveal",
                remake_notes="Hold the pack shot.",
                prompt_en="A product reveal.",
                confidence=0.9,
            ),
            profile="ads",
        )

        self.assertEqual("hook", shot.story_beat)
        self.assertEqual("Hold the pack shot.", shot.remake_notes)
        self.assertEqual("A product reveal.", shot.prompt_en)
        self.assertIn("story_beat", _required_fields("ads"))
        self.assertIn("prompt_en", _required_fields("ads"))

    def test_visual_stage_marks_profile_specific_story_positions_as_heuristic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            research_paths = ProjectPaths(root / "research")
            ads_paths = ProjectPaths(root / "ads")
            research = _build_shots(
                _media(research_paths, AnalysisProfile.research),
                [(0.0, 1.5, "high"), (1.5, 3.0, "high")],
                "scene_detection",
            )
            ads = _build_shots(
                _media(ads_paths, AnalysisProfile.ads),
                [(0.0, 1.5, "high"), (1.5, 3.0, "high")],
                "scene_detection",
            )

            self.assertEqual("heuristic_unverified:opening_sequence", research[0].story_beat)
            self.assertEqual("heuristic_unverified:closing_sequence", research[-1].story_beat)
            self.assertNotIn("hook", research[0].story_beat)
            self.assertEqual("heuristic_unverified:hook", ads[0].story_beat)
            self.assertEqual("heuristic_unverified:cta", ads[-1].story_beat)

    def test_scene_and_music_labels_are_profile_specific(self) -> None:
        forbidden_non_ads_terms = re.compile(r"\b(?:hook|cta|ad|advertising|remake|prompt)\b", re.IGNORECASE)
        segments = [(float(index), float(index + 1), "high") for index in range(16)]

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for profile in AnalysisProfile:
                with self.subTest(profile=profile.value):
                    paths = ProjectPaths(root / profile.value)
                    shots = _build_shots(_media(paths, profile), segments, "scene_detection")
                    scenes = _build_scenes(shots, profile)
                    style_tags = _style_tags("high", "fast", profile)
                    labels = " ".join([*(scene.scene_function for scene in scenes), *style_tags])

                    if profile is AnalysisProfile.ads:
                        self.assertIn("hook", labels.lower())
                        self.assertIn("cta", labels.lower())
                        self.assertIn("ad-friendly", style_tags)
                    else:
                        self.assertIsNone(forbidden_non_ads_terms.search(labels), labels)

            default_shots = _build_shots(
                _media(ProjectPaths(root / "default"), AnalysisProfile.research),
                segments,
                "scene_detection",
            )
            default_scene_labels = " ".join(scene.scene_function for scene in _build_scenes(default_shots))
            write_pcm(root / "profile.wav", 1.0, lambda _t: 0.1)
            default_music = profile_music(
                1.0,
                [BeatEvent(time=0.25, strength=1.0), BeatEvent(time=0.75, strength=1.0)],
                root / "profile.wav",
            )
            default_labels = " ".join([default_scene_labels, *default_music[0].style_tags])
            self.assertIsNone(forbidden_non_ads_terms.search(default_labels), default_labels)

    def test_analysis_stages_forward_the_media_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            paths = ProjectPaths(Path(temporary_directory) / "project")
            paths.ensure()
            media = _media(paths, AnalysisProfile.festival)

            def write_frames(_video: Path, directory: Path, shots: list[Shot], *, frame_rate: float | None = None) -> None:
                self.assertEqual(media.frame_rate, frame_rate)
                for shot in shots:
                    for name in shot.frame_refs:
                        (directory / name).write_bytes(b"frame")

            def write_contact(_video: Path, output: Path, _interval: float) -> None:
                output.write_bytes(b"contact")

            with (
                patch("video_analysis_mvp.visual._detect_shot_segments", return_value=([(0.0, 3.0, "high")], "test")),
                patch("video_analysis_mvp.visual._extract_shot_frames", side_effect=write_frames),
                patch("video_analysis_mvp.visual._build_contact_sheet", side_effect=write_contact),
                patch("video_analysis_mvp.visual._build_scenes", return_value=[]) as build_scenes,
            ):
                analyze_visual(media, paths)
            self.assertIs(build_scenes.call_args.args[1], AnalysisProfile.festival)

            write_pcm(paths.assets / "audio.wav", media.duration_seconds)
            dump_json(paths.data / "media_package.json", media)
            with (
                patch("video_analysis_mvp.audio.transcribe_audio", return_value=[]),
                patch("video_analysis_mvp.audio.detect_beats", return_value=[]),
                patch("video_analysis_mvp.audio.profile_music", return_value=[]) as profile_music,
            ):
                analyze_audio(media, paths)
            self.assertIs(profile_music.call_args.args[3], AnalysisProfile.festival)

    def test_non_ads_profiles_omit_ad_branch_and_remake_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            for profile in (
                AnalysisProfile.research,
                AnalysisProfile.streaming,
                AnalysisProfile.shortform,
                AnalysisProfile.festival,
            ):
                with self.subTest(profile=profile.value):
                    paths = ProjectPaths(Path(temporary_directory) / profile.value)
                    paths.ensure()
                    (paths.keyframes / "shot_0001_mid.jpg").write_bytes(b"frame")
                    media = _media(paths, profile)
                    shot = _shot()
                    shot.story_beat = "opening_sequence"
                    shot.scene_type = "opening_sequence"
                    shot.prompt_en = "Legacy prompt text must not survive this profile boundary."
                    report = build_report(media, [shot], [], [], [], [], paths)
                    artifacts = write_profile_delivery_package(report, media, [shot], [], [], [], [], paths)
                    write_shots_csv(paths.reports / "shot_breakdown.csv", [shot], profile)
                    report.artifacts.update(artifacts)
                    render_html_report(report, media, [shot], [], [], [], [], paths.reports / "report.html")
                    dump_json(paths.data / "media_package.json", media)
                    dump_json(paths.data / "analysis_report.json", report)
                    _publish_existing_artifacts(paths, media, report.artifacts)
                    deliverables = deliverables_payload(paths.root.parent, paths.root)
                    media_payload = derive_media_timeline(paths.root.parent, paths.root)
                    canvas_payload = derive_canvas_graph(paths.root.parent, paths.root)
                    deliverable_by_id = {
                        item["id"]: item for item in deliverables["artifacts"]
                    }

                    self.assertTrue(ADS_ONLY_ARTIFACTS.isdisjoint(report.artifacts))
                    self.assertTrue(ADS_ONLY_ARTIFACTS.isdisjoint(artifacts))
                    self.assertEqual(
                        str(paths.reports / "profile_analysis.html"),
                        report.artifacts["profile_analysis_html"],
                    )
                    self.assertNotIn("ad_breakdown_html", report.artifacts)
                    self.assertIn("profile_analysis_html", deliverable_by_id)
                    self.assertNotIn("ad_breakdown_html", deliverable_by_id)
                    self.assertTrue(deliverable_by_id["profile_analysis_html"]["present"])
                    self.assertEqual("blocked", deliverable_by_id["profile_analysis_html"]["readiness_status"])
                    self.assertIsNone(deliverable_by_id["profile_analysis_html"]["url"])
                    self.assertIn(
                        "/deliverables/profile_analysis_html/preview",
                        deliverable_by_id["profile_analysis_html"]["preview_url"],
                    )
                    for csv_name in ("shot_breakdown.csv", "shot_list.csv", "shot_table.csv"):
                        csv_bytes = (paths.reports / csv_name).read_bytes()
                        csv_header = csv_bytes.splitlines()[0].decode("utf-8")
                        for field in ADS_ONLY_CSV_FIELDS:
                            self.assertNotIn(field, csv_header)
                            self.assertNotIn(field.encode("utf-8"), csv_bytes)
                        self.assertIsNone(
                            FORBIDDEN_NON_AD_OUTPUT_TERMS.search(
                                csv_bytes.decode("utf-8").replace("_", " ")
                            ),
                            f"{profile.value} CSV leaked profile-specific language: {csv_name}",
                        )
                    for filename in (
                        "remake_brief.md",
                        "branch_board.html",
                        "prompt_reverse_engineering.md",
                        "model_prompt_pack.json",
                        "revision_plan.md",
                    ):
                        self.assertFalse((paths.reports / filename).exists())

                    lineage = __import__("json").loads((paths.data / "lineage.json").read_text(encoding="utf-8"))
                    self.assertEqual(lineage["branches"], [])
                    storyboard = (paths.reports / "storyboard.html").read_text(encoding="utf-8").lower()
                    breakdown = (paths.reports / "profile_analysis.html").read_text(encoding="utf-8").lower()
                    self.assertNotIn("remake", storyboard)
                    self.assertNotIn("branch_board", breakdown)
                    self.assertNotIn("remake_brief", breakdown)
                    self.assertIn("evidence export blocked", breakdown)
                    self.assertIn("current versioned media receipt is required", breakdown)
                    general_report = (paths.reports / "report.html").read_text(encoding="utf-8")
                    self.assertNotIn("Generation Interpretation", general_report)
                    self.assertNotIn("生成解释", general_report)
                    self.assertNotIn("generate prompt", general_report)

                    serialized_outputs = [
                        json.dumps(report.artifacts, ensure_ascii=False),
                        json.dumps(artifacts, ensure_ascii=False),
                        json.dumps(deliverables, ensure_ascii=False),
                        json.dumps(media_payload, ensure_ascii=False),
                        json.dumps(canvas_payload, ensure_ascii=False),
                    ]
                    output_files = [
                        *sorted(path for path in paths.reports.rglob("*") if path.is_file()),
                        *sorted(path for path in paths.data.rglob("*") if path.is_file()),
                        paths.manifest,
                    ]
                    for output_path in output_files:
                        serialized_outputs.append(output_path.relative_to(paths.root).as_posix())
                        serialized_outputs.append(output_path.read_text(encoding="utf-8", errors="ignore"))
                    for serialized_output in serialized_outputs:
                        self.assertIsNone(
                            FORBIDDEN_NON_AD_OUTPUT_TERMS.search(serialized_output),
                            f"{profile.value} output leaked profile-specific language: {serialized_output[:240]}",
                        )

    def test_non_ads_boundary_replaces_campaign_story_roles_before_serialization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            paths = ProjectPaths(Path(temporary_directory) / "research")
            shot = _shot()
            shot.prompt_en = "Generate an ad prompt."
            shot.prompt_zh = "生成广告提示词。"

            enforce_profile_output_boundary(_media(paths, AnalysisProfile.research), [shot])

            self.assertEqual("heuristic_unverified:opening_sequence", shot.story_beat)
            self.assertEqual(shot.story_beat, shot.scene_type)
            self.assertEqual("", shot.prompt_en)
            self.assertEqual("", shot.prompt_zh)
            self.assertEqual("", shot.remake_notes)

    def test_ads_profile_keeps_creative_outputs_but_marks_them_unverified(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            paths = ProjectPaths(Path(temporary_directory) / "ads")
            paths.ensure()
            (paths.keyframes / "shot_0001_mid.jpg").write_bytes(b"frame")
            media = _media(paths, AnalysisProfile.ads)
            shot = _shot()
            report = build_report(media, [shot], [], [], [], [], paths)
            artifacts = write_profile_delivery_package(report, media, [shot], [], [], [], [], paths)

            self.assertTrue(ADS_ONLY_ARTIFACTS.issubset(report.artifacts))
            self.assertTrue(ADS_ONLY_ARTIFACTS.issubset(artifacts))
            self.assertEqual(
                str(paths.reports / "profile_analysis.html"),
                artifacts["profile_analysis_html"],
            )
            self.assertNotIn("ad_breakdown_html", artifacts)
            with (paths.reports / "shot_list.csv").open(encoding="utf-8") as handle:
                ads_header = handle.readline()
            for field in ADS_ONLY_CSV_FIELDS:
                self.assertIn(field, ads_header)
            branch_board = (paths.reports / "branch_board.html").read_text(encoding="utf-8")
            remake_brief = (paths.reports / "remake_brief.md").read_text(encoding="utf-8")
            prompt_pack = (paths.reports / "model_prompt_pack.json").read_text(encoding="utf-8")
            self.assertIn("heuristic / unverified interpretation", branch_board)
            self.assertIn("Creative heuristic / unverified interpretation", remake_brief)
            self.assertIn('"verification_status": "unverified"', prompt_pack)

    def test_ads_non_ads_project_reuse_fails_before_existing_outputs_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            paths = ProjectPaths(Path(temporary_directory) / "same-project")
            paths.ensure()
            (paths.keyframes / "shot_0001_mid.jpg").write_bytes(PNG_1X1)
            ads_media = _media(paths, AnalysisProfile.ads)
            ads_shot = _shot()
            dump_json(paths.data / "media_package.json", ads_media)
            report = build_report(ads_media, [ads_shot], [], [], [], [], paths)
            artifacts = write_profile_delivery_package(report, ads_media, [ads_shot], [], [], [], [], paths)
            report.artifacts.update(artifacts)
            _publish_existing_artifacts(paths, ads_media, report.artifacts)
            protected = {
                name: (paths.reports / name).read_bytes()
                for name in (
                    "remake_brief.md",
                    "branch_board.html",
                    "prompt_reverse_engineering.md",
                    "model_prompt_pack.json",
                    "revision_plan.md",
                )
            }
            research_media = _media(paths, AnalysisProfile.research)
            research_shot = Shot.model_validate(ads_shot.model_dump(mode="json"))
            research_report = build_report(research_media, [research_shot], [], [], [], [], paths)
            with self.assertRaisesRegex(ValueError, "Project profile boundary mismatch"):
                write_profile_delivery_package(
                    research_report,
                    research_media,
                    [research_shot],
                    [],
                    [],
                    [],
                    [],
                    paths,
                )
            self.assertEqual("ads", load_json(paths.manifest)["profile"])
            for name, before in protected.items():
                self.assertEqual(before, (paths.reports / name).read_bytes(), name)

    def test_machine_story_positions_are_profile_specific_and_explicitly_unverified(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            research_paths = ProjectPaths(Path(temporary_directory) / "research")
            ads_paths = ProjectPaths(Path(temporary_directory) / "ads")
            research_paths.ensure()
            ads_paths.ensure()
            research_shot = _shot(annotation_source="machine", readiness_status="blocked")
            ads_shot = _shot(annotation_source="machine", readiness_status="blocked")
            human_shot_with_missing_beat = _shot(annotation_source="human", readiness_status="ready")
            human_shot_with_missing_beat.story_beat = ""
            human_shot_with_missing_beat.scene_type = ""

            _normalize_shots(_media(research_paths, AnalysisProfile.research), [research_shot])
            _normalize_shots(_media(ads_paths, AnalysisProfile.ads), [ads_shot])
            _normalize_shots(_media(research_paths, AnalysisProfile.research), [human_shot_with_missing_beat])

            self.assertEqual(research_shot.story_beat, "heuristic_unverified:opening_sequence")
            self.assertEqual(research_shot.scene_type, "heuristic_unverified:opening_sequence")
            self.assertNotIn("hook", research_shot.story_beat)
            self.assertEqual(ads_shot.story_beat, "heuristic_unverified:hook")
            self.assertEqual(human_shot_with_missing_beat.story_beat, "heuristic_unverified:opening_sequence")
            self.assertIn("heuristic interpretations are unverified", research_shot.review_notes)

    def test_evidence_handoff_separates_media_evidence_from_machine_interpretation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            paths = ProjectPaths(Path(temporary_directory) / "research")
            paths.ensure()
            (paths.keyframes / "shot_0001_mid.jpg").write_bytes(b"frame")
            shot = _shot(annotation_source="machine", readiness_status="blocked")
            _normalize_shots(_media(paths, AnalysisProfile.research), [shot])
            dataset = build_visualization_dataset(
                _media(paths, AnalysisProfile.research),
                [shot],
                {"status": "blocked", "professional_export_allowed": False, "shot_count": 1, "reasons": []},
                {"schema_version": 1, "nodes": [], "edges": [], "commits": [], "branches": []},
                paths,
            )

            annotation = dataset["shots"][0]["annotation"]
            self.assertEqual(annotation["claim_type"], "interpretation")
            self.assertTrue(annotation["heuristic"])
            self.assertEqual(annotation["verification_status"], "unverified")
            self.assertEqual(dataset["shots"][0]["story_beat_claim"]["claim_type"], "interpretation")
            self.assertIn("Interpretation fields are not source evidence", dataset["field_semantics"]["rule"])
            handoff = render_codex_handoff(dataset)
            self.assertIn("Trust boundary — read before data", handoff)
            self.assertIn("intentionally excludes transcript, provider output, and raw shot narratives", handoff)
            self.assertIn("heuristic interpretation / unverified", handoff)
            self.assertIn("untrusted evidence data", handoff)


if __name__ == "__main__":
    unittest.main()
