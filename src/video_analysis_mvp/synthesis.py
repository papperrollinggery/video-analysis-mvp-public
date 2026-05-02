from __future__ import annotations

import html
from pathlib import Path

from .paths import ProjectPaths
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
from .store import write_manifest
from .utils import format_clock, run_command
from .visual import write_shots_csv


def synthesize(paths: ProjectPaths) -> AnalysisReport:
    media = CanonicalMediaPackage.model_validate(load_json(paths.data / "media_package.json"))
    shots = [Shot.model_validate(item) for item in load_json(paths.data / "shots.json")]
    scenes = [Scene.model_validate(item) for item in load_json(paths.data / "scenes.json")]
    transcript = _load_list(paths.data / "transcript.json", TranscriptSegment)
    beats = _load_list(paths.data / "beats.json", BeatEvent)
    music = _load_list(paths.data / "music_profile.json", MusicProfile)
    _normalize_shots(media, shots)
    _attach_audio_to_shots(shots, transcript, beats, music)
    dump_json(paths.data / "shots.json", shots)
    write_shots_csv(paths.reports / "shot_breakdown.csv", shots)
    report = build_report(media, shots, scenes, transcript, beats, music, paths)
    dump_json(paths.data / "analysis_report.json", report)
    render_html_report(report, media, shots, scenes, transcript, beats, music, paths.reports / "report.html")
    render_pdf_report(paths.reports / "report.html", paths.reports / "overview.pdf")
    write_manifest(paths, media, "reported", report.artifacts)
    return report


def _load_list(path: Path, cls):
    if not path.exists():
        return []
    return [cls.model_validate(item) for item in load_json(path)]


def _attach_audio_to_shots(
    shots: list[Shot],
    transcript: list[TranscriptSegment],
    beats: list[BeatEvent],
    music: list[MusicProfile],
) -> None:
    for shot in shots:
        speech = [seg.text for seg in transcript if _overlaps(shot.start_time, shot.end_time, seg.start_time, seg.end_time)]
        joined_speech = " ".join(speech)[:220]
        shot.dialogue = joined_speech
        shot.speech_summary = joined_speech
        beat_count = sum(1 for beat in beats if shot.start_time <= beat.time <= shot.end_time)
        shot.beat_density = round(beat_count / max(shot.duration, 0.1), 3)
        shot.rhythm_notes = _rhythm_note(shot.beat_density)
        active_music = next((item for item in music if _overlaps(shot.start_time, shot.end_time, item.start_time, item.end_time)), None)
        shot.music_state = active_music.energy_level if active_music else "unknown"
        shot.sound_design = "music-led" if active_music else "review required"
        if not shot.audio_notes:
            shot.audio_notes = "review dialogue, music, and SFX relationship"


def _normalize_shots(media: CanonicalMediaPackage, shots: list[Shot]) -> None:
    frame_count = max(1, len(list((Path(media.review_copy_path).parent / "keyframes").glob("frame-*.jpg"))))
    for index, shot in enumerate(shots, start=1):
        if not shot.scene_no:
            shot.scene_no = f"{((index - 1) // 4) + 1:03d}"
        if not shot.shot_no:
            shot.shot_no = index
        if not shot.setup_id:
            shot.setup_id = chr(65 + ((index - 1) % 26))
        if not shot.timecode:
            shot.timecode = f"{format_clock(shot.start_time)}-{format_clock(shot.end_time)}"
        if not shot.frame_ref:
            shot.frame_ref = f"frame-{min(index, frame_count):04d}.jpg"
        if shot.composition == "unknown":
            shot.composition = "to annotate"
        if shot.camera_angle == "unknown":
            shot.camera_angle = "to annotate"
        if shot.camera_motion == "unknown":
            shot.camera_motion = "to annotate"
        if shot.equipment == "TBD":
            shot.equipment = "not inferable from final video"
        if shot.lens == "TBD":
            shot.lens = "not inferable from final video"
        if shot.subject == "unknown":
            shot.subject = "to annotate"
        if shot.action == "unknown" or shot.action == "review required":
            shot.action = "to annotate"
        if not shot.visual_description:
            shot.visual_description = "to annotate from frame"
        if not shot.review_notes:
            shot.review_notes = "machine segmented; visual fields require human/model annotation"
        if not shot.direction_notes:
            shot.direction_notes = "to annotate blocking, screen direction, and action beat"
        if not shot.lighting_vfx:
            shot.lighting_vfx = "to annotate lighting, VFX, and AI artifacts"


def _rhythm_note(beat_density: float) -> str:
    if beat_density >= 0.8:
        return "dense rhythm peaks; check edit/music alignment"
    if beat_density >= 0.25:
        return "moderate rhythm activity"
    return "sparse rhythm activity"


def _overlaps(a_start: float, a_end: float, b_start: float, b_end: float) -> bool:
    return max(a_start, b_start) < min(a_end, b_end)


def build_report(
    media: CanonicalMediaPackage,
    shots: list[Shot],
    scenes: list[Scene],
    transcript: list[TranscriptSegment],
    beats: list[BeatEvent],
    music: list[MusicProfile],
    paths: ProjectPaths,
) -> AnalysisReport:
    avg_shot = sum(shot.duration for shot in shots) / max(len(shots), 1)
    beat_rate = len(beats) / max(media.duration_seconds, 1.0) * 60
    profile = media.analysis_profile.value
    visual = [
        f"{len(shots)} shots estimated across {len(scenes)} scenes.",
        f"Average estimated shot duration is {avg_shot:.1f}s, indicating {_pace_label(avg_shot)} pacing.",
        "Shot labels are first-pass machine estimates and should be reviewed for client delivery precision.",
    ]
    audio = [
        f"{len(transcript)} transcript segments generated.",
        f"Music profile reads as {music[0].energy_level if music else 'unknown'} energy with {music[0].tempo_bucket if music else 'unknown'} tempo.",
        f"{len(beats)} rhythm peaks detected across the runtime.",
    ]
    rhythm = [
        f"Estimated rhythm density is {beat_rate:.1f} peaks per minute.",
        _profile_specific_rhythm(profile, avg_shot, beat_rate),
    ]
    takeaways = _takeaways(profile, avg_shot, beat_rate, transcript)
    artifacts = {
        "overview_pdf": str(paths.reports / "overview.pdf"),
        "report_html": str(paths.reports / "report.html"),
        "shot_breakdown_csv": str(paths.reports / "shot_breakdown.csv"),
        "transcript_srt": str(paths.reports / "transcript.srt"),
        "music_rhythm_summary": str(paths.reports / "music_rhythm_summary.json"),
        "contact_sheet": str(paths.assets / "contact_sheet.jpg"),
        "keyframes": str(paths.keyframes),
        "project_manifest": str(paths.manifest),
    }
    return AnalysisReport(
        project_id=media.project_id,
        profile=media.analysis_profile,
        summary=f"{media.project_id} analyzed as a {profile} video with {len(shots)} estimated shots, {len(transcript)} transcript segments, and {len(beats)} rhythm peaks.",
        technical={
            "duration_seconds": media.duration_seconds,
            "duration": format_clock(media.duration_seconds),
            "frame_rate": media.frame_rate,
            "resolution": media.resolution,
            "aspect_ratio": media.aspect_ratio,
        },
        visual_observations=visual,
        audio_observations=audio,
        rhythm_observations=rhythm,
        client_takeaways=takeaways,
        artifacts=artifacts,
    )


def _pace_label(avg_shot: float) -> str:
    if avg_shot < 3:
        return "fast"
    if avg_shot < 7:
        return "controlled"
    return "slow"


def _profile_specific_rhythm(profile: str, avg_shot: float, beat_rate: float) -> str:
    if profile in {"ads", "shortform"}:
        return "The analysis prioritizes opening hook, beat alignment, and CTA-ready pacing."
    if profile == "festival":
        return "The analysis prioritizes concept clarity, mood continuity, and audiovisual intent."
    return "The analysis prioritizes scene flow, continuity, and emotional energy."


def _takeaways(profile: str, avg_shot: float, beat_rate: float, transcript: list[TranscriptSegment]) -> list[str]:
    takeaways = []
    if profile in {"ads", "shortform"}:
        takeaways.append("Review the first 3-5 seconds against the strongest visual and audio peaks.")
        takeaways.append("Check whether brand, product, or topic recognition appears before viewer attention drops.")
    else:
        takeaways.append("Review scene grouping against actual narrative or emotional turns.")
        takeaways.append("Check whether recurring motifs are intentional enough to name in a client deck.")
    if not transcript:
        takeaways.append("No usable transcript was produced; run ASR again or import subtitles before final client delivery.")
    if beat_rate < 20:
        takeaways.append("Rhythm peak density is low; verify whether that is intentional restraint or a pacing issue.")
    elif beat_rate > 120:
        takeaways.append("Rhythm peak density is high; verify that edits and sound hits do not flatten emphasis.")
    return takeaways


def render_html_report(
    report: AnalysisReport,
    media: CanonicalMediaPackage,
    shots: list[Shot],
    scenes: list[Scene],
    transcript: list[TranscriptSegment],
    beats: list[BeatEvent],
    music: list[MusicProfile],
    path: Path,
) -> None:
    zh = _localized_report(report, media, shots, scenes, transcript, beats, music)
    css = """
    :root { color-scheme: dark; --ink:#f3f3f0; --text:#c9c9c3; --muted:#777771; --line:#252525; --line2:#3b3b3b; --paper:#050505; --panel:#111; --panel2:#171717; --accent:#f2f2ed; --soft:#d7d7d0; }
    * { box-sizing: border-box; }
    html { scroll-behavior:smooth; }
    body { margin:0; font-family:"Helvetica Neue","PingFang SC","Hiragino Sans GB","Noto Sans CJK SC","Microsoft YaHei",Arial,sans-serif; background:linear-gradient(135deg,#050505,#0b0b0b 56%,#151515); color:var(--ink); -webkit-font-smoothing:antialiased; }
    main { max-width:1480px; margin:0 auto; padding:24px 22px 64px; }
    header { display:grid; grid-template-columns: 1fr auto; gap:24px; align-items:end; border:1px solid var(--line); border-radius:4px; padding:22px; background:linear-gradient(180deg,rgba(24,24,24,.88),rgba(10,10,10,.96)); box-shadow:0 28px 80px rgba(0,0,0,.36); }
    h1 { font-size:clamp(42px,7vw,92px); line-height:.88; margin:0; letter-spacing:-.075em; font-weight:850; text-transform:lowercase; }
    h2 { font-size:16px; margin:0 0 14px; letter-spacing:-.01em; }
    p { color:var(--text); line-height:1.55; }
    .meta { color:var(--muted); font-size:13px; display:flex; gap:8px; flex-wrap:wrap; margin-top:14px; }
    .meta span, .badge, .lang button, .nav a { border:1px solid var(--line); background:#080808; padding:7px 9px; border-radius:999px; }
    .badge { color:var(--accent); text-transform:uppercase; letter-spacing:.1em; font-size:11px; }
    .topbar { position:sticky; top:0; z-index:10; display:flex; align-items:center; justify-content:space-between; gap:16px; padding:10px 0 14px; background:linear-gradient(180deg,#050505 0%,rgba(5,5,5,.86) 78%,transparent); }
    .nav { display:flex; gap:8px; flex-wrap:wrap; }
    .nav a { color:var(--muted); text-decoration:none; font:700 11px/1 "Helvetica Neue",Arial,sans-serif; text-transform:uppercase; letter-spacing:.09em; }
    .nav a:hover { color:var(--ink); border-color:var(--line2); }
    .lang { display:flex; gap:8px; justify-content:flex-end; }
    .lang button { cursor:pointer; color:var(--muted); font:700 11px/1 "Helvetica Neue",Arial,sans-serif; text-transform:uppercase; letter-spacing:.08em; }
    .lang button.active { background:var(--soft); color:#050505; border-color:var(--soft); }
    .grid { display:grid; grid-template-columns: repeat(3, 1fr); gap:12px; margin:16px 0; }
    .panel { background:linear-gradient(180deg,rgba(24,24,24,.92),rgba(12,12,12,.96)); border:1px solid var(--line); border-radius:4px; padding:17px; box-shadow:0 20px 60px rgba(0,0,0,.26); }
    .wide { grid-column: span 2; }
    ul { padding-left:18px; margin:0; }
    li { margin:8px 0; color:var(--text); }
    .atlas { margin:16px 0; border:1px solid var(--line); border-radius:4px; overflow:hidden; background:#080808; }
    .atlasTop { min-height:260px; display:grid; grid-template-columns:1.1fr .9fr; gap:18px; padding:22px; align-items:end; background:linear-gradient(135deg,#050505,#111 62%,#1a1a1a); position:relative; }
    .atlasTop:after { content:""; position:absolute; inset:0; background:repeating-linear-gradient(90deg,rgba(255,255,255,.035) 0 1px,transparent 1px 110px); opacity:.5; pointer-events:none; }
    .atlasTop > * { position:relative; z-index:1; }
    .atlasLabel { color:var(--soft); font-size:11px; letter-spacing:.16em; text-transform:uppercase; }
    .atlasTitle { margin:8px 0 0; font-size:clamp(58px,11vw,148px); line-height:.82; letter-spacing:-.09em; text-transform:uppercase; }
    .atlasDeck { color:var(--text); font-size:16px; line-height:1.45; max-width:520px; justify-self:end; }
    .shotIndex { display:grid; }
    .shotItem { display:grid; grid-template-columns:92px 170px minmax(240px,1fr) 150px 150px; gap:14px; align-items:center; min-height:104px; padding:12px 16px; border-top:1px solid var(--line); text-decoration:none; color:var(--ink); transition:background .28s ease, transform .28s ease; animation:rise .6s ease both; }
    .shotItem:hover { background:#181818; transform:translateX(5px); }
    .shotNo { font-size:22px; letter-spacing:-.04em; }
    .shotThumb { width:168px; aspect-ratio:2.39/1; object-fit:cover; border:1px solid var(--line2); filter:saturate(.82) contrast(1.06); transition:transform .35s ease, filter .35s ease; }
    .shotItem:hover .shotThumb { transform:scale(1.035); filter:saturate(1) contrast(1.12); }
    .shotName { font-size:18px; letter-spacing:-.03em; }
    .shotMeta { color:var(--muted); font-size:12px; line-height:1.45; }
    @keyframes rise { from { opacity:0; transform:translateY(12px); } to { opacity:1; transform:translateY(0); } }
    .tablepanel { padding:0; overflow:visible; }
    .tablehead { display:flex; justify-content:space-between; gap:16px; align-items:end; padding:16px 17px; border-bottom:1px solid var(--line); background:#101010; }
    .tablehead p { margin:5px 0 0; color:var(--muted); font-size:12px; }
    .tablewrap { overflow-x:auto; overflow-y:visible; }
    table { min-width:2100px; width:100%; border-collapse:separate; border-spacing:0; font-size:15px; background:#0d0d0d; }
    th, td { border-bottom:1px solid var(--line); border-right:1px solid rgba(37,37,37,.82); text-align:left; padding:14px 12px; vertical-align:top; }
    th { position:sticky; top:58px; z-index:3; color:var(--soft); background:#151515; font-weight:700; text-transform:uppercase; letter-spacing:.06em; font-size:12px; }
    td { color:var(--text); line-height:1.55; }
    tr:hover td { background:#171717; }
    td:first-child, th:first-child { position:sticky; left:0; z-index:2; background:#111; color:var(--ink); }
    th:first-child { z-index:4; background:#151515; }
    .contact { width:100%; border:1px solid var(--line); border-radius:6px; display:block; background:#090805; }
    .thumb { width:240px; aspect-ratio:2.39/1; object-fit:cover; border:1px solid var(--line2); border-radius:3px; display:block; background:#050504; }
    .small { color:var(--muted); font-size:13px; }
    .note { color:var(--muted); font-size:12px; padding:12px 17px; border-top:1px solid var(--line); margin:0; background:#101010; }
    .zh { display:none; }
    body[data-lang="zh"] .en { display:none; }
    body[data-lang="zh"] .zh { display:revert; }
    body[data-lang="en"] .en { display:revert; }
    body[data-lang="en"] .zh { display:none; }
    @media (max-width: 860px) { header, .grid, .atlasTop { grid-template-columns:1fr; } .wide { grid-column:auto; } main { padding:22px 16px 48px; } .atlasDeck{justify-self:start}.shotItem{grid-template-columns:1fr;gap:8px}.shotThumb{width:100%} }
    """
    contact = Path(report.artifacts["contact_sheet"])
    contact_src = f"../assets/{contact.name}"
    rows = "\n".join(
        _storyboard_row(shot, zh=False)
        for shot in shots[:30]
    )
    rows_zh = "\n".join(
        _storyboard_row(shot, zh=True)
        for shot in shots[:30]
    )
    atlas_rows = "\n".join(_shot_atlas_item(shot, zh=False, index=index) for index, shot in enumerate(shots[:24], start=1))
    atlas_rows_zh = "\n".join(_shot_atlas_item(shot, zh=True, index=index) for index, shot in enumerate(shots[:24], start=1))
    body = f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(report.project_id)} Report</title><style>{css}</style></head>
<body data-lang="en"><main>
<div class="topbar"><nav class="nav"><a href="/"><span class="en">Index</span><span class="zh">引导页</span></a><a href="/projects/{html.escape(report.project_id)}"><span class="en">Project</span><span class="zh">项目页</span></a><a href="#shot-board"><span class="en">Shot Board</span><span class="zh">分镜表</span></a></nav><div class="lang"><button type="button" data-set-lang="en" class="active">EN</button><button type="button" data-set-lang="zh">中文</button></div></div>
<header><div><h1>{html.escape(report.project_id)}</h1><p class="en">{html.escape(report.summary)}</p><p class="zh">{html.escape(zh["summary"])}</p><div class="meta"><span>{html.escape(report.profile.value)}</span><span>{html.escape(report.technical["duration"])}</span><span>{html.escape(str(report.technical["resolution"]))}</span></div></div><div class="badge"><span class="en">Local Analysis MVP</span><span class="zh">本地分析 MVP</span></div></header>
<section class="grid">
<article class="panel"><h2><span class="en">Visual</span><span class="zh">画面</span></h2><ul class="en">{''.join(f'<li>{html.escape(item)}</li>' for item in report.visual_observations)}</ul><ul class="zh">{''.join(f'<li>{html.escape(item)}</li>' for item in zh["visual"])}</ul></article>
<article class="panel"><h2><span class="en">Audio</span><span class="zh">声音</span></h2><ul class="en">{''.join(f'<li>{html.escape(item)}</li>' for item in report.audio_observations)}</ul><ul class="zh">{''.join(f'<li>{html.escape(item)}</li>' for item in zh["audio"])}</ul></article>
<article class="panel"><h2><span class="en">Rhythm</span><span class="zh">节奏</span></h2><ul class="en">{''.join(f'<li>{html.escape(item)}</li>' for item in report.rhythm_observations)}</ul><ul class="zh">{''.join(f'<li>{html.escape(item)}</li>' for item in zh["rhythm"])}</ul></article>
<article class="panel wide"><h2><span class="en">Client Takeaways</span><span class="zh">客户结论</span></h2><ul class="en">{''.join(f'<li>{html.escape(item)}</li>' for item in report.client_takeaways)}</ul><ul class="zh">{''.join(f'<li>{html.escape(item)}</li>' for item in zh["takeaways"])}</ul></article>
<article class="panel"><h2><span class="en">Contact Sheet</span><span class="zh">画面联系表</span></h2><img class="contact" src="{html.escape(contact_src)}" alt="Contact sheet"></article>
</section>
<section class="atlas en"><div class="atlasTop"><div><div class="atlasLabel">Index ({len(shots)})</div><div class="atlasTitle">Shot<br>Atlas</div></div><p class="atlasDeck">A cinematic visual index generated from the analysis output. Every analyzed shot becomes a navigable frame record with timecode, image, content, scale, movement, and review state.</p></div><div class="shotIndex">{atlas_rows}</div></section>
<section class="atlas zh"><div class="atlasTop"><div><div class="atlasLabel">索引 ({len(shots)})</div><div class="atlasTitle">Shot<br>Atlas</div></div><p class="atlasDeck">由分析结果自动生成的影像索引。每个被解析的镜头都会成为一个画面记录，包含时码、截图、内容、景别、运镜和复核状态。</p></div><div class="shotIndex">{atlas_rows_zh}</div></section>
<section id="shot-board" class="panel tablepanel en"><div class="tablehead"><div><h2>Shot Analysis Board</h2><p>Industrial shot-by-shot review table. One row equals one screen image / edit unit.</p></div><span class="badge">Film workstation</span></div><div class="tablewrap"><table><thead><tr><th>Shot</th><th>Panel</th><th>TC / Dur.</th><th>Content</th><th>Scene Type</th><th>Shot Size</th><th>Angle</th><th>Movement</th><th>Composition</th><th>Dialogue / Sound</th><th>Music / Rhythm</th><th>Video Prompt</th><th>Review</th></tr></thead><tbody>{rows}</tbody></table></div><p class="note">TapNow parity requires vision annotation. Run `analyze-video vision PROJECT_ID` with OPENAI_API_KEY to fill content, scene type, shot size, angle, movement, composition, and reusable prompts.</p></section>
<section id="shot-board-zh" class="panel tablepanel zh"><div class="tablehead"><div><h2>工业分镜头解析表</h2><p>每一行对应一个画面/剪辑单位，用于审片、复盘、提示词再生成。</p></div><span class="badge">影视工作台</span></div><div class="tablewrap"><table><thead><tr><th>镜头</th><th>画面</th><th>时码/时长</th><th>内容</th><th>场景类型</th><th>景别</th><th>角度</th><th>运镜</th><th>构图</th><th>对白/声音</th><th>音乐/节奏</th><th>视频提示词</th><th>复核</th></tr></thead><tbody>{rows_zh}</tbody></table></div><p class="note">要达到 TapNow 级别，必须运行视觉标注。设置 OPENAI_API_KEY 后执行 `analyze-video vision PROJECT_ID`，即可填充内容、场景类型、景别、角度、运镜、构图和可复用视频提示词。</p></section>
</main><script>
const buttons = document.querySelectorAll('[data-set-lang]');
function setLang(lang) {{
  document.body.dataset.lang = lang;
  document.documentElement.lang = lang === 'zh' ? 'zh-CN' : 'en';
  buttons.forEach(btn => btn.classList.toggle('active', btn.dataset.setLang === lang));
  localStorage.setItem('video-analysis-report-lang', lang);
}}
buttons.forEach(btn => btn.addEventListener('click', () => setLang(btn.dataset.setLang)));
setLang(localStorage.getItem('video-analysis-report-lang') || 'en');
</script></body></html>"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _localized_report(
    report: AnalysisReport,
    media: CanonicalMediaPackage,
    shots: list[Shot],
    scenes: list[Scene],
    transcript: list[TranscriptSegment],
    beats: list[BeatEvent],
    music: list[MusicProfile],
) -> dict[str, list[str] | str]:
    avg_shot = sum(shot.duration for shot in shots) / max(len(shots), 1)
    beat_rate = len(beats) / max(media.duration_seconds, 1.0) * 60
    music_energy = _zh_value(music[0].energy_level if music else "unknown")
    music_tempo = _zh_value(music[0].tempo_bucket if music else "unknown")
    return {
        "summary": f"{report.project_id} 已按 {report.profile.value} 类型完成分析：估算 {len(shots)} 个镜头、{len(transcript)} 段字幕/对白、{len(beats)} 个节奏峰值。",
        "visual": [
            f"估算 {len(shots)} 个镜头，归并为 {len(scenes)} 个场景段落。",
            f"平均镜头时长 {avg_shot:.1f} 秒，节奏判断为{_zh_value(_pace_label(avg_shot))}。",
            "镜头标签为首轮机器估算，客户交付前应进行人工复核。",
        ],
        "audio": [
            f"生成 {len(transcript)} 段字幕/对白。",
            f"音乐轮廓为{music_energy}能量、{music_tempo}速度。",
            f"全片检测到 {len(beats)} 个节奏峰值。",
        ],
        "rhythm": [
            f"估算节奏密度为每分钟 {beat_rate:.1f} 个峰值。",
            _zh_value(_profile_specific_rhythm(report.profile.value, avg_shot, beat_rate)),
        ],
        "takeaways": [_zh_value(item) for item in report.client_takeaways],
    }


def _zh_value(value: str) -> str:
    mapping = {
        "unknown": "待复核",
        "TBD": "待定",
        "to annotate": "待标注",
        "to annotate from frame": "待根据画面标注",
        "not inferable from final video": "无法仅凭成片可靠反推",
        "fast": "快",
        "controlled": "可控",
        "slow": "慢",
        "medium": "中等",
        "high": "高",
        "low": "低",
        "wide": "远景/全景",
        "close-up": "近景/特写",
        "detail": "细节",
        "static": "固定镜头",
        "eye-level": "平视",
        "low angle": "低机位",
        "high angle": "高机位",
        "overhead/graphic": "俯拍/图形化",
        "slow movement": "缓慢运动",
        "reframe": "重新构图",
        "push-in": "推进",
        "handheld/kinetic": "手持/动态",
        "cutaway": "插入/切出",
        "center-weighted": "中心构图",
        "subject-led": "主体主导",
        "graphic/insert": "图形/插入",
        "environment-led": "环境主导",
        "music-led": "音乐主导",
        "sync": "同期/同步",
        "tripod": "三脚架",
        "dolly/gimbal": "轨道/稳定器",
        "locked-off": "锁定机位",
        "handheld": "手持",
        "gimbal": "稳定器",
        "macro/insert rig": "微距/插入镜头设备",
        "review required": "需要复核",
        "machine segmented; visual fields require human/model annotation": "机器分段；画面字段需要人工或视觉模型标注",
        "to annotate blocking, screen direction, and action beat": "待标注调度、画面方向与动作节拍",
        "to annotate lighting, VFX, and AI artifacts": "待标注灯光、VFX 与 AI 痕迹",
        "audio/rhythm detected; dialogue requires ASR or subtitle import": "已检测音频/节奏；对白需 ASR 或字幕导入",
        "vision annotated; review before final client delivery": "已视觉标注；客户交付前请复核",
        "review blocking and screen direction": "复核调度与画面方向",
        "review practical light, VFX, and AI artifacts": "复核实际光源、VFX 与 AI 痕迹",
        "review dialogue, music, and SFX relationship": "复核对白、音乐与音效关系",
        "pending audio sync": "等待音频同步",
        "first-pass generated row; review required": "首轮生成行，需要人工复核",
        "dense rhythm peaks; check edit/music alignment": "节奏峰值密集，检查剪辑与音乐卡点",
        "moderate rhythm activity": "中等节奏活动",
        "sparse rhythm activity": "节奏活动较稀疏",
        "Review the frame for hook, product/topic visibility, and edit emphasis.": "复核该画面的开场抓力、产品/主题可见度与剪辑强调。",
        "Review the frame for concept, motif, mood, and visual continuity.": "复核该画面的概念、母题、情绪与视觉连续性。",
        "Review the frame for scene function, subject action, and continuity.": "复核该画面的场景功能、主体动作与连续性。",
        "The analysis prioritizes opening hook, beat alignment, and CTA-ready pacing.": "本分析优先关注开场抓力、音乐卡点与 CTA 前后的节奏组织。",
        "The analysis prioritizes concept clarity, mood continuity, and audiovisual intent.": "本分析优先关注概念清晰度、情绪连续性与音画意图。",
        "The analysis prioritizes scene flow, continuity, and emotional energy.": "本分析优先关注场景流动、连续性与情绪能量。",
        "Review scene grouping against actual narrative or emotional turns.": "对照实际叙事或情绪转折复核场景分组。",
        "Check whether recurring motifs are intentional enough to name in a client deck.": "检查重复母题是否足够明确，能否写入客户提案或复盘文档。",
        "No usable transcript was produced; run ASR again or import subtitles before final client delivery.": "当前未生成可用字幕；正式交付前应重新运行 ASR 或导入字幕。",
        "Rhythm peak density is low; verify whether that is intentional restraint or a pacing issue.": "节奏峰值密度偏低；需判断这是有意克制还是节奏问题。",
        "Rhythm peak density is high; verify that edits and sound hits do not flatten emphasis.": "节奏峰值密度偏高；需检查剪辑和声音重音是否削弱重点。",
        "Review the first 3-5 seconds against the strongest visual and audio peaks.": "用最强视觉点和声音峰值复核前 3-5 秒的开场抓力。",
        "Check whether brand, product, or topic recognition appears before viewer attention drops.": "检查品牌、产品或主题识别是否在注意力下降前出现。",
    }
    return mapping.get(value, value)


def _storyboard_row(shot: Shot, zh: bool) -> str:
    def v(value: str) -> str:
        return _zh_value(value) if zh else value

    thumb = f"../assets/keyframes/{shot.frame_ref}" if shot.frame_ref else "../assets/contact_sheet.jpg"
    tc = f"{shot.timecode}<br><span class='small'>{shot.duration:.1f}s</span>"
    shot_label = f"{shot.scene_no}-{shot.shot_no}<br><span class='small'>{html.escape(shot.setup_id)}</span>"
    content = v(shot.content_summary or shot.visual_description)
    sound = f"{html.escape(shot.dialogue or shot.speech_summary)}<br><span class='small'>{v(shot.sound_sync)} / {v(shot.audio_notes)}</span>"
    rhythm = f"{v(shot.music_state)}<br><span class='small'>{shot.beat_density:.2f} / {v(shot.rhythm_notes)}</span>"
    prompt = shot.prompt_zh if zh and shot.prompt_zh else shot.prompt_en
    if not prompt:
        prompt = "run vision annotation to generate prompt" if not zh else "运行视觉标注后生成提示词"
    notes = f"{v(shot.review_notes)}<br><span class='small'>{v(shot.style_notes or shot.continuity_notes)}</span>"
    cells = [
        shot_label,
        f"<img class='thumb' src='{html.escape(thumb)}' alt='frame {shot.shot_no}'>",
        tc,
        content,
        v(shot.scene_type or "to annotate"),
        v(shot.shot_scale),
        v(shot.camera_angle),
        v(shot.camera_motion),
        v(shot.composition),
        sound,
        rhythm,
        prompt,
        notes,
    ]
    return f'<tr id="shot-{html.escape(str(shot.shot_no))}">' + "".join(
        f"<td>{cell if '<' in str(cell) else html.escape(str(cell))}</td>" for cell in cells
    ) + "</tr>"


def _shot_atlas_item(shot: Shot, zh: bool, index: int) -> str:
    def v(value: str) -> str:
        return _zh_value(value) if zh else value

    thumb = f"../assets/keyframes/{shot.frame_ref}" if shot.frame_ref else "../assets/contact_sheet.jpg"
    title = v(shot.content_summary or shot.visual_description or "to annotate from frame")
    scene_type = v(shot.scene_type or "to annotate")
    shot_scale = v(shot.shot_scale)
    camera_motion = v(shot.camera_motion)
    review = v(shot.review_notes)
    style = f"animation-delay:{min(index * 0.025, 0.6):.3f}s"
    return (
        f'<a class="shotItem" style="{style}" href="#shot-{html.escape(str(shot.shot_no))}">'
        f'<div><div class="shotNo">{html.escape(str(shot.shot_no).zfill(2))}</div><div class="shotMeta">{html.escape(shot.timecode)} / {shot.duration:.1f}s</div></div>'
        f'<img class="shotThumb" src="{html.escape(thumb)}" alt="shot {html.escape(str(shot.shot_no))}">'
        f'<div><div class="shotName">{html.escape(title)}</div><div class="shotMeta">{html.escape(scene_type)}</div></div>'
        f'<div class="shotMeta">{html.escape(shot_scale)}<br>{html.escape(camera_motion)}</div>'
        f'<div class="shotMeta">{html.escape(review)}</div>'
        "</a>"
    )


def render_pdf_report(html_path: Path, pdf_path: Path) -> None:
    # wkhtmltopdf is not guaranteed locally; keep a deterministic PDF-like text artifact fallback.
    if __import__("shutil").which("wkhtmltopdf"):
        run_command(["wkhtmltopdf", str(html_path), str(pdf_path)], timeout=120)
        return
    content = html_path.read_text(encoding="utf-8")
    text = "PDF renderer not installed. Open report.html for the designed report.\n\n" + content
    pdf_path.write_text(text, encoding="utf-8")
