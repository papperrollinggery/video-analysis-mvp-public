from __future__ import annotations

import json
import html
from pathlib import Path
from typing import Any

from .artifacts import artifact_path
from .evidence_handoff import write_evidence_handoff
from .audio_synthesis import audio_presentation_values, build_project_audio_associations
from .paths import ProjectPaths
from .safe_io import advisory_file_lock, atomic_write_text
from .readiness import write_readiness
from .schemas import (
    AnalysisReport,
    BeatEvent,
    CanonicalMediaPackage,
    MusicProfile,
    Scene,
    Shot,
    TranscriptSegment,
    dump_json,
    load_json,
)
from .utils import format_clock
from .visual import write_shots_csv


BRANCHES = [
    {
        "id": "branch_safer",
        "name": "safer",
        "title": "Safer",
        "goal": "Keep the current structure and reduce execution risk.",
        "goal_zh": "保留原结构，只降低执行风险。",
        "scenario": "Use for proven ads that need cleaner pacing, clearer CTA, or lower production risk.",
        "keeper": "Best when the current ad already works and only needs cleaner pacing.",
        "risk": "May not create enough creative lift if the original hook is weak.",
        "cost": "Low",
    },
    {
        "id": "branch_hook",
        "name": "stronger_hook",
        "title": "Stronger Hook",
        "goal": "Move the strongest pain point or outcome into the first 3 seconds.",
        "goal_zh": "把最强痛点或结果提前到前 3 秒。",
        "scenario": "Use when retention drops early or the product promise arrives too late.",
        "keeper": "Best when the ad loses attention before the product is understood.",
        "risk": "A harder hook can feel less premium if the execution is too aggressive.",
        "cost": "Medium",
    },
    {
        "id": "branch_premium",
        "name": "premium_style",
        "title": "Premium Style",
        "goal": "Raise visual trust while preserving the same sales argument.",
        "goal_zh": "提升画面可信度，同时保留原卖点结构。",
        "scenario": "Use when the offer is credible but the current visual system feels low-trust.",
        "keeper": "Best when the offer is strong but the creative feels disposable.",
        "risk": "Over-polish can reduce native short-form believability.",
        "cost": "Medium",
    },
]

NON_NEUTRAL_STORY_BEATS = {
    "hook",
    "problem",
    "demo",
    "proof",
    "payoff",
    "cta",
    "product_reveal",
    "product_title",
    "brand_payoff",
}


def _longest_backtick_run(value: str) -> int:
    longest = 0
    current = 0
    for character in value:
        if character == "`":
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def _markdown_inline(value: object, *, fallback: str = "unknown") -> str:
    """Render untrusted text as a single inert Markdown code span."""
    text = " ".join(str(value).replace("\x00", "\ufffd").split()) or fallback
    fence = "`" * max(1, _longest_backtick_run(text) + 1)
    return f"{fence} {text} {fence}"


def _markdown_code_block(value: object, *, language: str = "text") -> str:
    """Render untrusted multiline text inside a fence it cannot close."""
    text = str(value).replace("\x00", "\ufffd").rstrip("\n")
    fence = "`" * max(3, _longest_backtick_run(text) + 1)
    info = language if language in {"json", "text"} else ""
    return f"{fence}{info}\n{text}\n{fence}"


def enforce_profile_output_boundary(media: CanonicalMediaPackage, shots: list[Shot]) -> None:
    """Remove campaign-only interpretation fields before a neutral-profile export."""
    if _is_ads_profile(media):
        return
    count = len(shots)
    for index, shot in enumerate(shots, start=1):
        original_beat = shot.story_beat
        beat = original_beat.removeprefix("heuristic_unverified:").strip().lower()
        if beat in NON_NEUTRAL_STORY_BEATS:
            shot.story_beat = f"heuristic_unverified:{_neutral_story_position(index, count)}"
        scene_type = shot.scene_type.removeprefix("heuristic_unverified:").strip().lower()
        if scene_type in NON_NEUTRAL_STORY_BEATS or shot.scene_type == original_beat:
            shot.scene_type = shot.story_beat
        shot.prompt_en = ""
        shot.prompt_zh = ""
        shot.remake_notes = ""
        shot.remake_notes_zh = ""


def enforce_project_profile_boundary(paths: ProjectPaths, media: CanonicalMediaPackage) -> None:
    """Reject ads/non-ads reuse before any delivery artifact can be replaced.

    Ads projects intentionally produce campaign-only files that are retained as
    user evidence.  Reusing that project id for a neutral profile (or the
    reverse) would make those retained bytes indistinguishable from current
    output, so the safe operation is a new project id.
    """
    requested = _profile_name(media.analysis_profile)
    observed: list[tuple[str, str]] = [("requested media", requested)]
    for label, path, key in (
        ("project manifest", paths.manifest, "profile"),
        ("media package", paths.data / "media_package.json", "analysis_profile"),
    ):
        if not path.exists():
            continue
        try:
            payload = load_json(path)
            if type(payload) is not dict or type(payload.get(key)) is not str:
                raise ValueError("profile field is missing")
            observed.append((label, _profile_name(payload[key])))
        except Exception as exc:
            raise ValueError(f"Cannot verify existing {label} profile; use a new project id") from exc
    categories = {"ads" if profile == "ads" else "non_ads" for _label, profile in observed}
    if len(categories) > 1:
        detail = ", ".join(f"{label}={profile}" for label, profile in observed)
        raise ValueError(
            "Project profile boundary mismatch: ads and non-ads evidence cannot share one project id "
            f"({detail}); use a new project id"
        )


def _neutral_story_position(index: int, count: int) -> str:
    if index == 1:
        return "opening_sequence"
    ratio = (index - 1) / max(count - 1, 1)
    if ratio < 0.25:
        return "early_sequence"
    if ratio < 0.65:
        return "middle_sequence"
    if index == count:
        return "closing_sequence"
    return "late_sequence"


def write_profile_delivery_package(
    report: AnalysisReport,
    media: CanonicalMediaPackage,
    shots: list[Shot],
    scenes: list[Scene],
    transcript: list[TranscriptSegment],
    beats: list[BeatEvent],
    music: list[MusicProfile],
    paths: ProjectPaths,
    *,
    _shots_lock_held: bool = False,
    audio_associations: dict[str, Any] | None = None,
) -> dict[str, str]:
    if _shots_lock_held:
        return _write_profile_delivery_package(report, media, shots, scenes, transcript, beats, music, paths, audio_associations=audio_associations)
    with advisory_file_lock(paths.data / ".shots.lock", root=paths.root):
        _require_current_shot_snapshot(paths, shots)
        return _write_profile_delivery_package(report, media, shots, scenes, transcript, beats, music, paths, audio_associations=audio_associations)


def _write_profile_delivery_package(
    report: AnalysisReport,
    media: CanonicalMediaPackage,
    shots: list[Shot],
    scenes: list[Scene],
    transcript: list[TranscriptSegment],
    beats: list[BeatEvent],
    music: list[MusicProfile],
    paths: ProjectPaths,
    *,
    audio_associations: dict[str, Any] | None = None,
) -> dict[str, str]:
    if audio_associations is None:
        audio_associations = build_project_audio_associations(paths, media, shots, scenes)
    if audio_associations["available"]:
        transcript, music = audio_presentation_values(audio_associations)
    enforce_project_profile_boundary(paths, media)
    enforce_profile_output_boundary(media, shots)
    is_ads = _is_ads_profile(media)
    storyboard = artifact_path(paths.root, "storyboard_html")
    shot_list = artifact_path(paths.root, "shot_list_csv")
    profile_analysis = artifact_path(paths.root, "profile_analysis_html")
    shot_table = artifact_path(paths.root, "shot_table_csv")
    remake_brief = artifact_path(paths.root, "remake_brief")
    branch_board = artifact_path(paths.root, "branch_board_html")
    prompt_reverse = artifact_path(paths.root, "prompt_reverse_engineering")
    model_prompt_pack = artifact_path(paths.root, "model_prompt_pack")
    revision_plan = artifact_path(paths.root, "revision_plan")
    lineage_path = artifact_path(paths.root, "lineage_json")
    readiness_path = artifact_path(paths.root, "readiness_json")

    dump_json(paths.data / "shots.json", shots)
    readiness = write_readiness(readiness_path, shots, workspace_root=paths.root.parent)
    render_storyboard(report, media, shots, transcript, beats, music, readiness, storyboard)
    write_shots_csv(shot_list, shots, media.analysis_profile)
    write_shots_csv(shot_table, shots, media.analysis_profile)
    lineage = build_lineage(media, shots, readiness)
    lineage["readiness"] = readiness
    dump_json(lineage_path, lineage)
    render_profile_analysis(report, media, shots, scenes, transcript, beats, music, readiness, profile_analysis)
    if is_ads:
        render_branch_board(report, media, shots, branch_board)
        write_remake_brief(report, media, shots, transcript, music, remake_brief)
        write_prompt_reverse_engineering(media, shots, prompt_reverse)
        write_model_prompt_pack(media, shots, model_prompt_pack)
        write_revision_plan(media, shots, revision_plan)

    evidence_artifacts = write_evidence_handoff(media, shots, readiness, lineage, paths, scenes=scenes, audio_associations=audio_associations)

    artifacts = {
        "storyboard_html": str(storyboard),
        "shot_list_csv": str(shot_list),
        "profile_analysis_html": str(profile_analysis),
        "shot_table_csv": str(shot_table),
        "lineage_json": str(lineage_path),
        "readiness_json": str(readiness_path),
    }
    if is_ads:
        artifacts.update(
            {
                "remake_brief": str(remake_brief),
                "branch_board_html": str(branch_board),
                "prompt_reverse_engineering": str(prompt_reverse),
                "model_prompt_pack": str(model_prompt_pack),
                "revision_plan": str(revision_plan),
            }
        )
    artifacts.update(evidence_artifacts)
    return artifacts


def _require_current_shot_snapshot(paths: ProjectPaths, shots: list[Shot]) -> None:
    path = paths.data / "shots.json"
    if not path.exists():
        return
    try:
        current = [Shot.model_validate(item) for item in load_json(path)]
    except Exception as exc:
        raise ValueError("Current shots receipt is invalid; delivery was not written") from exc
    current_payload = [shot.model_dump(mode="json") for shot in current]
    requested_payload = [shot.model_dump(mode="json") for shot in shots]
    if current_payload != requested_payload:
        raise RuntimeError("shots.json changed before delivery; reload the project and retry")


def build_lineage(
    media: CanonicalMediaPackage,
    shots: list[Shot],
    readiness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    nodes: list[dict[str, Any]] = [
        {
            "id": "asset_001",
            "type": "source_asset",
            "title": Path(media.source).name or media.project_id,
            "status": "input",
        }
    ]
    edges: list[dict[str, str]] = []
    shot_node_ids: list[str] = []
    readiness_by_shot = {
        str(item.get("shot_id")): item
        for item in (readiness or {}).get("shot_results", [])
        if isinstance(item, dict) and item.get("shot_id")
    }
    for shot in shots:
        shot_node_id = f"node_{shot.shot_id}"
        shot_node_ids.append(shot_node_id)
        gate = readiness_by_shot.get(shot.shot_id, {})
        annotation_state = str(gate.get("annotation_state") or "unverified")
        nodes.append(
            {
                "id": shot_node_id,
                "type": "storyboard_frame",
                "title": f"{shot.timecode or format_clock(shot.start_time)} {shot.content_summary or shot.visual_description}",
                "status": annotation_state,
                "shot_id": shot.shot_id,
                "frame_ref": shot.primary_frame_ref or shot.frame_ref,
                "frame_refs": shot.frame_refs,
                "story_beat": shot.story_beat,
                "story_beat_claim_type": "interpretation",
                "story_beat_verification": _annotation_verification(shot),
                "readiness_status": shot.readiness_status,
                "annotation_state": annotation_state,
                "provider_receipt_verified": gate.get("provider_receipt_verified") is True,
                "human_assertion": gate.get("human_assertion") is True,
                "confidence": shot.confidence,
                "confidence_is_review": False,
            }
        )
        edges.append({"from": "asset_001", "to": shot_node_id, "type": "derived_from"})

    commits = [
        {
            "id": "commit_001",
            "parentIds": [],
            "nodeIds": ["asset_001", *shot_node_ids],
            "message": "initial evidence extraction",
            "cost": 0.0,
        }
    ]
    branches = []
    for index, branch in enumerate(BRANCHES if _is_ads_profile(media) else [], start=1):
        prompt_id = f"prompt_{index:03d}"
        commit_id = f"commit_{index + 1:03d}"
        nodes.append(
            {
                "id": prompt_id,
                "type": "prompt",
                "title": f"{branch['name']} revision prompt",
                "status": "draft",
                "branch": branch["name"],
                "claim_type": "creative_interpretation",
                "heuristic": True,
                "verification_status": "unverified",
            }
        )
        if shot_node_ids:
            edges.append({"from": shot_node_ids[0], "to": prompt_id, "type": "uses_reference"})
        commits.append(
            {
                "id": commit_id,
                "parentIds": ["commit_001"],
                "nodeIds": [prompt_id],
                "message": f"draft {branch['name']} branch",
                "cost": 0.0,
            }
        )
        branches.append(
            {
                "id": branch["id"],
                "name": branch["name"],
                "headCommit": commit_id,
                "keeper": False,
            }
        )

    return {
        "schema_version": 1,
        "project_id": media.project_id,
        "nodes": nodes,
        "edges": edges,
        "commits": commits,
        "branches": branches,
    }


def render_storyboard(
    report: AnalysisReport,
    media: CanonicalMediaPackage,
    shots: list[Shot],
    transcript: list[TranscriptSegment],
    beats: list[BeatEvent],
    music: list[MusicProfile],
    readiness: dict[str, Any],
    path: Path,
) -> None:
    css = _storyboard_css()
    lang = _delivery_lang(media)
    include_remake = _is_ads_profile(media)
    rows = "".join(_storyboard_card(shot, lang, include_remake=include_remake) for shot in shots)
    table_rows = "".join(_shot_list_row(shot, lang, include_remake=include_remake) for shot in shots)
    reasons = readiness.get("reasons") or []
    readiness_items = "".join(f"<li>{html.escape(_gate_reason_text(str(item), lang))}</li>" for item in reasons) or (
        "<li>专业门禁已通过</li>" if lang == "zh" else "<li>professional readiness passed</li>"
    )
    status = str(readiness.get("status", "blocked"))
    status_class = "ready" if status == "ready" else "blocked"
    version = _version_label(lang)
    deck = (
        "逐镜头证据工作台。时码和媒体文件是证据；内容、叙事功能与创意建议属于带状态的解释。"
        if lang == "zh"
        else "Shot-level evidence workbench. Timecodes and media files are evidence; descriptions, narrative functions, and creative suggestions are stateful interpretations."
    )
    shot_list_title = "镜头表" if lang == "zh" else "Shot List"
    shot_list_deck = "用于逐镜头复核；解释字段必须结合来源与验证状态阅读。" if lang == "zh" else "A shot review table; interpretive fields must be read with their source and verification state."
    headers = ["镜头", "叙事解释", "时码", "标注内容", "主体 / 动作", "镜头语言", "声音"] if lang == "zh" else ["Shot", "Narrative interpretation", "Timecode", "Annotated content", "Subject / Action", "Camera", "Sound"]
    if include_remake:
        headers.append("创意复拍解释（未验证）" if lang == "zh" else "Creative remake interpretation (unverified)")
    headers.append("状态" if lang == "zh" else "Status")
    header_html = "".join(f"<th>{html.escape(item)}</th>" for item in headers)
    body = f"""<!doctype html>
<html lang="{'zh-CN' if lang == 'zh' else 'en'}">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(media.project_id)} Storyboard</title><style>{css}</style></head>
<body>
<main>
  <header>
    <div>
      <p class="eyebrow">Professional Shot Breakdown Workbench</p>
      <h1>{html.escape(media.project_id)}</h1>
      <div class="version">{html.escape(version)}</div>
      <p class="lede">{html.escape(deck)}</p>
    </div>
    <aside class="gate {status_class}">
      <b>{html.escape(_status_text(status, lang))}</b>
      <span>{'专业导出' if lang == 'zh' else 'professional export'}</span>
      <ul>{readiness_items}</ul>
    </aside>
  </header>
  <section class="metrics">
    <span>{len(shots)} {'个镜头' if lang == 'zh' else 'shots'}</span>
    <span>{html.escape(format_clock(media.duration_seconds))}</span>
    <span>{html.escape(media.resolution)}</span>
    <span>{'平均视觉置信度' if lang == 'zh' else 'avg vision'} {html.escape(str(readiness.get("average_visual_confidence", 0.0)))}</span>
  </section>
  <section class="storyboard">{rows}</section>
  <section class="shotlist">
    <div class="sectionHead"><h2>{html.escape(shot_list_title)}</h2><p>{html.escape(shot_list_deck)}</p></div>
    <div class="tableWrap"><table><thead><tr>{header_html}</tr></thead><tbody>{table_rows}</tbody></table></div>
  </section>
</main>
</body></html>"""
    atomic_write_text(path, body)


def render_profile_analysis(
    report: AnalysisReport,
    media: CanonicalMediaPackage,
    shots: list[Shot],
    scenes: list[Scene],
    transcript: list[TranscriptSegment],
    beats: list[BeatEvent],
    music: list[MusicProfile],
    readiness: dict[str, Any],
    path: Path,
) -> None:
    if not readiness.get("professional_export_allowed"):
        render_blocked_professional_export(media, shots, readiness, path)
        return
    lang = _delivery_lang(media)
    first_window = [shot for shot in shots if shot.start_time < 5][:3] or shots[:1]
    first_window_rows = "".join(_compact_shot_row(shot, lang) for shot in first_window)
    top_rows = "".join(_compact_shot_row(shot, lang) for shot in shots[:12])
    music_state = music[0].energy_level if music else "unknown"
    tempo = music[0].tempo_bucket if music else "unknown"
    css = _delivery_css()
    version = _version_label(lang)
    is_ads = _is_ads_profile(media)
    title = "镜头分析报告" if lang == "zh" else "Shot Analysis"
    lede = (
        "把视频拆成可回看、可核对的逐镜头证据。当前版本已通过 readiness 门禁；描述与叙事结论仍属于解释，必须对照原片。"
        if lang == "zh"
        else "A shot-level evidence package traceable to the source. This version passed its readiness gate; descriptions and narrative conclusions remain interpretations that must be checked against the video."
    )
    if is_ads:
        opening_title = "广告开场启发式解释（未验证）" if lang == "zh" else "Ad-opening heuristic interpretation (unverified)"
        opening_text = _hook_diagnosis(shots, transcript, beats, lang)
    else:
        opening_title = "开场序列解释（未验证）" if lang == "zh" else "Opening-sequence interpretation (unverified)"
        opening_text = _opening_sequence_interpretation(shots, transcript, beats, lang)
    terms_title = "证据包说明" if lang == "zh" else "Evidence Package"
    terms_text = "本地证据包包含分镜故事板、标准镜头表、readiness、lineage 与可复用的结构化数据；叙事分类和创意建议属于解释，不是源证据。" if lang == "zh" else "The local package includes a storyboard, shot table, readiness, lineage, and reusable structured data. Narrative labels and creative suggestions are interpretations, not source evidence."
    audio_title = "声音 / 节奏" if lang == "zh" else "Audio / Rhythm"
    files_title = "交付文件" if lang == "zh" else "Delivery Files"
    teardown_title = "逐镜头拆解" if lang == "zh" else "Beat-by-Beat Teardown"
    takeaway_title = "解释与复核备注（未验证）" if lang == "zh" else "Interpretations / review notes (unverified)"
    ready_title = "专业门禁" if lang == "zh" else "Professional Readiness"
    ready_text = "门禁已通过。以 shots.json、storyboard.html 和原片时码作为逐镜头证据主线；lineage.json 记录派生关系。" if lang == "zh" else "Gate passed. Use shots.json, storyboard.html, and source timecodes as the shot-evidence spine; lineage.json records derivation."
    files = ["storyboard.html", "shot_list.csv", "readiness.json", "lineage.json"]
    if is_ads:
        files.extend(["remake_brief.md (heuristic / unverified)", "branch_board.html (heuristic / unverified)"])
    files_html = "".join(f"<li>{html.escape(item)}</li>" for item in files)
    body = f"""<!doctype html>
<html lang="{'zh-CN' if lang == 'zh' else 'en'}">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(media.project_id)} Video Evidence</title><style>{css}</style></head>
<body><main>
<header>
  <p class="eyebrow">Video Evidence Workbench / 视频证据工作台</p>
  <h1>{html.escape(title)} · {html.escape(media.project_id)}</h1>
  <div class="version">{html.escape(version)}</div>
  <p class="lede">{html.escape(lede)}</p>
  <div class="metrics"><span>{html.escape(format_clock(media.duration_seconds))}</span><span>{html.escape(media.resolution)}</span><span>{len(shots)} {'个镜头' if lang == 'zh' else 'shots'}</span><span>{len(beats)} {'个节奏峰值' if lang == 'zh' else 'rhythm peaks'}</span></div>
</header>
<section class="grid">
  <article class="panel span2"><h2>{html.escape(opening_title)}</h2><p>{html.escape(opening_text)}</p><div class="rows">{first_window_rows}</div></article>
  <article class="panel"><h2>{html.escape(terms_title)}</h2><p>{html.escape(terms_text)}</p></article>
  <article class="panel"><h2>{html.escape(audio_title)}</h2><p>{html.escape(_audio_summary(music_state, tempo, len(transcript), lang))}</p></article>
  <article class="panel"><h2>{html.escape(files_title)}</h2><ul>{files_html}</ul></article>
</section>
<section class="panel"><h2>{html.escape(teardown_title)}</h2><div class="rows">{top_rows}</div></section>
<section class="panel"><h2>{html.escape(takeaway_title)}</h2><ul>{''.join(f'<li>{html.escape(_takeaway_text(item, lang))}</li>' for item in report.client_takeaways)}</ul></section>
<section class="panel"><h2>{html.escape(ready_title)}</h2><p>{html.escape(ready_text)}</p></section>
</main></body></html>"""
    atomic_write_text(path, body)


def render_blocked_professional_export(
    media: CanonicalMediaPackage,
    shots: list[Shot],
    readiness: dict[str, Any],
    path: Path,
) -> None:
    css = _delivery_css()
    reasons = readiness.get("reasons") or []
    items = "".join(f"<li>{html.escape(str(item))}</li>" for item in reasons) or "<li>readiness gate did not pass</li>"
    body = f"""<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(media.project_id)} Export Blocked</title><style>{css}</style></head>
<body><main>
<header>
  <p class="eyebrow">Evidence Export Blocked</p>
  <h1>{html.escape(media.project_id)}</h1>
  <p class="lede">当前只能作为 draft/debug package。结构化 evidence export 被 readiness gate 阻断。</p>
  <div class="metrics"><span>{len(shots)} shots</span><span>{html.escape(str(readiness.get("status", "blocked")))}</span><span>storyboard.html available</span><span>shot_list.csv available</span></div>
</header>
<section class="panel"><h2>Blocked Reasons</h2><ul>{items}</ul></section>
<section class="panel"><h2>Next Actions</h2><ul><li>Run provider annotation for every shot, or review every shot manually.</li><li>Resolve missing or placeholder evidence fields.</li><li>Regenerate the package after readiness passes.</li></ul></section>
</main></body></html>"""
    atomic_write_text(path, body)


def render_branch_board(report: AnalysisReport, media: CanonicalMediaPackage, shots: list[Shot], path: Path) -> None:
    css = _delivery_css()
    branch_columns = "".join(_branch_column(branch, shots) for branch in BRANCHES)
    body = f"""<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(media.project_id)} Branch Board</title><style>{css}</style></head>
<body><main>
<header>
  <p class="eyebrow">Creative Git for AI Video</p>
  <h1>Branch Experiment Board</h1>
  <p class="lede">Creative heuristic / unverified interpretation — not source evidence. Static branch board for choosing a keeper direction. No graph editor, login, collaboration, rollback UI, or multi-project history in v1.</p>
  <div class="metrics"><span>{html.escape(media.project_id)}</span><span>{len(shots)} shots</span><span>3 branches</span></div>
</header>
<section class="branches">{branch_columns}</section>
<section class="panel">
  <h2>Keeper Decision / 客户选择</h2>
  <div class="decision"><p>Keeper branch:</p><div class="line"></div><p>Reject reasons:</p><div class="box"></div><p>Next changes:</p><div class="box"></div></div>
</section>
</main></body></html>"""
    atomic_write_text(path, body)


def write_remake_brief(
    report: AnalysisReport,
    media: CanonicalMediaPackage,
    shots: list[Shot],
    transcript: list[TranscriptSegment],
    music: list[MusicProfile],
    path: Path,
) -> None:
    transcript_note = " ".join(segment.text for segment in transcript[:4]).strip() or "No usable transcript. Add human subtitle/voiceover summary before final delivery."
    music_note = f"{music[0].energy_level} energy / {music[0].tempo_bucket} tempo" if music else "unknown music profile"
    lines = [
        f"# Remake Brief: {_markdown_inline(media.project_id)}",
        "",
        "> Creative heuristic / unverified interpretation. This brief is not source evidence and requires operator review.",
        "",
        "## Positioning",
        "Single-ad teardown package for a creative lead. Use this as the handoff brief for editor, director, or AI generation workflow.",
        "",
        "## Audience Assumption",
        "Creative lead reviewing what to keep, remake, or generate next.",
        "",
        "## Core Sell / Message",
        "Review the original ad and move the clearest product promise or viewer pain into the first 3 seconds.",
        "",
        "## Shot Sequence",
    ]
    for shot in shots[:12]:
        lines.append(
            "- "
            f"{_markdown_inline(shot.timecode)}: "
            f"{_markdown_inline(shot.content_summary or shot.visual_description)}; "
            f"action: {_markdown_inline(shot.action)}; "
            f"audio: {_markdown_inline(shot.dialogue or shot.audio_notes)}"
        )
    lines.extend(
        [
            "",
            "## Voice / Subtitle Notes",
            _markdown_code_block(transcript_note),
            "",
            "## Sound / Rhythm Notes",
            _markdown_inline(music_note),
            "",
            "## Prompt Reverse Engineering",
            "- Visual style: derive from approved keyframes; keep uncertain fields explicit.",
            "- Subject/action: use the shot table as the source of truth.",
            "- Camera/motion: do not invent lens or equipment when not visible.",
            "- Negative prompt: avoid off-brand artifacts, unreadable text, inconsistent hands/faces, and unmotivated camera movement.",
            "",
            "## Branches",
            "- safer: keep structure, tighten pacing, clarify CTA.",
            "- stronger_hook: rebuild the first 3 seconds around pain/outcome.",
            "- premium_style: improve visual trust without losing short-form directness.",
            "",
            "## Human Review Checklist",
            "- Confirm hook claim is accurate.",
            "- Confirm on-screen text and voiceover are not fabricated.",
            "- Confirm keeper branch in branch_board.html.",
        ]
    )
    atomic_write_text(path, "\n".join(lines) + "\n")


def write_prompt_reverse_engineering(media: CanonicalMediaPackage, shots: list[Shot], path: Path) -> None:
    lang = _delivery_lang(media)
    lines = [
        f"# 提示词反推与模型适配: {_markdown_inline(media.project_id)}"
        if lang == "zh"
        else f"# Prompt Reverse Engineering: {_markdown_inline(media.project_id)}",
        "",
        "> 创意解释 / 启发式 / 未验证；不是源证据，使用前必须由 operator 复核。" if lang == "zh" else "> Creative interpretation / heuristic / unverified. This is not source evidence; operator review is required.",
        "",
        "## 使用规则" if lang == "zh" else "## Source Rules",
        "- 只使用 `shot_table.csv` 和主帧可见证据。" if lang == "zh" else "- Use visible frame evidence from `shot_table.csv`.",
        "- 不确定的镜头、灯光、器材、品牌承诺必须保留为未知，不补编。" if lang == "zh" else "- Keep unknown lens, lighting, and production details explicit.",
        "- 正式给模型前，优先复制 `model_prompt_pack.json` 中对应模型块。" if lang == "zh" else "- For generation, prefer the matching model block in `model_prompt_pack.json`.",
        "",
        "## 模型适配方式" if lang == "zh" else "## Model Adapters",
        "- `universal_text_to_video`: 通用文本生视频，适合先跑粗样。" if lang == "zh" else "- `universal_text_to_video`: generic text-to-video prompt.",
        "- `image_to_video`: 以主帧/关键帧为参考，适合重做同构镜头。" if lang == "zh" else "- `image_to_video`: use the primary frame as visual reference.",
        "- `runway_gen_style`: 强调镜头运动、主体运动、负面约束。" if lang == "zh" else "- `runway_gen_style`: camera motion, subject motion, negative constraints.",
        "- `kling_style_json`: 严格 JSON 字段，便于脚本映射和批量生成。" if lang == "zh" else "- `kling_style_json`: strict JSON fields for automation.",
        "- `veo_sora_narrative`: 更完整的电影化自然语言描述。" if lang == "zh" else "- `veo_sora_narrative`: longer cinematic natural-language prompt.",
        "- `luma_pika_edit`: 更适合图生视频或局部改动的保留/修改描述。" if lang == "zh" else "- `luma_pika_edit`: preservation and edit instructions for image-to-video/edit flows.",
        "",
        "## 逐镜头提示词" if lang == "zh" else "## Shot Prompts",
    ]
    for shot in shots[:12]:
        adapter = build_prompt_adapter(shot, media)
        universal = adapter["universal_text_to_video"]
        image_to_video = adapter["image_to_video"]
        kling = adapter["kling_style_json"]
        veo = adapter["veo_sora_narrative"]
        lines.extend(
            [
                f"### {_markdown_inline(shot.timecode or shot.shot_id)}",
                f"- 参考帧: {_markdown_inline(shot.primary_frame_ref or shot.frame_ref or 'contact_sheet.jpg')}"
                if lang == "zh"
                else f"- Reference: {_markdown_inline(shot.primary_frame_ref or shot.frame_ref or 'contact_sheet.jpg')}",
                f"- 主体 / 动作: {_markdown_inline(_shot_subject(shot, lang))} / {_markdown_inline(_shot_action(shot, lang))}"
                if lang == "zh"
                else f"- Subject/action: {_markdown_inline(_shot_subject(shot, lang))} / {_markdown_inline(_shot_action(shot, lang))}",
                f"- 镜头语言: {_markdown_inline(_camera_text(shot, lang, include_composition=True))}"
                if lang == "zh"
                else f"- Camera/composition: {_markdown_inline(_camera_text(shot, lang, include_composition=True))}",
                "",
                "#### 通用文本生视频" if lang == "zh" else "#### Universal Text-to-Video",
                _markdown_code_block(universal["prompt"]),
                "#### 图生视频 / 参考帧" if lang == "zh" else "#### Image-to-Video / Reference Frame",
                _markdown_code_block(image_to_video["prompt"]),
                "#### 严格 JSON 模板" if lang == "zh" else "#### Strict JSON Template",
                _markdown_code_block(json.dumps(kling, ensure_ascii=False, indent=2), language="json"),
                "#### 电影化叙述模板" if lang == "zh" else "#### Cinematic Narrative Template",
                _markdown_code_block(veo["prompt"]),
                "",
            ]
        )
    atomic_write_text(path, "\n".join(lines).rstrip() + "\n")


def write_model_prompt_pack(media: CanonicalMediaPackage, shots: list[Shot], path: Path) -> None:
    lang = _delivery_lang(media)
    pack = {
        "schema_version": 1,
        "project_id": media.project_id,
        "delivery_language": lang,
        "claim_type": "creative_interpretation",
        "heuristic": True,
        "verification_status": "unverified",
        "note": (
            "模型适配提示词包，供 operator 复核后复制或映射到自动化脚本；这是生成用 prompt 结构，不是官方 API 请求 schema。"
            if lang == "zh"
            else "Model-specific prompt adapters for operator review. These are generation-ready prompt blocks, not official API request schemas."
        ),
        "source_rules": (
            [
                "使用每个 shot 的 primary_frame_ref 作为视觉证据。",
                "不补编品牌承诺、字幕、人脸、法律文案、镜头或器材。",
                "未知制作细节保留为未知。",
                "需要保持原构图时优先使用 image_to_video。",
            ]
            if lang == "zh"
            else [
                "Use shot primary_frame_ref as visual evidence.",
                "Do not invent brand claims, subtitles, faces, legal copy, lens, or equipment.",
                "Keep unknown production details explicit.",
                "Prefer image_to_video when preserving original framing matters.",
            ]
        ),
        "shots": [build_prompt_adapter(shot, media) for shot in shots],
    }
    atomic_write_text(path, json.dumps(pack, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def build_prompt_adapter(shot: Shot, media: CanonicalMediaPackage) -> dict[str, Any]:
    lang = _delivery_lang(media)
    subject = _shot_subject(shot, lang) or _shot_subject(shot, "en") or "visible subject"
    action = _shot_action(shot, lang) or _shot_action(shot, "en") or "visible action"
    content = _shot_title(shot, lang)
    camera = _camera_text(shot, lang, include_composition=False) or "camera pending"
    composition = _camera_text(shot, lang, include_composition=True) or camera
    style = (shot.style_notes_zh or shot.style_notes or shot.visual_description or "match the reference frame").strip()
    remake = _shot_remake(shot, lang) or _shot_remake(shot, "en") or "preserve the visible subject and camera intent"
    duration = max(1.0, round(float(shot.duration or 3.0), 1))
    aspect_ratio = _aspect_label(media)
    reference = shot.primary_frame_ref or shot.frame_ref or "contact_sheet.jpg"
    negative = _negative_prompt(lang)
    universal_prompt = _join_prompt_parts(
        [
            content,
            f"主体：{subject}" if lang == "zh" else f"Subject: {subject}",
            f"动作：{action}" if lang == "zh" else f"Action: {action}",
            f"镜头：{camera}" if lang == "zh" else f"Camera: {camera}",
            f"构图：{composition}" if lang == "zh" else f"Composition: {composition}",
            f"风格：{style}" if lang == "zh" else f"Style: {style}",
            f"时长：{duration}s，画幅：{aspect_ratio}" if lang == "zh" else f"Duration: {duration}s, aspect ratio: {aspect_ratio}",
        ]
    )
    image_prompt = _join_prompt_parts(
        [
            f"以参考帧 {reference} 为第一视觉约束。" if lang == "zh" else f"Use reference frame {reference} as the first visual constraint.",
            f"保留主体、构图、产品识别和光线方向；生成动作：{action}。" if lang == "zh" else f"Preserve subject, composition, product legibility, and light direction; generate action: {action}.",
            f"镜头运动：{camera}。" if lang == "zh" else f"Camera movement: {camera}.",
            f"复拍/生成要求：{_sentence(remake, lang)}" if lang == "zh" else f"Remake/generation note: {_sentence(remake, lang)}",
        ]
    )
    must_keep = (
        ["同一产品类别", "同一故事段落", "主体清晰可读"]
        if lang == "zh"
        else ["same product category", "same story beat", "readable main subject"]
    )
    must_avoid = (
        ["伪造字幕", "错误品牌标志", "多余手指或扭曲人脸", "随机镜头运动"]
        if lang == "zh"
        else ["fake subtitles", "wrong logos", "extra hands or distorted faces", "random camera movement"]
    )
    preserve = (
        ["主体身份", "产品可读性", "机位角度", "构图关系"]
        if lang == "zh"
        else ["subject identity", "product legibility", "camera angle", "composition"]
    )
    return {
        "shot_id": shot.shot_id,
        "shot_no": shot.shot_no,
        "timecode": shot.timecode,
        "duration_seconds": duration,
        "aspect_ratio": aspect_ratio,
        "primary_frame_ref": reference,
        "claim_type": "creative_interpretation",
        "heuristic": True,
        "verification_status": "unverified",
        "story_beat": _beat_text(shot.story_beat, lang),
        "universal_text_to_video": {
            "prompt": universal_prompt,
            "negative_prompt": negative,
            "duration_seconds": duration,
            "aspect_ratio": aspect_ratio,
        },
        "image_to_video": {
            "reference_image": reference,
            "prompt": image_prompt,
            "negative_prompt": negative,
            "motion_strength": "medium" if duration <= 3.0 else "low_to_medium",
            "preserve": preserve,
        },
        "runway_gen_style": {
            "prompt": universal_prompt,
            "camera_motion": shot.camera_motion,
            "subject_motion": action,
            "style": style,
            "negative_prompt": negative,
        },
        "kling_style_json": {
            "scene": content,
            "subject": subject,
            "action": action,
            "camera": camera,
            "composition": composition,
            "style": style,
            "duration_seconds": duration,
            "aspect_ratio": aspect_ratio,
            "negative_prompt": negative,
            "must_keep": must_keep,
            "must_avoid": must_avoid,
        },
        "veo_sora_narrative": {
            "prompt": _narrative_prompt(content, subject, action, camera, composition, style, lang),
            "negative_prompt": negative,
        },
        "luma_pika_edit": {
            "reference_image": reference,
            "prompt": image_prompt,
            "edit_intent": remake,
            "preserve": "主体、产品、构图、光线方向" if lang == "zh" else "main subject, product, framing, lighting direction",
            "change": "只改动作和节奏；除非 operator 选择分支变体" if lang == "zh" else "motion and timing only unless operator requests a branch variation",
            "negative_prompt": negative,
        },
    }


def write_revision_plan(media: CanonicalMediaPackage, shots: list[Shot], path: Path) -> None:
    first_shot = shots[0] if shots else None
    hook_ref = first_shot.timecode if first_shot else "00:00-00:03"
    lines = [
        f"# Revision Plan: {_markdown_inline(media.project_id)}",
        "",
        "> Creative heuristic / unverified interpretation. This plan is not source evidence and requires operator review.",
        "",
        "## Goal",
        "Choose one keeper branch from `branch_board.html`, then produce one revised script/shot list before generation or reshoot.",
        "",
        "## Branch Tasks",
        "### safer",
        "- Keep the current shot order.",
        "- Tighten subtitles and CTA.",
        "- Remove unclear claims before production.",
        "",
        "### stronger_hook",
        f"- Rewrite {_markdown_inline(hook_ref)} around the clearest pain, result, or contrast.",
        "- Move proof or outcome earlier if the demo arrives late.",
        "- Produce two opening subtitle variants.",
        "",
        "### premium_style",
        "- Keep the same sales argument.",
        "- Upgrade lighting, framing, wardrobe/prop discipline, and product legibility.",
        "- Avoid over-polished footage if the channel needs native UGC believability.",
        "",
        "## Review Gate",
        "- Client selects keeper branch.",
        "- Client writes reject reason if no branch is acceptable.",
        "- Operator updates prompt/script and records the next commit in `lineage.json`.",
    ]
    atomic_write_text(path, "\n".join(lines) + "\n")


def _storyboard_card(shot: Shot, lang: str, *, include_remake: bool) -> str:
    thumb = _thumb_path(shot)
    title = _shot_title(shot, lang)
    camera = _camera_text(shot, lang)
    if not camera:
        camera = "待标注" if lang == "zh" else "vision pending"
    subject = _shot_subject(shot, lang)
    action = _shot_action(shot, lang)
    remake = _shot_remake(shot, lang)
    labels = _labels(lang)
    remake_row = (
        f'<dt>{labels["remake"]}</dt><dd>{html.escape(remake or labels["remake_pending"])}</dd>'
        if include_remake
        else ""
    )
    return f"""<article class="frame" id="{html.escape(shot.shot_id)}">
  <img src="{html.escape(thumb)}" alt="shot {shot.shot_no}">
  <div class="frameBody">
    <div class="frameTop"><span>#{shot.shot_no:02d}</span><b>{html.escape(_story_beat_display(shot, lang))}</b></div>
    <h2>{html.escape(title)}</h2>
    <p class="shotCopy">{html.escape(subject or labels["subject_pending"])} / {html.escape(action or labels["action_pending"])}</p>
    <dl>
      <dt>{labels["tc"]}</dt><dd>{html.escape(shot.timecode)} · {shot.duration:.1f}s</dd>
      <dt>{labels["camera"]}</dt><dd>{html.escape(camera)}</dd>
      <dt>{labels["sound"]}</dt><dd>{html.escape(_rhythm_text(shot.sound_rhythm or shot.rhythm_notes, lang) or labels["sound_pending"])}</dd>
      {remake_row}
    </dl>
    <div class="badges"><span>{html.escape(_boundary_text(shot.boundary_confidence, lang))}</span><span>{html.escape(_status_text(shot.readiness_status or "blocked", lang))}</span><span>{shot.visual_confidence:.2f}</span></div>
  </div>
</article>"""


def _shot_list_row(shot: Shot, lang: str, *, include_remake: bool) -> str:
    camera = _camera_text(shot, lang, include_composition=True)
    cells = (
        "<tr>"
        f"<td>#{shot.shot_no:02d}</td>"
        f"<td>{html.escape(_story_beat_display(shot, lang))}</td>"
        f"<td>{html.escape(shot.timecode)}<br>{shot.duration:.1f}s</td>"
        f"<td>{html.escape(_shot_title(shot, lang))}</td>"
        f"<td>{html.escape(_shot_subject(shot, lang))}<br>{html.escape(_shot_action(shot, lang))}</td>"
        f"<td>{html.escape(camera)}</td>"
        f"<td>{html.escape(_rhythm_text(shot.sound_rhythm or shot.dialogue or shot.audio_notes, lang))}</td>"
    )
    if include_remake:
        cells += f"<td>{html.escape(_shot_remake(shot, lang))}</td>"
    return cells + f"<td>{html.escape(_status_text(shot.readiness_status or 'blocked', lang))}<br>{shot.visual_confidence:.2f}</td></tr>"


def _compact_shot_row(shot: Shot, lang: str) -> str:
    thumb = _thumb_path(shot)
    title = _shot_title(shot, lang)
    return (
        "<div class='shotrow'>"
        f"<img src='{html.escape(thumb)}' alt='shot {shot.shot_no}'>"
        f"<div><b>{html.escape(shot.timecode)} / {shot.duration:.1f}s</b><p>{html.escape(title)}</p></div>"
        f"<div class='tag'>{html.escape(_rhythm_text(shot.rhythm_notes or 'review', lang))}</div>"
        "</div>"
    )


def _thumb_path(shot: Shot) -> str:
    ref = shot.primary_frame_ref or shot.frame_ref
    return f"../assets/keyframes/{ref}" if ref else "../assets/contact_sheet.jpg"


def _shot_title(shot: Shot, lang: str) -> str:
    if lang == "zh":
        return shot.content_summary_zh or shot.content_summary or shot.visual_description or "需要视觉标注或人工复核"
    return shot.content_summary or shot.visual_description or shot.content_summary_zh or "Vision model or human review required"


def _shot_subject(shot: Shot, lang: str) -> str:
    return (shot.subject_zh or shot.subject) if lang == "zh" else (shot.subject or shot.subject_zh)


def _shot_action(shot: Shot, lang: str) -> str:
    return (shot.action_zh or shot.action) if lang == "zh" else (shot.action or shot.action_zh)


def _shot_remake(shot: Shot, lang: str) -> str:
    return (shot.remake_notes_zh or shot.remake_notes) if lang == "zh" else (shot.remake_notes or shot.remake_notes_zh)


def _camera_text(shot: Shot, lang: str, include_composition: bool = False) -> str:
    """Return only camera evidence already present on the shot.

    Camera labels are model/human observations, not keys into a campaign
    template.  Preserve them verbatim across profiles and locales so a generic
    label such as ``wide side profile`` cannot silently become a vehicle claim.
    """
    values = [shot.shot_scale, shot.camera_angle, shot.camera_motion]
    if include_composition:
        values.append(shot.composition)
    separator = "，" if lang == "zh" else ", "
    return separator.join(item for item in values if _filled(item))


def _beat_text(value: str, lang: str) -> str:
    value = value or ""
    if value.startswith("heuristic_unverified:"):
        value = value.split(":", 1)[1]
    if lang != "zh":
        return value.replace("_", " ") or "beat pending"
    mapping = {
        "hook": "钩子",
        "setup": "建立场景",
        "demo": "能力演示",
        "reaction": "人物反应",
        "motif": "视觉母题",
        "product_reveal": "产品露出",
        "proof": "能力证明",
        "payoff": "情绪收束",
        "motif_payoff": "母题回收",
        "product_title": "产品标题",
        "brand_payoff": "品牌收束",
        "cta": "尾卡",
        "opening_sequence": "开场序列",
        "early_sequence": "前段序列",
        "middle_sequence": "中段序列",
        "late_sequence": "后段序列",
        "closing_sequence": "收尾序列",
    }
    return mapping.get(value, value or "段落待复核")


def _story_beat_display(shot: Shot, lang: str) -> str:
    value = _beat_text(shot.story_beat, lang)
    verification = _annotation_verification(shot)
    if verification == "human_reviewed":
        suffix = "解释 / 已人工复核" if lang == "zh" else "interpretation / human-reviewed"
    elif shot.annotation_source == "machine" or shot.story_beat.startswith("heuristic_unverified:"):
        suffix = "启发式解释 / 未验证" if lang == "zh" else "heuristic interpretation / unverified"
    else:
        suffix = "模型解释 / 未验证" if lang == "zh" else "model interpretation / unverified"
    return f"{value} · {suffix}"


def _status_text(value: str, lang: str) -> str:
    value = (value or "").lower()
    if lang != "zh":
        return value.upper() if value in {"ready", "blocked"} else value
    return {
        "ready": "已通过",
        "blocked": "阻断",
        "draft": "草稿",
        "rejected": "已拒绝",
        "needs_review": "需复核",
    }.get(value, value or "阻断")


def _boundary_text(value: str, lang: str) -> str:
    value = (value or "low").lower()
    if lang != "zh":
        return f"{value} boundary"
    return {"high": "边界高置信", "medium": "边界中置信", "low": "边界低置信"}.get(value, f"边界{value}")


def _rhythm_text(value: str, lang: str) -> str:
    if not value:
        return ""
    if lang != "zh":
        return value
    text = value
    replacements = [
        ("low;", "低能量；"),
        ("medium;", "中等能量；"),
        ("high;", "高能量；"),
        ("sparse rhythm activity", "节奏稀疏"),
        ("moderate rhythm activity", "节奏中等"),
        ("dense rhythm peaks", "节奏峰值密集"),
        ("check edit/music alignment", "检查剪辑与音乐重音是否对齐"),
        ("beats", "节奏峰值"),
        ("review", "待复核"),
    ]
    for source, target in replacements:
        text = text.replace(source, target)
    return text.replace("; ", "；").replace("； ", "；").replace(";", "；")


def _audio_summary(energy: str, tempo: str, transcript_count: int, lang: str) -> str:
    if lang != "zh":
        return f"Music profile: {energy} energy, {tempo} tempo. Transcript segments: {transcript_count}."
    return f"音乐画像：{_music_text(energy)}能量，{_tempo_text(tempo)}速度。转写片段：{transcript_count} 个。"


def _music_text(value: str) -> str:
    return {"low": "低", "medium": "中等", "high": "高", "unknown": "未知"}.get((value or "").lower(), value)


def _tempo_text(value: str) -> str:
    return {"slow": "慢", "medium": "中等", "fast": "快", "unknown": "未知"}.get((value or "").lower(), value)


def _gate_reason_text(value: str, lang: str) -> str:
    if lang != "zh":
        return value
    replacements = {
        "professional readiness passed": "专业门禁已通过",
        "complete vision annotation or all-shot human review required": "需要完成全量视觉标注或逐镜头人工复核",
        "duplicate primary frame refs": "检测到重复主帧",
        "placeholder strings in professional fields": "关键字段仍包含占位文本",
    }
    return replacements.get(value, value)


def _aspect_label(media: CanonicalMediaPackage) -> str:
    ratio = float(media.aspect_ratio or 0.0)
    if ratio and ratio < 0.8:
        return "9:16"
    if ratio and ratio > 1.5:
        return "16:9"
    return "1:1"


def _negative_prompt(lang: str) -> str:
    if lang == "zh":
        return "不要生成错误品牌标志、不可读字幕、额外手指、扭曲人脸、随机镜头运动、塑料感皮肤、漂浮物体、无动机变焦。"
    return "wrong logos, unreadable subtitles, extra fingers, distorted faces, random camera movement, plastic skin, floating objects, unmotivated zoom."


def _join_prompt_parts(parts: list[str]) -> str:
    return " ".join(part.strip() for part in parts if part and part.strip())


def _sentence(value: str, lang: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    if lang == "zh":
        return text.rstrip("。.!！；; ") + "。"
    return text.rstrip(".。!！；; ") + "."


def _narrative_prompt(content: str, subject: str, action: str, camera: str, composition: str, style: str, lang: str) -> str:
    if lang == "zh":
        return _join_prompt_parts(
            [
                _sentence(content, lang),
                _sentence(f"{subject}，{action}", lang),
                _sentence(f"镜头使用{camera}，保持{composition}", lang),
                _sentence(f"商业广告电影质感：{style}", lang),
                "不要伪造字幕、品牌标志、法律声明或不可见细节。",
            ]
        )
    return _join_prompt_parts(
        [
            _sentence(content, lang),
            _sentence(f"{subject} {action}", lang),
            _sentence(f"Use {camera}. Maintain {composition}", lang),
            _sentence(f"Commercial film look: {style}", lang),
            "No fabricated copy, logos, legal claims, or unseen details.",
        ]
    )


def _labels(lang: str) -> dict[str, str]:
    if lang == "zh":
        return {
            "tc": "时码",
            "camera": "镜头",
            "sound": "声音",
            "remake": "复拍",
            "subject_pending": "主体待复核",
            "action_pending": "动作待复核",
            "sound_pending": "声音待复核",
            "remake_pending": "复拍建议待复核",
        }
    return {
        "tc": "TC",
        "camera": "Camera",
        "sound": "Sound",
        "remake": "Remake",
        "subject_pending": "subject pending",
        "action_pending": "action pending",
        "sound_pending": "sound pending",
        "remake_pending": "remake pending",
    }


def _filled(value: str) -> bool:
    return bool(value and value.lower() not in {"unknown", "tbd", "review required"})


def _branch_column(branch: dict[str, str], shots: list[Shot]) -> str:
    first_shot = shots[0] if shots else None
    prompt_change = "Rewrite first-frame text, tighten subject/action wording, preserve approved visual references."
    shot_change = first_shot.timecode if first_shot else "0:00-0:03"
    return f"""<article class="panel branch">
<p class="eyebrow">{html.escape(branch['name'])}</p>
<h2>{html.escape(branch['title'])}</h2>
<p>{html.escape(branch['goal'])}</p>
<dl>
  <dt>Key shot change</dt><dd>{html.escape(shot_change)} focus change</dd>
  <dt>Prompt / asset change</dt><dd>{html.escape(prompt_change)}</dd>
  <dt>Estimated cost</dt><dd>{html.escape(branch['cost'])}</dd>
  <dt>Risk</dt><dd>{html.escape(branch['risk'])}</dd>
  <dt>Applicable scenario</dt><dd>{html.escape(branch['scenario'])}</dd>
  <dt>Keeper recommendation</dt><dd>{html.escape(branch['keeper'])}</dd>
</dl>
</article>"""


def _hook_diagnosis(shots: list[Shot], transcript: list[TranscriptSegment], beats: list[BeatEvent], lang: str = "zh") -> str:
    early_beats = [beat for beat in beats if beat.time <= 3.0]
    early_text = " ".join(seg.text for seg in transcript if seg.start_time <= 3.0).strip()
    if not shots:
        return "未验证启发式：没有可用镜头数据，需要人工复核前 3 秒。" if lang == "zh" else "Unverified heuristic: no shot data is available; review the first 3 seconds manually."
    if not early_text:
        if lang == "zh":
            return f"未验证启发式：前 3 秒检测到 {len(early_beats)} 个节奏峰值，但没有可靠转写；应检查首帧是否明确产品、场景张力或结果承诺。"
        return f"Unverified heuristic: the first pass sees {len(early_beats)} rhythm peaks in the first 3 seconds but no reliable transcript. Verify whether the first frame names the product, pain, or outcome."
    if lang == "zh":
        return f"未验证启发式：开场转写为“{early_text[:140]}”。检查画面、字幕和声音是否在 0:03 前指向同一个痛点或结果。"
    return f"Unverified heuristic: opening transcript is “{early_text[:140]}”. Check whether the visual, subtitle, and audio all point to the same pain/outcome before 0:03."


def _opening_sequence_interpretation(
    shots: list[Shot], transcript: list[TranscriptSegment], beats: list[BeatEvent], lang: str = "zh"
) -> str:
    early_beats = [beat for beat in beats if beat.time <= 3.0]
    early_text = " ".join(seg.text for seg in transcript if seg.start_time <= 3.0).strip()
    if not shots:
        return "未验证解释：没有可用镜头数据，需要人工复核开场序列。" if lang == "zh" else "Unverified interpretation: no shot data is available; review the opening sequence manually."
    if lang == "zh":
        transcript_note = f"开场转写为“{early_text[:140]}”" if early_text else "没有可靠开场转写"
        return f"未验证解释：前 3 秒检测到 {len(early_beats)} 个节奏峰值，{transcript_note}；结合原片复核开场信息、节奏和场景衔接。"
    transcript_note = f"the opening transcript is “{early_text[:140]}”" if early_text else "there is no reliable opening transcript"
    return f"Unverified interpretation: {len(early_beats)} rhythm peaks were detected in the first 3 seconds and {transcript_note}; check opening information, rhythm, and continuity against the source."


def _delivery_lang(media: CanonicalMediaPackage) -> str:
    value = str(media.metadata.get("delivery_language") or "zh").lower()
    return "en" if value.startswith("en") else "zh"


def _is_ads_profile(media: CanonicalMediaPackage) -> bool:
    return _profile_name(media.analysis_profile) == "ads"


def _profile_name(value: object) -> str:
    return str(getattr(value, "value", value)).strip().lower()


def _annotation_verification(shot: Shot) -> str:
    if shot.story_beat.startswith("heuristic_unverified:"):
        return "unverified"
    return "human_reviewed" if shot.annotation_source == "human" and shot.readiness_status == "ready" else "unverified"


def _version_label(lang: str) -> str:
    return "中文版本" if lang == "zh" else "English version"


def _takeaway_text(value: str, lang: str) -> str:
    if lang != "zh":
        return value
    prefix = "Interpretation/review note (unverified): "
    if value.startswith(prefix):
        return f"解释/复核备注（未验证）：{_takeaway_text(value[len(prefix):], lang)}"
    mapping = {
        "Review the first 3-5 seconds against the strongest visual and audio peaks.": "用最强画面点和声音峰值复核前 3-5 秒。",
        "Check whether brand, product, or topic recognition appears before viewer attention drops.": "检查品牌、产品或主题识别是否在注意力下降前出现。",
        "No usable transcript was produced; run ASR again or import subtitles before final client delivery.": "当前没有可用转写；正式交付前应重新运行 ASR 或导入字幕。",
        "Rhythm peak density is low; verify whether that is intentional restraint or a pacing issue.": "节奏峰值密度偏低；判断这是有意克制还是节奏问题。",
        "Rhythm peak density is high; verify that edits and sound hits do not flatten emphasis.": "节奏峰值密度偏高；检查剪辑和声音重音是否削弱重点。",
    }
    return mapping.get(value, value)


def _delivery_css() -> str:
    return """
    *{box-sizing:border-box}body{margin:0;background:#f5f5f2;color:#111;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",Arial,sans-serif}main{max-width:1240px;margin:0 auto;padding:28px}header{border-bottom:1px solid #aaa;padding:18px 0 26px;margin-bottom:20px}h1{font-size:42px;line-height:1.04;margin:0 0 12px;letter-spacing:0}h2{font-size:22px;margin:0 0 14px;line-height:1.2}.eyebrow{font-size:12px;text-transform:uppercase;letter-spacing:.12em;color:#555}.version{display:inline-block;border:1px solid #999;background:#fff;border-radius:999px;padding:6px 10px;margin:0 0 12px;font-size:13px}.lede{max-width:820px;font-size:18px;line-height:1.65;color:#333}.metrics{display:flex;gap:8px;flex-wrap:wrap}.metrics span,.tag{border:1px solid #999;border-radius:999px;padding:6px 9px;background:#fff}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}.span2{grid-column:span 2}.panel{border:1px solid #999;background:#fff;padding:18px;margin-bottom:14px}.panel p,.panel li{line-height:1.7}.rows{display:grid;gap:10px}.shotrow{display:grid;grid-template-columns:150px minmax(0,1fr) 150px;gap:16px;align-items:start;border-top:1px solid #ccc;padding:14px 0}.shotrow img{width:150px;aspect-ratio:16/9;object-fit:cover;border:1px solid #aaa;background:#eee}.shotrow p{margin:7px 0 0;color:#444;line-height:1.65}.branches{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.branch dl{display:grid;gap:8px}.branch dt{font-size:12px;color:#555;text-transform:uppercase}.branch dd{margin:0 0 8px;line-height:1.55}.decision .line{height:34px;border-bottom:1px solid #777}.decision .box{height:90px;border:1px solid #999;margin:8px 0 14px}@media(max-width:860px){.grid,.branches{grid-template-columns:1fr}.span2{grid-column:auto}.shotrow{grid-template-columns:1fr}.shotrow img{width:100%}}
    """


def _storyboard_css() -> str:
    return """
    :root{color-scheme:dark;--bg:#0b0c0d;--panel:#151716;--panel2:#101211;--ink:#f4f0e8;--text:#c9c1b4;--muted:#80796e;--line:#2a2e2b;--green:#8edb9a;--amber:#e3bf74;--red:#df7f70}
    *{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:"Helvetica Neue","PingFang SC",Arial,sans-serif}main{max-width:1540px;margin:0 auto;padding:20px}
    header{display:grid;grid-template-columns:minmax(0,1fr) 360px;gap:20px;align-items:start;border-bottom:1px solid var(--line);padding:8px 0 20px;margin-bottom:16px}h1{font-size:44px;line-height:1.02;margin:0 0 12px;letter-spacing:0}h2{margin:0;font-size:20px;line-height:1.32}.eyebrow{margin:0 0 10px;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.14em}.version{display:inline-block;border:1px solid var(--line);border-radius:999px;background:var(--panel2);padding:7px 10px;margin:0 0 12px;color:var(--text);font-size:13px}.lede{max-width:820px;color:var(--text);font-size:17px;line-height:1.7}.gate{border:1px solid var(--line);background:var(--panel);border-radius:6px;padding:15px}.gate b{font-size:28px}.gate span{display:block;color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.1em}.gate ul{margin:12px 0 0;padding-left:18px;color:var(--text);line-height:1.65}.gate.ready{border-color:var(--green)}.gate.blocked{border-color:var(--red)}
    .metrics{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px}.metrics span,.badges span{border:1px solid var(--line);border-radius:999px;background:var(--panel2);padding:7px 9px;color:var(--text);font-size:12px}.storyboard{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.frame{border:1px solid var(--line);border-radius:6px;background:var(--panel);overflow:hidden;display:grid;grid-template-columns:42% 1fr;min-height:260px}.frame img{width:100%;height:100%;object-fit:cover;background:#050505;border-right:1px solid var(--line)}.frameBody{padding:16px;display:grid;gap:12px;align-content:start}.frameTop{display:flex;justify-content:space-between;gap:10px;color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.1em}.frame p{margin:0;color:var(--text);line-height:1.62}.shotCopy{font-size:14px}.frame dl{display:grid;grid-template-columns:58px 1fr;gap:8px 12px;margin:0;color:var(--text);font-size:13px}.frame dt{color:var(--muted);text-transform:uppercase;font-size:11px;letter-spacing:.09em}.frame dd{margin:0;line-height:1.55}.badges{display:flex;gap:6px;flex-wrap:wrap}.shotlist{margin-top:20px;border:1px solid var(--line);border-radius:6px;background:var(--panel);overflow:hidden}.sectionHead{display:flex;justify-content:space-between;gap:12px;align-items:end;padding:16px;border-bottom:1px solid var(--line)}.sectionHead p{margin:0;color:var(--muted);line-height:1.5}.tableWrap{overflow:auto}table{width:100%;min-width:1450px;border-collapse:collapse;table-layout:fixed}th,td{border-bottom:1px solid var(--line);padding:13px;text-align:left;vertical-align:top;font-size:13px;line-height:1.58;white-space:normal;word-break:break-word}th{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.1em;background:#111312}th:nth-child(1),td:nth-child(1){width:70px}th:nth-child(2),td:nth-child(2){width:110px}th:nth-child(3),td:nth-child(3){width:120px}th:nth-child(9),td:nth-child(9){width:90px}
    @media(max-width:1100px){header{grid-template-columns:1fr}.storyboard{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:720px){main{padding:12px}.storyboard{grid-template-columns:1fr}h1{font-size:34px}.sectionHead{display:block}}
    """
