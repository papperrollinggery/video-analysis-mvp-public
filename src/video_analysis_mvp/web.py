from __future__ import annotations

import html
import hmac
import ipaddress
import json
import mimetypes
import os
import secrets
import socket
import stat
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from .artifacts import PROFESSIONAL_EXPORT_IDS, artifact_path
from .config import load_runtime_config, mask_secret, save_runtime_config
from .delivery import _camera_text, _status_text, _story_beat_display
from .doctor import run_doctor
from .pipeline import run_full_pipeline
from .paths import resolve_project_root
from .readiness import evaluate_project_readiness
from .safe_io import advisory_file_lock
from .schemas import AnalysisProfile, Shot
from .store import find_projects, workspace_path
from .workspace_api import (
    ApiError,
    _deliverable_file_info,
    dispatch_api,
    regenerate_project_report,
    load_project_manifest,
    project_write_lock,
    read_project_json,
    is_professional_export_path,
    is_current_project_file,
    validated_project_root,
    write_project_json,
)


INDEX_CSS = """
:root{color-scheme:dark;--bg:#090a0a;--rail:#101211;--panel:#151716;--panel2:#0f1110;--ink:#f5f0e8;--text:#c9c0b3;--muted:#81796d;--line:#2b302d;--line2:#454b47;--green:#8fdc9b;--amber:#e4c06e;--red:#df806f;--blue:#9fc7ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:"Helvetica Neue","PingFang SC","Hiragino Sans GB",Arial,sans-serif;-webkit-font-smoothing:antialiased}a{color:inherit}
.shell{min-height:100vh;display:grid;grid-template-columns:300px minmax(0,1fr)}.side{position:sticky;top:0;height:100vh;overflow:auto;background:var(--rail);border-right:1px solid var(--line);padding:12px}.main{padding:12px 14px 32px}.topbar{display:flex;justify-content:space-between;gap:12px;align-items:center;margin-bottom:10px}.brand{font-size:21px;line-height:1;margin:6px 0 6px;font-weight:850}.brand small{display:block;margin-top:7px;color:var(--muted);font-size:11px;font-weight:500}.kicker,label{color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.12em}.muted{color:var(--muted);font-size:12px;line-height:1.45}.panel{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:10px}.runbox{border:1px solid var(--line);background:#0a0b0b;border-radius:7px;padding:11px}.form{display:grid;gap:8px}input,select,textarea{width:100%;border:1px solid var(--line2);background:#070808;color:var(--ink);padding:8px;border-radius:5px;font:inherit;outline:none}textarea{min-height:58px;resize:vertical}input:focus,select:focus,textarea:focus{border-color:var(--blue);box-shadow:0 0 0 1px rgba(159,199,255,.25)}button{border:1px solid var(--green);background:var(--green);color:#071007;padding:9px 11px;border-radius:5px;font:800 11px/1 "Helvetica Neue",Arial,sans-serif;cursor:pointer;text-transform:uppercase;letter-spacing:.08em}button.secondary{background:#0b0c0c;color:var(--ink);border-color:var(--line2)}.lang{display:flex;gap:6px;margin-bottom:10px}.lang button{background:#0b0c0c;color:var(--muted);border:1px solid var(--line);padding:6px 9px;font-size:10px}.lang button.active{background:var(--ink);color:#0b0c0c;border-color:var(--ink)}.zh{display:none}body[data-lang="zh"] .en{display:none}body[data-lang="zh"] .zh{display:revert}body[data-lang="en"] .en{display:revert}body[data-lang="en"] .zh{display:none}
.guideLink{display:inline-flex;text-decoration:none;color:var(--text);font-size:12px;text-transform:uppercase;letter-spacing:.09em}.statusline,.metrics,.badges{display:flex;gap:8px;flex-wrap:wrap}.pill,.statusline span,.metrics span,.badges span{border:1px solid var(--line);border-radius:999px;background:#0b0c0c;color:var(--text);padding:6px 9px;font-size:12px}.ok{color:var(--green)}.warn{color:var(--amber)}.bad{color:var(--red)}.titleRow{display:flex;align-items:flex-end;justify-content:space-between;gap:18px;margin-bottom:14px}.titleRow h1{font-size:36px;line-height:1;margin:0;font-weight:850;letter-spacing:0}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.projects{display:grid;gap:10px}.project{display:grid;grid-template-columns:1fr auto;gap:12px;align-items:center;padding:13px;border:1px solid var(--line);border-radius:6px;background:var(--panel2);text-decoration:none}.project:hover{border-color:var(--blue);background:#171a1a}.project b{font-size:14px}.health{display:grid;gap:8px;margin-top:12px}.health div{display:grid;grid-template-columns:1fr auto;gap:10px;border:1px solid var(--line);border-radius:5px;background:#0b0c0c;padding:9px}.importMeta{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:14px 0}.importMeta span{border:1px solid var(--line);border-radius:5px;padding:9px 8px;background:#101212;color:var(--text);font-size:12px}.check{display:flex;gap:8px;align-items:center;color:var(--text);text-transform:none;letter-spacing:0}.check input{width:auto}
.queue{display:grid;grid-template-columns:1fr 1fr;gap:10px}.gate{border:1px solid var(--line);background:var(--panel);border-radius:6px;padding:10px}.gate.ready{border-color:var(--green)}.gate.blocked{border-color:var(--red)}.gate h2{margin:0 0 6px;font-size:22px}.gate ul{margin:8px 0 0;padding-left:18px;color:var(--text);line-height:1.35}.workbench{display:grid;grid-template-columns:172px minmax(0,1fr) 380px;gap:10px;align-items:start}.timeline{display:grid;gap:6px}.timeline a{display:grid;grid-template-columns:35px 1fr;gap:6px;align-items:center;text-decoration:none;border:1px solid var(--line);border-radius:5px;background:#0b0c0c;padding:6px}.timeline a:hover{border-color:var(--blue)}.timeline b{font-size:12px}.timeline span{color:var(--muted);font-size:10px;line-height:1.25}.shotMatrix{overflow:hidden}.matrixHead{display:flex;justify-content:space-between;gap:10px;align-items:center;margin-bottom:8px}.matrixScroll{overflow:auto;max-height:calc(100vh - 190px)}.shotTable{width:100%;min-width:980px;border-collapse:collapse;table-layout:fixed}.shotTable th,.shotTable td{border-top:1px solid var(--line);padding:7px 8px;text-align:left;vertical-align:top;font-size:12px;line-height:1.35}.shotTable th{position:sticky;top:0;background:var(--panel);z-index:1;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.1em}.shotTable img{width:110px;aspect-ratio:16/9;object-fit:cover;border:1px solid var(--line);background:#050505}.shotTable .num{width:48px}.shotTable .frameCol{width:126px}.shotTable .tc{width:92px}.shotTable .beat{width:86px}.shotTable .camera{width:180px}.shotTable .creativeNote{width:230px}.clip{color:var(--text);display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}.inspector{display:grid;gap:10px;max-height:calc(100vh - 96px);overflow:auto}.inspectorCard{border:1px solid var(--line);border-radius:6px;background:var(--panel);padding:10px}.inspectorCard h2{margin:0 0 8px;font-size:16px}.fields{display:grid;grid-template-columns:1fr 1fr;gap:7px}.fields .wide{grid-column:1/-1}.artifactList{display:grid;gap:7px}.artifact{display:grid;grid-template-columns:1fr auto;gap:8px;align-items:center;text-decoration:none;border:1px solid var(--line);border-radius:5px;background:#0b0c0c;padding:8px}.artifact[data-present="false"]{opacity:.55;border-style:dashed}.terminal{font-family:Menlo,Consolas,monospace;color:#aeb8aa;background:#070808;border:1px solid var(--line);border-radius:6px;padding:10px;font-size:11px;line-height:1.45;overflow:auto}.stack{display:grid;gap:10px}.decisionForm input[type=radio]{width:auto}.branchChoice{display:grid;grid-template-columns:1fr auto;gap:8px;border:1px solid var(--line);border-radius:5px;background:#0b0c0c;padding:8px}.branchChoice:has(input:checked){border-color:var(--green);background:#101a12}
@media(max-width:1250px){.workbench{grid-template-columns:190px minmax(0,1fr)}.inspector{grid-column:1/-1;grid-template-columns:repeat(2,minmax(0,1fr))}.queue{grid-template-columns:1fr}}@media(max-width:900px){.shell{grid-template-columns:1fr}.side{position:relative;height:auto;border-right:0;border-bottom:1px solid var(--line)}.main{padding:16px 12px 36px}.workbench,.grid,.queue{grid-template-columns:1fr}.storyboard,.inspector{grid-template-columns:1fr}.titleRow{display:block}.fields{grid-template-columns:1fr}}
"""


LOCAL_CORS_HOSTS = {"localhost", "127.0.0.1", "::1"}
CLIENT_EXPORT_IDS = PROFESSIONAL_EXPORT_IDS
ACTIVE_DOCUMENT_SUFFIXES = {
    ".htm",
    ".mht",
    ".mhtml",
    ".svg",
    ".svgz",
    ".xht",
    ".xhtml",
    ".xml",
}
MAX_REQUEST_BODY_BYTES = 1024 * 1024
REQUEST_BODY_TIMEOUT_SECONDS = 10.0


def parse_single_byte_range(value: str, file_size: int) -> tuple[int, int] | None:
    """Parse one RFC 9110 byte range; return ``None`` when unsatisfiable."""
    if file_size <= 0 or not value.startswith("bytes="):
        return None
    spec = value[6:].strip()
    if not spec or "," in spec or spec.count("-") != 1:
        return None
    left, right = (part.strip() for part in spec.split("-", 1))
    try:
        if not left:
            suffix_length = int(right)
            if suffix_length <= 0:
                return None
            start = max(0, file_size - suffix_length)
            return start, file_size - 1
        start = int(left)
        if start < 0 or start >= file_size:
            return None
        if not right:
            return start, file_size - 1
        end = int(right)
        if end < start:
            return None
        return start, min(end, file_size - 1)
    except ValueError:
        return None


def attachment_content_disposition(filename: str) -> str:
    """Build a response-splitting-safe attachment header value."""
    if not filename or any(ord(character) < 32 or ord(character) == 127 for character in filename):
        raise ValueError("Unsafe download filename")
    suffix = Path(filename).suffix
    fallback = "download"
    if suffix and len(suffix) <= 12 and suffix.isascii() and all(
        character.isalnum() or character == "." for character in suffix
    ):
        fallback += suffix
    encoded = quote(filename, safe="")
    return f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{encoded}'


def _stat_snapshot(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    """Identify the exact regular-file revision authorized for a response."""
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        stat.S_IFMT(info.st_mode),
    )


def frontend_dist_path() -> Path | None:
    """Return a built frontend directory, with an explicit override for packaging/tests."""
    configured = os.environ.get("VIDEO_ANALYSIS_FRONTEND_DIST")
    if configured is not None:
        candidate = Path(configured).expanduser().resolve()
        return candidate if (candidate / "index.html").is_file() else None
    candidates = (
        Path(__file__).resolve().parent / "frontend_dist",
        Path(__file__).resolve().parents[2] / "frontend" / "dist",
    )
    return next((candidate for candidate in candidates if (candidate / "index.html").is_file()), None)


def is_loopback_host(host: str | None) -> bool:
    """Accept only localhost names or IP loopback addresses."""
    if not host:
        return False
    candidate = host.strip().lower().rstrip(".")
    if candidate == "localhost":
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def request_host_is_loopback(host_header: str | None) -> bool:
    if not host_header:
        return False
    try:
        parsed = urlparse(f"//{host_header}")
        parsed.port
    except ValueError:
        return False
    return (
        parsed.username is None
        and parsed.password is None
        and is_loopback_host(parsed.hostname)
    )


def local_cors_origin(origin: str | None, host_header: str | None) -> str | None:
    """Return an Origin only when it exactly matches this local HTTP origin.

    A different service on another loopback port is a different security
    principal.  Echoing every localhost origin would let that service read the
    session token and turn browser CSRF protection into ambient local trust.
    The Vite development server proxies API requests, so production CORS is
    neither required nor enabled for sibling loopback origins.
    """
    if not origin or not host_header:
        return None
    parsed = urlparse(origin)
    try:
        origin_port = parsed.port or 80
        host = urlparse(f"//{host_header}")
        host_port = host.port or 80
    except ValueError:
        return None
    if (
        parsed.scheme != "http"
        or parsed.hostname not in LOCAL_CORS_HOSTS
        or host.hostname not in LOCAL_CORS_HOSTS
        or parsed.hostname != host.hostname
        or origin_port != host_port
        or parsed.username is not None
        or parsed.password is not None
        or host.username is not None
        or host.password is not None
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        return None
    return origin


def serve(host: str, port: int, workspace: str | None) -> None:
    if not is_loopback_host(host):
        raise ValueError("The workbench may only bind to a loopback host (localhost, 127.0.0.1, or ::1).")
    root = workspace_path(workspace)
    frontend_dist = frontend_dist_path()
    csrf_token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def do_HEAD(self) -> None:
            if not self._request_origin_allowed():
                return
            parsed = urlparse(self.path)
            if parsed.path == "/api" or parsed.path.startswith("/api/"):
                self._api(root, "HEAD", parsed)
                return
            if parsed.path.startswith("/files/"):
                self._file(root, unquote(parsed.path.split("/", 2)[2]), body=False)
                return
            if self._legacy_get(parsed, body=False):
                return
            if frontend_dist is not None:
                self._frontend(frontend_dist, parsed.path, body=False)
                return
            self.send_error(404)

        def do_GET(self) -> None:
            if not self._request_origin_allowed():
                return
            parsed = urlparse(self.path)
            if parsed.path == "/api" or parsed.path.startswith("/api/"):
                self._api(root, "GET", parsed)
                return
            if parsed.path.startswith("/files/"):
                self._file(root, unquote(parsed.path.split("/", 2)[2]))
                return
            if self._legacy_get(parsed):
                return
            if frontend_dist is not None:
                self._frontend(frontend_dist, parsed.path)
                return
            self.send_error(404)

        def do_POST(self) -> None:
            if not self._request_origin_allowed():
                return
            parsed = urlparse(self.path)
            if parsed.path == "/api" or parsed.path.startswith("/api/"):
                self._api(root, "POST", parsed)
                return
            if parsed.path.startswith(("/shots/", "/vision/")):
                self.send_error(
                    410,
                    "This legacy review endpoint is retired; use the primary workspace",
                )
                return
            if self.headers.get_content_type() != "application/x-www-form-urlencoded":
                self.send_error(415, "Legacy form mutations require application/x-www-form-urlencoded")
                return
            raw_body = self._request_body(json_response=False)
            if raw_body is None:
                return
            try:
                body = raw_body.decode("utf-8")
            except UnicodeDecodeError:
                self.send_error(400, "Request body must be UTF-8")
                return
            data = {key: value[0] for key, value in parse_qs(body).items()}
            supplied_token = data.pop("_csrf", "")
            if not supplied_token or not hmac.compare_digest(supplied_token, csrf_token):
                self.send_error(403, "Invalid CSRF token")
                return
            if parsed.path == "/analyze":
                source = data.get("source", "").strip()
                if urlparse(source).scheme.lower() in {"http", "https"}:
                    self.send_error(
                        400,
                        "URL ingest is disabled in the browser service; use the explicit CLI workflow for a trusted URL",
                    )
                    return
                result_box: dict[str, object] = {}

                def work() -> None:
                    result_box["result"] = run_full_pipeline(
                        source,
                        profile=AnalysisProfile(data.get("profile", "research")),
                        password=data.get("password") or None,
                        workspace=str(root),
                        language=data.get("language") or "auto",
                        delivery_language=data.get("delivery_language") or "zh",
                        skip_asr=data.get("skip_asr") == "on",
                        with_vision=data.get("with_vision") == "on",
                    )

                thread = threading.Thread(target=work)
                thread.start()
                thread.join()
                result = result_box["result"]
                project_id = Path(result.artifacts["project_manifest"]).parent.name  # type: ignore[attr-defined]
                self._redirect(f"/projects/{quote(project_id)}")
                return
            if parsed.path == "/settings":
                try:
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
                except ValueError as exc:
                    self.send_error(400, str(exc))
                    return
                self._redirect("/settings?saved=1")
                return
            if parsed.path.startswith("/regenerate/"):
                project_id = self._validated_project_id(unquote(parsed.path.split("/", 2)[2]))
                if project_id is None:
                    return
                try:
                    project = validated_project_root(root, project_id)
                    regenerate_project_report(root, project)
                except ApiError as exc:
                    self.send_error(exc.status, exc.message)
                    return
                self._redirect(f"/projects/{quote(project_id)}")
                return
            if parsed.path.startswith("/decision/"):
                project_id = self._validated_project_id(unquote(parsed.path.split("/", 2)[2]))
                if project_id is None:
                    return
                try:
                    save_keeper_decision(root, project_id, data)
                except ApiError as exc:
                    self.send_error(exc.status, exc.message)
                    return
                self._redirect(f"/projects/{quote(project_id)}")
                return
            self.send_error(404)

        def do_PATCH(self) -> None:
            if not self._request_origin_allowed():
                return
            parsed = urlparse(self.path)
            if parsed.path == "/api" or parsed.path.startswith("/api/"):
                self._api(root, "PATCH", parsed)
                return
            self.send_error(404)

        def do_DELETE(self) -> None:
            if not self._request_origin_allowed():
                return
            parsed = urlparse(self.path)
            if parsed.path == "/api" or parsed.path.startswith("/api/"):
                self._api(root, "DELETE", parsed)
                return
            self.send_error(404)

        def do_OPTIONS(self) -> None:
            if not self._request_origin_allowed():
                return
            self.send_response(204)
            self.send_header("Allow", "GET,POST,PATCH,DELETE,HEAD,OPTIONS")
            self._cors_headers()
            self.send_header("Access-Control-Allow-Methods", "GET,POST,PATCH,DELETE,HEAD,OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-VEW-CSRF")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args) -> None:
            return

        def _request_origin_allowed(self) -> bool:
            if not request_host_is_loopback(self.headers.get("Host")):
                self.send_error(403, "Loopback Host header required")
                return False
            origin = self.headers.get("Origin")
            if origin and local_cors_origin(origin, self.headers.get("Host")) is None:
                self.send_error(403, "Cross-origin request blocked")
                return False
            if self.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
                self.send_error(403, "Cross-site request blocked")
                return False
            return True

        def _validated_project_id(self, project_id: str) -> str | None:
            try:
                resolve_project_root(project_id, root)
            except ValueError:
                self.send_error(400, "Invalid project id")
                return None
            return project_id

        def _redirect(self, target: str) -> None:
            self.send_response(303)
            self.send_header("Location", target)
            self.end_headers()

        def _legacy_get(self, parsed, body: bool = True) -> bool:  # type: ignore[no-untyped-def]
            path = parsed.path
            explicit_legacy = path == "/legacy" or path.startswith("/legacy/")
            if frontend_dist is not None and not explicit_legacy:
                return False
            legacy_path = (path[7:] or "/") if explicit_legacy else path
            if legacy_path == "/":
                self._html(render_index(root, csrf_token), body=body)
                return True
            if legacy_path == "/settings":
                saved = parse_qs(parsed.query).get("saved", ["0"])[0] == "1"
                self._html(render_settings(root, csrf_token, saved=saved), body=body)
                return True
            if legacy_path.startswith("/projects/"):
                project_id = unquote(legacy_path.split("/", 2)[2])
                try:
                    validated_project_root(root, project_id)
                    content = render_project(root, project_id, csrf_token)
                except ApiError:
                    self.send_error(404)
                    return True
                self._html(content, body=body)
                return True
            if explicit_legacy:
                self.send_error(404)
                return True
            return False

        def _html(self, content: str, body: bool = True) -> None:
            payload = content.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; media-src 'self'; connect-src 'self'; object-src 'none'; "
                "base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
            )
            self.end_headers()
            if body:
                self.wfile.write(payload)

        def _frontend(self, dist: Path, requested_path: str, body: bool = True) -> None:
            try:
                dist_root = dist.resolve()
                requested = unquote(requested_path).lstrip("/")
                candidate = (dist_root / requested).resolve() if requested else dist_root / "index.html"
                inside_dist = candidate.is_relative_to(dist_root)
                is_file = candidate.is_file()
            except (OSError, ValueError):
                self.send_error(404)
                return
            if not inside_dist:
                self.send_error(404)
                return
            if not is_file:
                # Missing fingerprinted/static assets must not receive SPA HTML.
                if requested.startswith("assets/") or Path(requested).suffix:
                    self.send_error(404)
                    return
                candidate = dist_root / "index.html"
            self._static(candidate, dist_root, body=body)

        def _static(self, path: Path, dist_root: Path, body: bool = True) -> None:
            payload = path.read_bytes()
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}:
                content_type += "; charset=utf-8"
            relative = path.relative_to(dist_root)
            cache_control = (
                "public, max-age=31536000, immutable"
                if relative.parts and relative.parts[0] == "assets"
                else "no-cache" if path.name == "index.html" else "public, max-age=3600"
            )
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", cache_control)
            self.send_header("X-Content-Type-Options", "nosniff")
            if path.suffix.lower() == ".html":
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
                    "media-src 'self' blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
                    "form-action 'self'; frame-ancestors 'none'",
                )
            self.end_headers()
            if body:
                self.wfile.write(payload)

        def _api(self, workspace_root: Path, method: str, parsed) -> None:  # type: ignore[no-untyped-def]
            body = b""
            if method in {"POST", "PATCH", "DELETE"}:
                if self.headers.get_content_type() != "application/json":
                    self._json({"error": {"message": "API mutations require application/json", "details": None, "status": 415}}, status=415)
                    return
                supplied_token = self.headers.get("X-VEW-CSRF", "")
                if not supplied_token or not hmac.compare_digest(supplied_token, csrf_token):
                    self._json({"error": {"message": "Invalid CSRF token", "details": None, "status": 403}}, status=403)
                    return
                request_body = self._request_body(json_response=True)
                if request_body is None:
                    return
                body = request_body
            send_body = method != "HEAD"
            if method == "HEAD":
                body = b""
                method = "GET"
            if method == "GET" and parsed.path == "/api/session":
                self._json({"csrf_token": csrf_token}, body=send_body)
                return
            if method == "GET" and self._blocked_client_preview(workspace_root, parsed.path):
                self._json(
                    {
                        "error": {
                            "message": "Professional export is blocked until readiness checks pass",
                            "details": None,
                            "status": 403,
                        }
                    },
                    status=403,
                    body=send_body,
                )
                return
            try:
                status, data = dispatch_api(workspace_root, method, parsed.path, parsed.query, body)
            except ApiError as exc:
                status = exc.status
                data = {"error": {"message": exc.message, "details": exc.details, "status": exc.status}}
            self._json(data, status=status, body=send_body)

        def _request_body(self, *, json_response: bool) -> bytes | None:
            def reject(status: int, message: str) -> None:
                if json_response:
                    self._json(
                        {"error": {"message": message, "details": None, "status": status}},
                        status=status,
                    )
                else:
                    self.send_error(status, message)

            if self.headers.get("Transfer-Encoding"):
                reject(400, "Transfer-Encoding request bodies are not supported")
                return None
            raw_length = self.headers.get("Content-Length", "0")
            try:
                length = int(raw_length)
            except (TypeError, ValueError):
                reject(400, "Content-Length must be a non-negative integer")
                return None
            if length < 0:
                reject(400, "Content-Length must be a non-negative integer")
                return None
            if length > MAX_REQUEST_BODY_BYTES:
                self.close_connection = True
                reject(413, "Request body is too large")
                return None
            if length == 0:
                return b""

            previous_timeout = self.connection.gettimeout()
            try:
                self.connection.settimeout(REQUEST_BODY_TIMEOUT_SECONDS)
                body = self.rfile.read(length)
            except (TimeoutError, socket.timeout):
                self.close_connection = True
                reject(408, "Request body timed out")
                return None
            finally:
                self.connection.settimeout(previous_timeout)
            if len(body) != length:
                self.close_connection = True
                reject(400, "Incomplete request body")
                return None
            return body

        def _json(self, data: object, status: int = 200, body: bool = True) -> None:
            try:
                payload = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
            except (TypeError, ValueError):
                status = 500
                payload = b'{"error":{"message":"Response contains an unsupported value","details":null,"status":500}}'
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self._cors_headers()
            self.send_header("Content-Length", str(len(payload) if body else 0))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            if body:
                self.wfile.write(payload)

        def _cors_headers(self) -> None:
            origin = local_cors_origin(self.headers.get("Origin"), self.headers.get("Host"))
            if origin is not None:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")

        def _file(self, workspace_root: Path, requested: str, body: bool = True) -> None:
            try:
                workspace_root = workspace_root.resolve()
                lexical_path = Path(os.path.abspath(os.fspath(workspace_root / requested)))
                path = lexical_path.resolve()
                if path != lexical_path:
                    raise ValueError("Symlinked file paths are not served")
                relative = path.relative_to(workspace_root)
                if len(relative.parts) < 2:
                    raise ValueError("A project-relative file path is required")
                project = resolve_project_root(relative.parts[0], workspace_root)
            except (ApiError, OSError, ValueError):
                self.send_error(404)
                return
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            suffix = path.suffix.lower()
            if path.suffix == ".md":
                content_type = "text/markdown; charset=utf-8"
            content_disposition: str | None = None
            if content_type == "application/octet-stream":
                try:
                    content_disposition = attachment_content_disposition(path.name)
                except ValueError:
                    self.send_error(404)
                    return
            from .media import _open_regular_no_symlinks

            # Generators, vision, legacy review mutations, and report readers
            # share this lock.  Authorize the committed generation and open its
            # descriptor inside one critical section; the response can then use
            # that immutable descriptor even if a later generation starts.
            with advisory_file_lock(project / "data" / ".shots.lock", root=project):
                try:
                    load_project_manifest(project)
                    valid_file = (
                        project == workspace_root / relative.parts[0]
                        and path.is_file()
                        and is_current_project_file(project, path)
                    )
                    if not valid_file:
                        raise ValueError("Project file is not current")
                    if suffix in ACTIVE_DOCUMENT_SUFFIXES or content_type in {
                        "application/xhtml+xml",
                        "application/xml",
                        "image/svg+xml",
                        "text/xml",
                    }:
                        self.send_error(403, "Active document previews are not served")
                        return
                    if self._blocked_client_export(workspace_root, path):
                        self.send_error(403, "Professional export is blocked until readiness checks pass")
                        return
                    authorized_info = path.lstat()
                    safe_open = _open_regular_no_symlinks(path)
                    descriptor = safe_open.__enter__()
                    opened_info = os.fstat(descriptor)
                    if _stat_snapshot(authorized_info) != _stat_snapshot(opened_info):
                        safe_open.__exit__(None, None, None)
                        raise ValueError("Project file changed between authorization and open")
                except (ApiError, OSError, ValueError):
                    self.send_error(404)
                    return
            try:
                file_size = os.fstat(descriptor).st_size
                range_header = self.headers.get("Range", "")
                start = 0
                end = file_size - 1
                status = 200
                if range_header:
                    selected_range = parse_single_byte_range(range_header, file_size)
                    if selected_range is None:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{file_size}")
                        self.send_header("Accept-Ranges", "bytes")
                        self.send_header("Content-Length", "0")
                        self.send_header("X-Content-Type-Options", "nosniff")
                        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
                        self.send_header("Referrer-Policy", "no-referrer")
                        self.end_headers()
                        return
                    start, end = selected_range
                    status = 206
                length = end - start + 1 if file_size else 0
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(length))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Cross-Origin-Resource-Policy", "same-origin")
                self.send_header("Referrer-Policy", "no-referrer")
                if suffix == ".html":
                    self.send_header(
                        "Content-Security-Policy",
                        "default-src 'none'; img-src 'self' data:; media-src 'self'; "
                        "style-src 'unsafe-inline'; script-src 'none'; base-uri 'none'; "
                        "form-action 'none'; frame-ancestors 'none'; connect-src 'none'; sandbox",
                    )
                elif content_disposition is not None:
                    self.send_header("Content-Disposition", content_disposition)
                if status == 206:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
                self.end_headers()
                if body:
                    os.lseek(descriptor, start, os.SEEK_SET)
                    remaining = length
                    while remaining:
                        chunk = os.read(descriptor, min(64 * 1024, remaining))
                        if not chunk:
                            break
                        try:
                            self.wfile.write(chunk)
                        except (BrokenPipeError, ConnectionResetError):
                            self.close_connection = True
                            return
                        remaining -= len(chunk)
            finally:
                safe_open.__exit__(None, None, None)

        def _blocked_client_export(self, workspace_root: Path, path: Path) -> bool:
            relative = path.relative_to(workspace_root)
            if len(relative.parts) < 3:
                return False
            project = workspace_root / relative.parts[0]
            if not is_professional_export_path(project, path):
                return False
            readiness = readiness_for_project(project)
            return not bool(readiness.get("professional_export_allowed"))

        def _blocked_client_preview(self, workspace_root: Path, requested_path: str) -> bool:
            parts = [unquote(part) for part in requested_path.strip("/").split("/")]
            if (
                len(parts) != 6
                or parts[:2] != ["api", "projects"]
                or parts[3] != "deliverables"
                or parts[4] not in CLIENT_EXPORT_IDS
                or parts[5] != "preview"
            ):
                return False
            try:
                workspace_root = workspace_root.resolve()
                project = (workspace_root / parts[2]).resolve()
                if not project.is_relative_to(workspace_root) or not project.is_dir():
                    return False
            except (OSError, ValueError):
                return False
            readiness = readiness_for_project(project)
            return not bool(readiness.get("professional_export_allowed"))

    print(f"Serving video analysis UI at http://{host}:{port}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


def csrf_field(token: str) -> str:
    return f'<input type="hidden" name="_csrf" value="{html.escape(token, quote=True)}">'


def render_index(root: Path, csrf_token: str) -> str:
    projects = find_projects(str(root))
    doctor = run_doctor(str(root))
    cards = "\n".join(render_project_card(root, project.project_id, project.source, project.status) for project in projects)
    latest = projects[0] if projects else None
    latest_status = (
        readiness_for_project(resolve_project_root(latest.project_id, root))["status"]
        if latest
        else "waiting"
    )
    doctor_class = "ok" if doctor.status == "success" else "warn"
    return page(
        f"""
<div class="shell">
<aside class="side">
  <div class="lang"><button type="button" data-set-lang="en">EN</button><button type="button" data-set-lang="zh" class="active">中文</button></div>
  <div class="runbox">
    <div class="kicker">Shot Breakdown Workbench</div>
    <h2 class="brand"><span class="en">Evidence First.</span><span class="zh">证据优先。</span><small>auditable shot-level research</small></h2>
    <div class="importMeta"><span>local first</span><span>review gate</span><span>shot data</span></div>
    <form class="form" method="post" action="/analyze">
      {csrf_field(csrf_token)}
      <div><label><span class="en">Local video file path</span><span class="zh">本地视频文件路径</span></label><input name="source" required placeholder="/path/to/video.mp4"></div>
      <p class="muted"><span class="en">For a trusted URL, use the explicit CLI ingest workflow.</span><span class="zh">可信 URL 请使用显式 CLI 导入流程。</span></p>
      <div><label><span class="en">Profile</span><span class="zh">类型</span></label><select name="profile"><option value="research">Research / 研究</option><option value="ads">Ads / 广告</option><option value="shortform">Shortform / 短视频</option><option value="streaming">Streaming / 流媒体</option><option value="festival">Festival / 电影节</option></select></div>
      <div><label><span class="en">Password</span><span class="zh">密码</span></label><input type="password" name="password" autocomplete="off" spellcheck="false" placeholder="Optional"></div>
      <div><label><span class="en">Delivery language</span><span class="zh">交付语言</span></label><select name="delivery_language"><option value="zh">中文版本</option><option value="en">English version</option></select></div>
      <div><label><span class="en">ASR language</span><span class="zh">转写语言</span></label><input name="language" placeholder="auto, Chinese, English"></div>
      <label class="check"><input type="checkbox" name="skip_asr" checked> <span class="en">Fast pass without ASR</span><span class="zh">跳过 ASR 快速跑</span></label>
      <label class="check"><input type="checkbox" name="with_vision"> <span class="en">Send frames to configured vision provider</span><span class="zh">将关键帧发送到已配置的视觉模型</span></label>
      <button type="submit"><span class="en">Analyze Video</span><span class="zh">分析视频</span></button>
    </form>
  </div>
  <div style="height:14px"></div>
  <a class="guideLink" href="/settings"><span class="en">Settings</span><span class="zh">设置</span></a>
</aside>
<main class="main">
  <div class="topbar"><a class="guideLink" href="/">Workspace</a><div class="statusline"><span>{html.escape(latest.project_id if latest else "no-project")}</span><span>{html.escape(latest_status)}</span></div></div>
  <div class="titleRow"><div><div class="kicker">Operator Queue</div><h1><span class="en">Import. Detect shots. Annotate. Review. Export evidence.</span><span class="zh">导入、拆镜头、标注、复核、导出证据。</span></h1></div><span class="pill">evidence gate</span></div>
  <section class="queue">
    <div class="panel"><div class="kicker">Projects</div><div class="projects" style="margin-top:12px">{cards or '<div class="muted">No projects yet.</div>'}</div></div>
    <div class="panel"><div class="kicker">Runtime</div><div class="health"><div><span>doctor</span><b class="{doctor_class}">{html.escape(doctor.status)}</b></div><div><span>review pathway</span><b class="{'ok' if any('evidence readiness:' in item and 'blocked' not in item for item in doctor.diagnostics) else 'bad'}">{'available' if any('evidence readiness:' in item and 'blocked' not in item for item in doctor.diagnostics) else 'needs review'}</b></div><div><span>storyboard/shot list</span><b class="ok">primary</b></div></div></div>
  </section>
</main></div>
"""
    )


def render_project(root: Path, project_id: str, csrf_token: str) -> str:
    try:
        project = validated_project_root(root, project_id)
        manifest_model = load_project_manifest(project)
    except ApiError:
        raise
    manifest_path = project / "project_manifest.json"
    manifest = manifest_model.model_dump(mode="json")
    is_ads = str(manifest.get("profile") or "research") == "ads"
    shots = load_shots(project)
    lang = project_delivery_language(project)
    readiness = readiness_for_project(project, shots)
    artifacts = project_artifacts(root, project, manifest_path)
    decision = load_keeper_decision(project)
    lineage = load_lineage(project)
    gate_class = "ready" if readiness.get("professional_export_allowed") else "blocked"
    return page(
        f"""
<div class="shell">
<aside class="side">
  <div class="lang"><button type="button" data-set-lang="en">EN</button><button type="button" data-set-lang="zh" class="active">中文</button></div>
  <div class="runbox">
    <div class="kicker">Active Project</div>
    <h2 class="brand">{html.escape(project_id)}<small>{html.escape(str(manifest.get('source','')))}</small></h2>
    <div class="importMeta"><span>{len(shots)} shots</span><span>{html.escape(str(readiness.get('status','blocked')))}</span><span>{html.escape(str(manifest.get('status','unknown')))}</span></div>
    <form class="form" method="post" action="/regenerate/{quote(project_id)}">
      {csrf_field(csrf_token)}
      <button class="secondary" type="submit">{'明确完成交付包' if lang == 'zh' else 'Finalize Package'}</button>
    </form>
    <p class="muted">{'旧版证据视图中的镜头与视觉字段只读；编辑与复核请返回主工作台。明确完成交付包仍使用同一门禁。' if lang == 'zh' else 'Shot and vision evidence is read-only in this legacy view. Use the primary workspace for editing and review; explicit Finalize uses the same readiness gate.'}</p>
  </div>
  <div style="height:14px"></div>
  <a class="guideLink" href="/">← Workspace</a>
</aside>
<main class="main">
  <div class="titleRow"><div><div class="kicker">Storyboard / Shot List</div><h1><span class="en">Professional Shot Breakdown Workbench</span><span class="zh">专业分镜拆片工作台</span></h1></div><div class="statusline"><span>{'中文版本' if lang == 'zh' else 'English version'}</span><span>{html.escape(_status_text(str(readiness.get('status','blocked')), lang))}</span><span>{len(shots)} {'个镜头' if lang == 'zh' else 'shots'}</span></div></div>
  {render_readiness_gate(readiness, gate_class, lang)}
  <section class="workbench">
    <aside class="timeline panel"><div class="kicker">{'镜头时间线' if lang == 'zh' else 'Shot Timeline'}</div>{render_timeline(shots, lang)}</aside>
    {render_shot_matrix(project_id, shots, lang, is_ads)}
    <aside class="inspector"><div class="panel"><div class="kicker">Read-only evidence fallback</div><p class="muted">{'全部镜头保留在时间线与矩阵中；编辑、视觉复核和音频复核只在主工作台进行。' if lang == 'zh' else 'All shots remain visible in the timeline and matrix. Editing, vision review and audio review belong to the primary workspace.'}</p><a class="guideLink" href="/projects/{quote(project_id)}">{'打开主工作台' if lang == 'zh' else 'Open primary workspace'} →</a></div></aside>
  </section>
  <section class="grid" style="margin-top:12px">
    <div class="panel"><div class="kicker">Exports</div><div class="artifactList" style="margin-top:12px">{''.join(render_artifact_link(item, readiness) for item in artifacts)}</div></div>
    {f'<div class="panel"><div class="kicker">Creative branch decision · heuristic</div>{render_keeper_form(project_id, decision, csrf_token)}</div>' if is_ads else ''}
    <div class="terminal">{render_lineage_summary(lineage, readiness, decision)}</div>
  </section>
</main></div>
"""
    )


def render_readiness_gate(readiness: dict[str, object], gate_class: str, lang: str = "zh") -> str:
    reasons = readiness.get("reasons") if isinstance(readiness.get("reasons"), list) else []
    items = "".join(f"<li>{html.escape(str(item))}</li>" for item in reasons) or ("<li>专业门禁已通过</li>" if lang == "zh" else "<li>professional readiness passed</li>")
    return f"""<section class="gate {gate_class}" style="margin-bottom:12px">
<div class="kicker">{'专业门禁' if lang == 'zh' else 'Readiness Gate'}</div>
<h2>{html.escape(_status_text(str(readiness.get('status','blocked')), lang))}</h2>
<div class="metrics"><span>{'视觉标注完成' if lang == 'zh' else 'vision annotation complete'}: {html.escape(str(readiness.get('vision_annotation_complete', False)).lower())}</span><span>{'人工复核覆盖' if lang == 'zh' else 'human review complete'}: {html.escape(str(readiness.get('human_review_override', False)).lower())}</span><span>{'密钥仅供调用' if lang == 'zh' else 'provider key (not verification)'}: {html.escape(str(readiness.get('vision_key_configured', False)).lower())}</span><span>{'空字段率' if lang == 'zh' else 'empty'}: {html.escape(str(readiness.get('critical_empty_rate', 0)))}</span><span>{'平均视觉置信度' if lang == 'zh' else 'avg vision'}: {html.escape(str(readiness.get('average_visual_confidence', 0)))}</span><span>{'低边界占比' if lang == 'zh' else 'low boundary'}: {html.escape(str(readiness.get('low_boundary_confidence_rate', 0)))}</span></div>
<ul>{items}</ul>
</section>"""


def render_timeline(shots: list[Shot], lang: str = "zh") -> str:
    return "".join(
        f'<a href="#row-{html.escape(shot.shot_id)}"><b>#{shot.shot_no:02d}</b><span>{html.escape(shot.timecode)}<br>{html.escape(_story_beat_display(shot, lang))}</span></a>'
        for shot in shots
    )


def render_shot_matrix(project_id: str, shots: list[Shot], lang: str, is_ads: bool) -> str:
    if not shots:
        return '<section class="shotMatrix panel">No shots yet.</section>'
    header = (
        ["#", "主帧", "时码", "段落", "画面 / 动作", "镜头语言"]
        if lang == "zh"
        else ["#", "Frame", "TC", "Beat", "Content / Action", "Camera"]
    )
    if is_ads:
        header.append("创意提示词（未验证）" if lang == "zh" else "Creative prompt (unverified)")
    rows = "".join(render_shot_matrix_row(project_id, shot, lang, is_ads) for shot in shots)
    header_html = "".join(f"<th>{html.escape(label)}</th>" for label in header)
    title = "分镜矩阵" if lang == "zh" else "Shot Matrix"
    if is_ads:
        deck = "更密的信息表：主帧、时码、段落、动作、镜头语言、创意指令集中在一屏。" if lang == "zh" else "Dense shot table: frame, timecode, beat, action, camera, and creative direction in one pass."
    else:
        deck = "更密的信息表：主帧、时码、段落、动作与镜头语言集中在一屏。" if lang == "zh" else "Dense shot table: frame, timecode, beat, action, and camera evidence in one pass."
    return f"""<section class="shotMatrix panel">
<div class="matrixHead"><div><div class="kicker">{html.escape(title)}</div><div class="muted">{html.escape(deck)}</div></div><span class="pill">{len(shots)} {'镜头' if lang == 'zh' else 'shots'}</span></div>
<div class="matrixScroll"><table class="shotTable"><thead><tr>{header_html}</tr></thead><tbody>{rows}</tbody></table></div>
</section>"""


def render_shot_matrix_row(project_id: str, shot: Shot, lang: str, is_ads: bool) -> str:
    thumb = f"/files/{quote(f'{project_id}/assets/keyframes/{shot.primary_frame_ref or shot.frame_ref}')}" if shot.primary_frame_ref or shot.frame_ref else ""
    content = shot_text(shot, "content", lang)
    action = shot_text(shot, "action", lang)
    prompt = shot.prompt_zh if lang == "zh" else shot.prompt_en
    if is_ads and not prompt:
        prompt = f"{content} / {_camera_text(shot, lang)}"
    prompt_cell = f'<td class="creativeNote"><div class="clip">{html.escape(prompt)}</div></td>' if is_ads else ""
    return (
        f'<tr id="row-{html.escape(shot.shot_id)}">'
        f'<td class="num"><span>#{shot.shot_no:02d}</span></td>'
        f'<td class="frameCol"><img src="{html.escape(thumb)}" alt="shot {shot.shot_no}"></td>'
        f'<td class="tc">{html.escape(shot.timecode)}<br>{shot.duration:.1f}s</td>'
        f'<td class="beat">{html.escape(_story_beat_display(shot, lang))}<br><span class="muted">{html.escape(_status_text(shot.readiness_status or "blocked", lang))}</span></td>'
        f'<td><b>{html.escape(content)}</b><div class="muted">{html.escape(action)}</div></td>'
        f'<td class="camera">{html.escape(_camera_text(shot, lang, include_composition=True))}</td>'
        + prompt_cell
        + "</tr>"
    )


def render_settings(root: Path, csrf_token: str, saved: bool = False) -> str:
    config = load_runtime_config(root)
    provider_openai = "selected" if config.vision_provider == "openai" else ""
    provider_minimax = "selected" if config.vision_provider == "minimax_mcp" else ""
    saved_html = '<div class="panel"><b>Settings saved.</b></div>' if saved else ""
    return page(
        f"""
<div class="shell">
<aside class="side"><div class="lang"><button type="button" data-set-lang="en">EN</button><button type="button" data-set-lang="zh" class="active">中文</button></div><h2 class="brand">Runtime Settings</h2><p class="muted">Provider keys enable optional vision calls; they never prove that annotation or review is complete.</p><a class="guideLink" href="/">← Project Index</a></aside>
<main class="main">{saved_html}<section class="gate blocked"><div class="kicker">Vision Provider</div><h2>Local credentials</h2><p class="muted">Use environment variables when possible. File-backed secrets are private to the local user and endpoint changes require key re-entry.</p><div class="statusline"><span>MiniMax: {html.escape(mask_secret(config.minimax_api_key))}</span><span>OpenAI: {html.escape(mask_secret(config.openai_api_key))}</span></div></section>
<form class="form panel" method="post" action="/settings" autocomplete="off">
{csrf_field(csrf_token)}
<div><label>Default provider</label><select name="vision_provider"><option value="openai" {provider_openai}>OpenAI compatible</option><option value="minimax_mcp" {provider_minimax}>MiniMax MCP</option></select></div>
<div class="grid">
<div><label>MiniMax API Key</label><input type="password" name="minimax_api_key" placeholder="{html.escape(mask_secret(config.minimax_api_key))}"></div>
<div><label>MiniMax API Host</label><input name="minimax_api_host" value="{html.escape(config.minimax_api_host)}" placeholder="https://api.minimaxi.com"></div>
<div><label>China host</label><input value="https://api.minimaxi.com" readonly></div>
</div>
<div class="grid">
<div><label>OpenAI API Key</label><input type="password" name="openai_api_key" placeholder="{html.escape(mask_secret(config.openai_api_key))}"></div>
<div><label>OpenAI Base URL</label><input name="openai_base_url" value="{html.escape(config.openai_base_url)}"></div>
<div><label>OpenAI Vision Model</label><input name="openai_model" value="{html.escape(config.openai_model)}"></div>
</div>
<button type="submit">Save Local Settings</button>
</form></main></div>
"""
    )


def render_project_card(root: Path, project_id: str, source: str, status: str) -> str:
    try:
        project = resolve_project_root(project_id, root)
    except ValueError:
        return ""
    readiness = readiness_for_project(project)
    css_class = "ok" if readiness.get("professional_export_allowed") else "bad"
    return (
        f'<a class="project" href="/projects/{quote(project_id)}">'
        f'<span><b>{html.escape(project_id)}</b><br><span class="muted">{html.escape(source)}</span></span>'
        f'<span class="pill {css_class}">{html.escape(str(readiness.get("status", status)))}</span></a>'
    )


def project_artifacts(root: Path, project: Path, manifest_path: Path) -> list[dict[str, object]]:
    specs = [
        ("primary", "Storyboard / 分镜故事板", artifact_path(project, "storyboard_html")),
        ("primary", "Shot list / 标准镜头表", artifact_path(project, "shot_list_csv")),
        ("evidence", "Profile analysis / 分析报告", artifact_path(project, "profile_analysis_html")),
        ("appendix", "Lineage JSON / 血缘 JSON", artifact_path(project, "lineage_json")),
        ("gate", "Readiness JSON / 门禁 JSON", artifact_path(project, "readiness_json")),
        ("legacy", "Legacy report / 旧报告", artifact_path(project, "report_html")),
        ("data", "Manifest / 项目清单", manifest_path),
    ]
    manifest = load_project_manifest(project).model_dump(mode="json")
    if str(manifest.get("profile") or "research") == "ads":
        specs.extend(
            [
                ("creative", "Remake brief · heuristic / 复拍简报", artifact_path(project, "remake_brief")),
                ("creative", "Model prompt pack · unverified / 模型提示词包", artifact_path(project, "model_prompt_pack")),
                ("creative", "Branch board · heuristic / 分支附录", artifact_path(project, "branch_board_html")),
            ]
        )
    return [
        _legacy_artifact_item(root, project, group, label, path)
        for group, label, path in specs
    ]


def _legacy_artifact_item(
    root: Path,
    project: Path,
    group: str,
    label: str,
    path: Path,
) -> dict[str, object]:
    """Describe a legacy-page artifact without following links or special files."""
    present = False
    relative = path.name
    try:
        workspace = Path(os.path.abspath(os.fspath(root)))
        project_root = Path(os.path.abspath(os.fspath(project)))
        candidate = Path(os.path.abspath(os.fspath(path)))
        candidate.relative_to(project_root)
        info = _deliverable_file_info(project_root, candidate)
        if info is not None and info.st_size > 0:
            relative = candidate.relative_to(workspace).as_posix()
            present = True
    except (OSError, ValueError):
        pass
    return {
        "group": group,
        "label": label,
        "path": path,
        "present": present,
        "rel": relative,
    }


def render_artifact_link(item: dict[str, object], readiness: dict[str, object] | None = None) -> str:
    path = item["path"]
    if not isinstance(path, Path):
        raise TypeError("artifact path must be a pathlib.Path")
    present = bool(item["present"])
    label = str(item["label"])
    group = str(item["group"])
    rel = str(item["rel"])
    professional_blocked = (
        readiness is not None
        and is_professional_export_path(path.parents[1], path)
        and not readiness.get("professional_export_allowed")
    )
    if present:
        status_class = "bad" if professional_blocked else "warn" if group == "creative" else "ok"
        status_text = "blocked" if professional_blocked else "unverified" if group == "creative" else "available"
        content = f'<span>{html.escape(label)}<br><small>{html.escape(group)} · {html.escape(rel)}</small></span><span class="pill {status_class}">{status_text}</span>'
        if professional_blocked:
            return f'<div class="artifact" data-present="true" aria-disabled="true">{content}</div>'
        href = f"/files/{quote(rel)}"
        return f'<a class="artifact" data-present="true" href="{href}">{content}</a>'
    return f'<div class="artifact" data-present="false"><span>{html.escape(label)}<br><small>{html.escape(group)} · {html.escape(rel)}</small></span><span class="pill warn">missing</span></div>'


def render_keeper_form(project_id: str, decision: dict[str, str], csrf_token: str) -> str:
    keeper = decision.get("keeper_branch", "")
    reject_reason = html.escape(decision.get("reject_reason", ""))
    revision_request = html.escape(decision.get("revision_request", ""))
    return f"""<form class="form decisionForm" method="post" action="/decision/{quote(project_id)}">
{csrf_field(csrf_token)}
<label class="branchChoice"><span>safer</span><input type="radio" name="keeper_branch" value="safer" {"checked" if keeper == "safer" else ""}></label>
<label class="branchChoice"><span>stronger_hook</span><input type="radio" name="keeper_branch" value="stronger_hook" {"checked" if keeper == "stronger_hook" else ""}></label>
<label class="branchChoice"><span>premium_style</span><input type="radio" name="keeper_branch" value="premium_style" {"checked" if keeper == "premium_style" else ""}></label>
<label>Reject reason</label><textarea name="reject_reason">{reject_reason}</textarea>
<label>Next revision request</label><textarea name="revision_request">{revision_request}</textarea>
<button type="submit">Save Decision</button>
</form>"""


def load_shots(project: Path) -> list[Shot]:
    path = project / "data" / "shots.json"
    data = read_project_json(project, path, None)
    if data is None:
        return []
    try:
        if not isinstance(data, list):
            raise ApiError(409, "Shot receipt must be a JSON array")
        return [Shot.model_validate(item) for item in data]
    except ApiError:
        raise
    except Exception:
        raise ApiError(409, "Shot receipt failed schema validation") from None


def readiness_for_project(project: Path, shots: list[Shot] | None = None) -> dict[str, object]:
    # ``shots`` is retained for call compatibility; the gate deliberately
    # reloads current project evidence and validates its persisted v2 binding.
    return evaluate_project_readiness(project, workspace_root=project.parent)


def load_keeper_decision(project: Path) -> dict[str, str]:
    path = project / "data" / "keeper_decision.json"
    data = read_project_json(project, path, None)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ApiError(409, "Keeper decision receipt must be a JSON object")
    return {key: str(value) for key, value in data.items() if value is not None}


def load_lineage(project: Path) -> dict[str, object]:
    path = project / "data" / "lineage.json"
    data = read_project_json(project, path, None)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ApiError(409, "Lineage receipt must be a JSON object")
    return data


def render_lineage_summary(lineage: dict[str, object], readiness: dict[str, object], decision: dict[str, str]) -> str:
    nodes = lineage.get("nodes") if isinstance(lineage.get("nodes"), list) else []
    commits = lineage.get("commits") if isinstance(lineage.get("commits"), list) else []
    branches = lineage.get("branches") if isinstance(lineage.get("branches"), list) else []
    parts = [
        "lineage.json / readiness",
        f"schema_version: {html.escape(str(lineage.get('schema_version', 'missing')))}",
        f"nodes: {len(nodes)}",
        f"commits: {len(commits)}",
        f"branches: {', '.join(html.escape(str(item.get('name', 'unknown'))) for item in branches if isinstance(item, dict)) or 'missing'}",
        f"readiness: {html.escape(str(readiness.get('status', 'blocked')))}",
        f"keeper: {html.escape(decision.get('keeper_branch', 'none'))}",
        f"reject_reason: {'set' if decision.get('reject_reason') else 'empty'}",
    ]
    return "<br>".join(parts)


def save_keeper_decision(root: Path, project_id: str, data: dict[str, str]) -> None:
    project = validated_project_root(root, project_id)
    if load_project_manifest(project).profile != AnalysisProfile.ads:
        raise ApiError(409, "Keeper decisions are available only for ads projects")
    keeper_branch = data.get("keeper_branch", "").strip()
    if keeper_branch not in {"", "safer", "stronger_hook", "premium_style"}:
        raise ApiError(400, "Unsupported keeper branch")
    decision = {
        "keeper_branch": keeper_branch,
        "reject_reason": data.get("reject_reason", "").strip(),
        "revision_request": data.get("revision_request", "").strip(),
        "updated_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }
    with project_write_lock(project):
        write_project_json(project, project / "data" / "keeper_decision.json", decision)
def project_delivery_language(project: Path) -> str:
    path = project / "data" / "media_package.json"
    data = read_project_json(project, path, None)
    if data is None:
        return "zh"
    if not isinstance(data, dict):
        raise ApiError(409, "Media receipt must be a JSON object")
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    value = str(metadata.get("delivery_language") or "zh").lower()
    return "en" if value.startswith("en") else "zh"


def shot_text(shot: Shot, kind: str, lang: str) -> str:
    if kind == "content":
        return (shot.content_summary_zh or shot.content_summary or shot.visual_description) if lang == "zh" else (shot.content_summary or shot.visual_description or shot.content_summary_zh)
    if kind == "subject":
        return (shot.subject_zh or shot.subject) if lang == "zh" else (shot.subject or shot.subject_zh)
    if kind == "action":
        return (shot.action_zh or shot.action) if lang == "zh" else (shot.action or shot.action_zh)
    if kind == "remake":
        return (shot.remake_notes_zh or shot.remake_notes) if lang == "zh" else (shot.remake_notes or shot.remake_notes_zh)
    return ""


def page(content: str) -> str:
    script = """
<script>
const buttons = document.querySelectorAll('[data-set-lang]');
function setLang(lang){document.body.dataset.lang=lang;document.documentElement.lang=lang==='zh'?'zh-CN':'en';buttons.forEach(btn=>btn.classList.toggle('active',btn.dataset.setLang===lang));localStorage.setItem('video-analysis-ui-lang',lang);}
buttons.forEach(btn=>btn.addEventListener('click',()=>setLang(btn.dataset.setLang)));
setLang(localStorage.getItem('video-analysis-ui-lang') || 'zh');
</script>
"""
    return f"<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'><title>Shot Breakdown Workbench</title><style>{INDEX_CSS}</style></head><body data-lang='zh'>{content}{script}</body></html>"
