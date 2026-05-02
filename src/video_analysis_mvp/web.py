from __future__ import annotations

import html
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from .config import load_runtime_config, mask_secret, save_runtime_config
from .pipeline import run_full_pipeline, run_report, run_vision
from .schemas import AnalysisProfile
from .store import find_projects, workspace_path


INDEX_CSS = """
:root{color-scheme:dark;--bg:#050505;--bg2:#0b0b0b;--panel:#111;--panel2:#171717;--rail:#080808;--ink:#f3f3f0;--text:#c9c9c3;--muted:#777771;--line:#252525;--line2:#3b3b3b;--accent:#f2f2ed;--soft:#d7d7d0;--black:#020202}
*{box-sizing:border-box}body{margin:0;background:linear-gradient(135deg,var(--bg),#0b0b0b 54%,#151515);color:var(--ink);font-family:"Helvetica Neue","PingFang SC","Hiragino Sans GB","Noto Sans CJK SC","Microsoft YaHei",Arial,sans-serif;-webkit-font-smoothing:antialiased}a{color:inherit}
.shell{min-height:100vh;display:grid;grid-template-columns:360px 1fr}.side{position:sticky;top:0;height:100vh;border-right:1px solid var(--line);padding:22px;background:linear-gradient(180deg,var(--rail),#101010);overflow:auto}.main{padding:24px 28px 56px}.brand{font-size:22px;line-height:1;margin:0 0 10px;letter-spacing:.02em}.kicker,label{font-size:11px;color:var(--soft);text-transform:uppercase;letter-spacing:.16em}.muted{color:var(--muted);font-size:13px;line-height:1.55}.panel{background:linear-gradient(180deg,rgba(24,24,24,.92),rgba(12,12,12,.96));border:1px solid var(--line);border-radius:4px;padding:18px;box-shadow:0 24px 60px rgba(0,0,0,.32)}.form{display:grid;gap:13px;margin-top:22px}input,select{width:100%;border:1px solid var(--line);background:#070707;color:var(--ink);padding:12px;border-radius:3px;font:inherit;outline:none}input:focus,select:focus{border-color:var(--soft);box-shadow:0 0 0 1px rgba(255,255,255,.12)}input::placeholder{color:#62625d}button{border:1px solid var(--soft);background:var(--soft);color:#050505;padding:12px 14px;border-radius:3px;font:700 12px/1 "Helvetica Neue",Arial,sans-serif;cursor:pointer;text-transform:uppercase;letter-spacing:.1em}button.secondary{background:#101010;color:var(--ink);border-color:var(--line2)}button:hover{filter:brightness(1.08)}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.metric{padding:16px;border:1px solid var(--line);background:rgba(18,18,18,.78);border-radius:4px}.metric b{font-size:25px;letter-spacing:-.02em}.projects{display:grid;gap:10px;margin-top:18px}.project{display:grid;grid-template-columns:1fr auto;gap:12px;align-items:center;padding:14px;border:1px solid var(--line);border-radius:4px;background:rgba(16,16,16,.82);text-decoration:none}.project:hover{border-color:var(--soft);background:#191919}.pill{font-size:11px;border:1px solid var(--line2);padding:5px 8px;border-radius:999px;color:var(--soft);text-transform:uppercase;letter-spacing:.08em}.hero{display:grid;grid-template-columns:minmax(0,1fr) 420px;gap:16px;margin-bottom:18px;align-items:stretch}.hero h1{font-size:38px;line-height:1.02;margin:0 0 12px;letter-spacing:-.045em;max-width:720px;font-weight:800}.hero .panel{display:flex;flex-direction:column;justify-content:space-between}.bars{height:220px;border:1px solid var(--line);border-radius:4px;background:linear-gradient(180deg,#151515,#050505);display:flex;align-items:end;gap:4px;padding:18px;position:relative;overflow:hidden}.bars:before{content:"";position:absolute;inset:0;background:repeating-linear-gradient(90deg,transparent 0 39px,rgba(255,255,255,.04) 40px),linear-gradient(180deg,transparent,rgba(0,0,0,.45));pointer-events:none}.bar{position:relative;z-index:1;flex:1;background:linear-gradient(180deg,#efefea,#8f8f89);min-height:14px}.bar:nth-child(3n){background:linear-gradient(180deg,#b8b8b2,#62625d)}.bar:nth-child(4n){background:linear-gradient(180deg,#777,#333)}.lang{display:flex;gap:8px;margin:0 0 20px}.lang button{background:#080808;color:var(--muted);border:1px solid var(--line);padding:7px 10px;font-size:11px}.lang button.active{background:var(--soft);color:#050505;border-color:var(--soft)}.zh{display:none}body[data-lang="zh"] .en{display:none}body[data-lang="zh"] .zh{display:revert}body[data-lang="en"] .en{display:revert}body[data-lang="en"] .zh{display:none}
.opbar{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:16px}.opbar .muted{max-width:760px}.contact{width:100%;padding:0;display:block;background:#050505}.statusline{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}.statusline span{border:1px solid var(--line);border-radius:999px;padding:6px 9px;color:var(--text);font-size:12px;background:#0d0d0d}.guideLink{display:inline-flex;align-items:center;gap:8px;margin-bottom:14px;color:var(--soft);font-size:12px;text-decoration:none;text-transform:uppercase;letter-spacing:.1em}
@media(max-width:980px){.shell{grid-template-columns:1fr}.side{position:relative;height:auto;border-right:0;border-bottom:1px solid var(--line)}.hero,.grid{grid-template-columns:1fr}.main{padding:18px 14px 44px}.hero h1{font-size:30px}}
"""


def serve(host: str, port: int, workspace: str | None) -> None:
    root = workspace_path(workspace)

    class Handler(BaseHTTPRequestHandler):
        def do_HEAD(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path.startswith("/files/"):
                self._file(root, unquote(parsed.path.split("/", 2)[2]), body=False)
                return
            payload = b""
            self.send_response(200 if parsed.path in {"/", "/settings"} or parsed.path.startswith("/projects/") else 404)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self._html(render_index(root))
                return
            if parsed.path == "/settings":
                saved = parse_qs(parsed.query).get("saved", ["0"])[0] == "1"
                self._html(render_settings(root, saved=saved))
                return
            if parsed.path.startswith("/projects/"):
                project_id = unquote(parsed.path.split("/", 2)[2])
                self._html(render_project(root, project_id))
                return
            if parsed.path.startswith("/files/"):
                self._file(root, unquote(parsed.path.split("/", 2)[2]))
                return
            self.send_error(404)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            length = int(self.headers.get("content-length", "0"))
            body = self.rfile.read(length).decode("utf-8")
            data = {k: v[0] for k, v in parse_qs(body).items()}
            if parsed.path == "/analyze":
                source = data.get("source", "").strip()
                profile = AnalysisProfile(data.get("profile", "ads"))
                password = data.get("password") or None
                language = data.get("language") or "auto"
                skip_asr = data.get("skip_asr") == "on"
                result_box: dict[str, object] = {}

                def work() -> None:
                    result_box["result"] = run_full_pipeline(
                        source,
                        profile=profile,
                        password=password,
                        workspace=str(root),
                        language=language,
                        skip_asr=skip_asr,
                    )

                thread = threading.Thread(target=work)
                thread.start()
                thread.join()
                result = result_box["result"]
                project_id = Path(result.artifacts["project_manifest"]).parent.name  # type: ignore[attr-defined]
                self.send_response(303)
                self.send_header("Location", f"/projects/{quote(project_id)}")
                self.end_headers()
                return
            if parsed.path == "/settings":
                save_runtime_config(
                    root,
                    {
                        "vision_provider": data.get("vision_provider", ""),
                        "openai_api_key": data.get("openai_api_key", ""),
                        "openai_base_url": data.get("openai_base_url", ""),
                        "openai_model": data.get("openai_model", ""),
                        "minimax_api_key": data.get("minimax_api_key", ""),
                        "minimax_api_host": data.get("minimax_api_host", ""),
                    },
                )
                self.send_response(303)
                self.send_header("Location", "/settings?saved=1")
                self.end_headers()
                return
            if parsed.path.startswith("/regenerate/"):
                project_id = unquote(parsed.path.split("/", 2)[2])
                run_report(project_id, workspace=str(root))
                self.send_response(303)
                self.send_header("Location", f"/projects/{quote(project_id)}")
                self.end_headers()
                return
            if parsed.path.startswith("/vision/"):
                project_id = unquote(parsed.path.split("/", 2)[2])
                provider = data.get("vision_provider") or None
                run_vision(project_id, workspace=str(root), provider=provider)
                run_report(project_id, workspace=str(root))
                self.send_response(303)
                self.send_header("Location", f"/projects/{quote(project_id)}")
                self.end_headers()
                return
            self.send_error(404)

        def log_message(self, format: str, *args) -> None:
            return

        def _html(self, content: str) -> None:
            payload = content.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _file(self, workspace_root: Path, requested: str, body: bool = True) -> None:
            path = (workspace_root / requested).resolve()
            if not str(path).startswith(str(workspace_root.resolve())) or not path.exists():
                self.send_error(404)
                return
            payload = path.read_bytes()
            content_type = "text/html" if path.suffix == ".html" else "application/octet-stream"
            if path.suffix == ".jpg":
                content_type = "image/jpeg"
            if path.suffix == ".json":
                content_type = "application/json"
            if path.suffix == ".csv":
                content_type = "text/csv"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if body:
                self.wfile.write(payload)

    print(f"Serving video analysis UI at http://{host}:{port}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


def render_index(root: Path) -> str:
    projects = find_projects(str(root))
    cards = "\n".join(
        f'<a class="project" href="/projects/{quote(p.project_id)}"><span><b>{html.escape(p.project_id)}</b><br><span class="muted">{html.escape(p.source)}</span></span><span class="pill">{html.escape(p.status)}</span></a>'
        for p in projects
    )
    bars = "".join(f'<div class="bar" style="height:{height}%"></div>' for height in [30, 62, 46, 78, 52, 90, 38, 64, 72, 44, 84, 58])
    return page(
        f"""
<div class="shell">
<aside class="side"><div class="lang"><button type="button" data-set-lang="en" class="active">EN</button><button type="button" data-set-lang="zh">中文</button></div><h2 class="brand"><span class="en">Video Analysis MVP</span><span class="zh">视频分析 MVP</span></h2><p class="muted"><span class="en">Local shot, speech, rhythm, and client report engine.</span><span class="zh">本地分镜、对白、音乐节奏与客户报告引擎。</span></p><a class="guideLink" href="/settings"><span class="en">Settings</span><span class="zh">设置</span></a>
<form class="form" method="post" action="/analyze">
<div><label><span class="en">Video file path or URL</span><span class="zh">视频文件路径或链接</span></label><input name="source" required placeholder="/path/to/video.mp4 or https://..."></div>
<div><label><span class="en">Profile</span><span class="zh">分析类型</span></label><select name="profile"><option value="ads">Ads / 广告</option><option value="shortform">Shortform / 短视频</option><option value="streaming">Streaming / 流媒体</option><option value="festival">Festival / 电影节</option></select></div>
<div><label><span class="en">Password</span><span class="zh">密码</span></label><input name="password" placeholder="Optional"></div>
<div><label><span class="en">Language</span><span class="zh">语言</span></label><input name="language" placeholder="auto, Chinese, English"></div>
<label style="display:flex;gap:8px;align-items:center;text-transform:none;letter-spacing:0;color:var(--ink)"><input style="width:auto" type="checkbox" name="skip_asr" checked> <span class="en">Skip ASR for fast first pass</span><span class="zh">跳过转写，先做快速分析</span></label>
<button type="submit"><span class="en">Run Analysis</span><span class="zh">开始分析</span></button>
</form></aside>
<main class="main"><section class="hero"><div class="panel"><div><div class="kicker"><span class="en">Film Analysis Workbench</span><span class="zh">影视分析工作台</span></div><h1><span class="en">Local shot board, transcript, rhythm map, and delivery report.</span><span class="zh">本地分镜表、字幕、节奏图与客户交付报告。</span></h1><p class="muted"><span class="en">Designed for focused review: import, analyze, annotate, regenerate.</span><span class="zh">面向专注审片：导入、分析、标注、重新生成。</span></p></div><div class="statusline"><span>Shot board</span><span>ASR</span><span>Music rhythm</span><span>Vision annotation</span></div></div><div class="bars">{bars}</div></section>
<section class="grid"><div class="metric"><b>{len(projects)}</b><br><span class="muted"><span class="en">Projects</span><span class="zh">项目</span></span></div><div class="metric"><b>Local</b><br><span class="muted"><span class="en">Processing mode</span><span class="zh">本地处理</span></span></div><div class="metric"><b>JSON</b><br><span class="muted"><span class="en">Review loop</span><span class="zh">复核闭环</span></span></div></section>
<section class="projects">{cards or '<div class="panel muted">No projects yet.</div>'}</section></main></div>
"""
    )


def render_project(root: Path, project_id: str) -> str:
    project = root / project_id
    manifest_path = project / "project_manifest.json"
    if not manifest_path.exists():
        return page("<main class='main'><div class='panel'>Project not found.</div></main>")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    report_rel = _rel(root, project / "reports" / "report.html")
    contact_rel = _rel(root, project / "assets" / "contact_sheet.jpg")
    links = [
        ("Designed report / 设计报告", report_rel),
        ("Overview PDF / 总览 PDF", _rel(root, project / "reports" / "overview.pdf")),
        ("Shot CSV / 分镜表 CSV", _rel(root, project / "reports" / "shot_breakdown.csv")),
        ("Transcript SRT / 字幕 SRT", _rel(root, project / "reports" / "transcript.srt")),
        ("Rhythm JSON / 节奏 JSON", _rel(root, project / "reports" / "music_rhythm_summary.json")),
        ("Manifest / 项目清单", _rel(root, manifest_path)),
    ]
    link_html = "".join(f'<a class="project" href="/files/{quote(path)}"><span>{html.escape(label)}</span><span class="pill">open</span></a>' for label, path in links)
    return page(
        f"""
<div class="shell"><aside class="side"><div class="lang"><button type="button" data-set-lang="en" class="active">EN</button><button type="button" data-set-lang="zh">中文</button></div><h2 class="brand">{html.escape(project_id)}</h2><p class="muted">{html.escape(manifest.get('source',''))}</p><a class="guideLink" href="/settings"><span class="en">Settings</span><span class="zh">设置</span></a><form class="form" method="post" action="/vision/{quote(project_id)}"><div><label><span class="en">Vision provider</span><span class="zh">视觉模型</span></label><select name="vision_provider"><option value="openai">OpenAI compatible</option><option value="minimax_mcp">MiniMax MCP</option></select></div><button type="submit"><span class="en">Vision Annotate</span><span class="zh">视觉标注</span></button></form><form class="form" method="post" action="/regenerate/{quote(project_id)}"><button class="secondary" type="submit"><span class="en">Regenerate Report</span><span class="zh">重新生成报告</span></button></form><p class="muted"><span class="en">Configure keys in Settings. Vision annotation uploads keyframes to the selected provider.</span><span class="zh">在设置页配置 key。视觉标注会把关键帧上传到所选服务。</span></p></aside>
<main class="main"><a class="guideLink" href="/"><span class="en">← Guide / Project Index</span><span class="zh">← 引导页 / 项目索引</span></a><section class="hero"><div class="panel"><div><div class="kicker"><span class="en">Active Project</span><span class="zh">当前项目</span></div><h1><span class="en">Project Package</span><span class="zh">项目交付包</span></h1><p class="muted"><span class="en">Status</span><span class="zh">状态</span>: {html.escape(manifest.get('status','unknown'))}</p></div><div class="statusline"><span>Report</span><span>CSV</span><span>SRT</span><span>JSON</span></div></div><img class="panel contact" src="/files/{quote(contact_rel)}" alt="Contact sheet"></section><section class="projects">{link_html}</section></main></div>
"""
    )


def render_settings(root: Path, saved: bool = False) -> str:
    config = load_runtime_config(root)
    provider_openai = "selected" if config.vision_provider == "openai" else ""
    provider_minimax = "selected" if config.vision_provider == "minimax_mcp" else ""
    saved_html = (
        '<div class="panel"><b class="en">Settings saved.</b><b class="zh">设置已保存。</b></div>'
        if saved
        else ""
    )
    return page(
        f"""
<div class="shell">
<aside class="side"><div class="lang"><button type="button" data-set-lang="en" class="active">EN</button><button type="button" data-set-lang="zh">中文</button></div><h2 class="brand"><span class="en">Runtime Settings</span><span class="zh">运行设置</span></h2><p class="muted"><span class="en">Provider keys are stored locally in this workspace and never printed back in full.</span><span class="zh">模型 key 保存在当前 workspace 本地配置中，页面不会完整回显。</span></p><a class="guideLink" href="/"><span class="en">← Project Index</span><span class="zh">← 项目索引</span></a></aside>
<main class="main">{saved_html}<section class="hero"><div class="panel"><div><div class="kicker"><span class="en">Vision Provider</span><span class="zh">视觉模型配置</span></div><h1><span class="en">Configure once. Annotate every project.</span><span class="zh">配置一次，所有项目可用。</span></h1><p class="muted"><span class="en">MiniMax MCP is recommended for your China-region key. Use https://api.minimaxi.com.</span><span class="zh">中国区 MiniMax key 建议使用 MiniMax MCP，并设置 https://api.minimaxi.com。</span></p></div><div class="statusline"><span>MiniMax: {html.escape(mask_secret(config.minimax_api_key))}</span><span>OpenAI: {html.escape(mask_secret(config.openai_api_key))}</span></div></div><div class="panel"><p class="muted"><span class="en">Security note: this is a local developer MVP. The config file is not encrypted. Do not commit it.</span><span class="zh">安全提示：这是本地开发版，配置文件未加密。不要提交到 git。</span></p></div></section>
<form class="form panel" method="post" action="/settings" autocomplete="off">
<div><label><span class="en">Default provider</span><span class="zh">默认视觉模型</span></label><select name="vision_provider"><option value="openai" {provider_openai}>OpenAI compatible</option><option value="minimax_mcp" {provider_minimax}>MiniMax MCP</option></select></div>
<div class="grid">
<div><label>MiniMax API Key</label><input type="password" name="minimax_api_key" placeholder="{html.escape(mask_secret(config.minimax_api_key))}"></div>
<div><label>MiniMax API Host</label><input name="minimax_api_host" value="{html.escape(config.minimax_api_host)}" placeholder="https://api.minimaxi.com"></div>
<div><label><span class="en">China host</span><span class="zh">中国区 Host</span></label><input value="https://api.minimaxi.com" readonly></div>
</div>
<div class="grid">
<div><label>OpenAI API Key</label><input type="password" name="openai_api_key" placeholder="{html.escape(mask_secret(config.openai_api_key))}"></div>
<div><label>OpenAI Base URL</label><input name="openai_base_url" value="{html.escape(config.openai_base_url)}"></div>
<div><label>OpenAI Vision Model</label><input name="openai_model" value="{html.escape(config.openai_model)}"></div>
</div>
<button type="submit"><span class="en">Save Local Settings</span><span class="zh">保存本地设置</span></button>
</form></main></div>
"""
    )


def _rel(root: Path, path: Path) -> str:
    return str(path.resolve().relative_to(root.resolve()))


def page(content: str) -> str:
    script = """
<script>
const buttons = document.querySelectorAll('[data-set-lang]');
function setLang(lang){document.body.dataset.lang=lang;document.documentElement.lang=lang==='zh'?'zh-CN':'en';buttons.forEach(btn=>btn.classList.toggle('active',btn.dataset.setLang===lang));localStorage.setItem('video-analysis-ui-lang',lang);}
buttons.forEach(btn=>btn.addEventListener('click',()=>setLang(btn.dataset.setLang)));
setLang(localStorage.getItem('video-analysis-ui-lang') || 'en');
</script>
"""
    return f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'><title>Video Analysis MVP</title><style>{INDEX_CSS}</style></head><body data-lang='en'>{content}{script}</body></html>"
