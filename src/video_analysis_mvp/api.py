from __future__ import annotations

import asyncio
import hmac
import secrets
from typing import Any
from urllib.parse import quote, urlencode

from . import __version__
from .config import VisionProvider
from .media import DEFAULT_MAX_DURATION_SECONDS, infer_source_type
from .pipeline import (
    run_audio,
    run_full_pipeline,
    run_ingest_only,
    run_report,
    run_vision,
    run_visual,
)
from .schemas import AnalysisProfile
from .store import find_projects

try:
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse
    from pydantic import BaseModel, StrictBool, field_validator
    from starlette.concurrency import run_in_threadpool
except Exception as exc:  # pragma: no cover
    FastAPI = None  # type: ignore[assignment]
    Request = object  # type: ignore[assignment,misc]
    JSONResponse = object  # type: ignore[assignment,misc]
    BaseModel = object  # type: ignore[assignment,misc]
    StrictBool = bool  # type: ignore[assignment,misc]
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None


if FastAPI is None:  # pragma: no cover
    raise RuntimeError("FastAPI is not installed. Install the api extra or use `analyze-video serve`.")


class RunRequest(BaseModel):
    source: str
    profile: AnalysisProfile = AnalysisProfile.research
    password: str | None = None
    workspace: str | None = None
    project_id: str | None = None
    language: str = "auto"
    skip_asr: bool = True
    with_vision: StrictBool = False
    max_duration_seconds: float = DEFAULT_MAX_DURATION_SECONDS

    @field_validator("source")
    @classmethod
    def require_local_file_source(cls, value: str) -> str:
        if infer_source_type(value).value == "url":
            raise ValueError(
                "URL sources are not accepted by the FastAPI service; "
                "use the CLI trusted-operator ingest path"
            )
        return value


class ProjectRequest(BaseModel):
    workspace: str | None = None


class AudioRequest(ProjectRequest):
    language: str = "auto"
    skip_asr: bool = True


class VisionRequest(ProjectRequest):
    provider: VisionProvider | None = None
    model: str | None = None
    limit: int | None = None


MAX_REQUEST_BODY_BYTES = 1024 * 1024
REQUEST_BODY_TIMEOUT_SECONDS = 15.0


class RequestBodyLimitMiddleware:
    """Bound request streams before FastAPI buffers or validates their bodies."""

    def __init__(self, application: Any) -> None:
        self.application = application

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.application(scope, receive, send)
            return
        try:
            declared_lengths = [
                value.decode("ascii", errors="strict").strip()
                for name, value in scope.get("headers", [])
                if name.lower() == b"content-length"
            ]
            if any(not value.isdigit() for value in declared_lengths):
                raise ValueError("Content-Length must contain decimal digits")
            parsed_lengths = [int(value) for value in declared_lengths]
        except (UnicodeDecodeError, ValueError):
            await self._error(scope, receive, send, 400, "invalid Content-Length")
            return
        transfer_encoding = any(
            name.lower() == b"transfer-encoding"
            for name, _value in scope.get("headers", [])
        )
        if len(parsed_lengths) > 1 or (parsed_lengths and transfer_encoding):
            await self._error(scope, receive, send, 400, "invalid Content-Length")
            return
        if parsed_lengths and parsed_lengths[0] > MAX_REQUEST_BODY_BYTES:
            await self._error(
                scope,
                receive,
                send,
                413,
                f"request body exceeds {MAX_REQUEST_BODY_BYTES} bytes",
            )
            return

        if scope.get("method") not in {"POST", "PATCH", "PUT", "DELETE"}:
            await self.application(scope, receive, send)
            return

        buffered: list[dict[str, Any]] = []
        received = 0
        deadline = asyncio.get_running_loop().time() + REQUEST_BODY_TIMEOUT_SECONDS
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                await self._error(scope, receive, send, 408, "request body read timed out")
                return
            try:
                message = await asyncio.wait_for(receive(), timeout=remaining)
            except TimeoutError:
                await self._error(scope, receive, send, 408, "request body read timed out")
                return
            if message.get("type") != "http.request":
                buffered.append(message)
                break
            body = message.get("body", b"")
            received += len(body)
            if received > MAX_REQUEST_BODY_BYTES:
                await self._error(
                    scope,
                    receive,
                    send,
                    413,
                    f"request body exceeds {MAX_REQUEST_BODY_BYTES} bytes",
                )
                return
            buffered.append(message)
            if not message.get("more_body", False):
                break

        if parsed_lengths and received != parsed_lengths[0]:
            await self._error(scope, receive, send, 400, "Content-Length does not match request body")
            return

        async def replay_receive() -> dict[str, Any]:
            if buffered:
                return buffered.pop(0)
            return await receive()

        await self.application(scope, replay_receive, send)

    @staticmethod
    async def _error(
        scope: dict[str, Any],
        receive: Any,
        send: Any,
        status_code: int,
        message: str,
    ) -> None:
        response = JSONResponse({"error": message}, status_code=status_code)
        await response(scope, receive, send)


app = FastAPI(title="Video Evidence Workbench", version=__version__)
app.add_middleware(RequestBodyLimitMiddleware)
_CSRF_TOKEN = secrets.token_urlsafe(32)


@app.middleware("http")
async def local_api_boundary(request: Request, call_next):  # type: ignore[no-untyped-def]
    """Keep the optional FastAPI adapter behind the same local-only boundary."""
    from .web import is_loopback_host, local_cors_origin, request_host_is_loopback

    client_host = request.client.host if request.client else None
    if not is_loopback_host(client_host) or not request_host_is_loopback(request.headers.get("host")):
        return JSONResponse({"error": "loopback client and Host required"}, status_code=403)
    origin = request.headers.get("origin")
    if origin and local_cors_origin(origin, request.headers.get("host")) is None:
        return JSONResponse({"error": "cross-origin request blocked"}, status_code=403)
    if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
        return JSONResponse({"error": "cross-site request blocked"}, status_code=403)
    if request.method in {"POST", "PATCH", "PUT", "DELETE"}:
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            return JSONResponse({"error": "API mutations require application/json"}, status_code=415)
        supplied = request.headers.get("x-vew-csrf", "")
        if not supplied or not hmac.compare_digest(supplied, _CSRF_TOKEN):
            return JSONResponse({"error": "invalid CSRF token"}, status_code=403)
    return await call_next(request)

@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/session")
@app.get("/session")
def session() -> dict[str, str]:
    return {"csrf_token": _CSRF_TOKEN}


@app.get("/projects")
def projects(workspace: str | None = None) -> list[dict[str, Any]]:
    return [project.model_dump(mode="json") for project in find_projects(workspace)]


@app.post("/projects")
def create_and_run(request: RunRequest) -> dict[str, Any]:
    return run_full_pipeline(
        request.source,
        profile=request.profile,
        password=request.password,
        workspace=request.workspace,
        project_id=request.project_id,
        language=request.language,
        skip_asr=request.skip_asr,
        with_vision=request.with_vision,
        max_duration_seconds=request.max_duration_seconds,
    ).model_dump(mode="json")


@app.post("/projects/ingest")
def ingest(request: RunRequest) -> dict[str, Any]:
    return run_ingest_only(
        request.source,
        profile=request.profile,
        password=request.password,
        workspace=request.workspace,
        project_id=request.project_id,
        max_duration_seconds=request.max_duration_seconds,
    ).model_dump(mode="json")


@app.post("/projects/{project_id}/analyze/visual")
def analyze_visual(project_id: str, request: ProjectRequest) -> dict[str, Any]:
    return run_visual(project_id, workspace=request.workspace).model_dump(mode="json")


@app.post("/projects/{project_id}/analyze/audio")
def analyze_audio(project_id: str, request: AudioRequest) -> dict[str, Any]:
    return run_audio(
        project_id,
        workspace=request.workspace,
        language=request.language,
        skip_asr=request.skip_asr,
    ).model_dump(mode="json")


async def _audio_review_dispatch(request: Request, project_id: str, tail: str, workspace: str | None):
    from .store import workspace_path
    from .workspace_api import ApiError, dispatch_api

    query = dict(request.query_params)
    query.pop("workspace", None)
    body = await request.body() if request.method == "PATCH" else b""
    try:
        status, payload = await run_in_threadpool(dispatch_api, workspace_path(workspace), request.method,
                                                 f"/api/projects/{quote(project_id, safe='')}/audio{tail}", urlencode(query), body)
    except ApiError as exc:
        return JSONResponse({"error": {"message": exc.message, "details": exc.details, "status": exc.status}}, status_code=exc.status)
    return JSONResponse(payload, status_code=status)


def _audio_query_openapi() -> dict[str, Any]:
    from .audio_review import MAX_PAGE_SIZE

    fields = {
        "offset": {"type": "integer", "minimum": 0, "default": 0},
        "limit": {"type": "integer", "minimum": 1, "maximum": MAX_PAGE_SIZE, "default": 50},
        "kind": {"type": "string", "enum": ["voice", "music", "sfx", "silence", "mixed"]},
        "review_status": {"type": "string", "enum": ["unreviewed", "reviewed", "rejected", "needs_work", "needs_review"]},
        "shot_id": {"type": "string"},
        "expected_generation_id": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    }
    return {"parameters": [{"name": name, "in": "query", "required": False, "schema": schema} for name, schema in fields.items()]}


@app.get("/api/projects/{project_id}/audio", openapi_extra=_audio_query_openapi())
@app.get("/projects/{project_id}/audio", openapi_extra=_audio_query_openapi())
async def audio_review_list(project_id: str, request: Request, workspace: str | None = None):
    return await _audio_review_dispatch(request, project_id, "", workspace)


@app.get("/api/projects/{project_id}/audio/events/{event_id}")
@app.get("/projects/{project_id}/audio/events/{event_id}")
async def audio_review_event(project_id: str, event_id: str, request: Request, workspace: str | None = None):
    return await _audio_review_dispatch(request, project_id, "/events/" + quote(event_id, safe=""), workspace)


def _audio_review_openapi() -> dict[str, Any]:
    from .audio_review import REVIEW_REQUEST_SCHEMA

    return {"requestBody": {"required": True, "content": {"application/json": {"schema": REVIEW_REQUEST_SCHEMA}}},
            "parameters": [{"name": "X-VEW-CSRF", "in": "header", "required": True, "schema": {"type": "string"}, "description": "Read the local /session token; never a provider credential."}]}


@app.patch("/api/projects/{project_id}/audio/events/{event_id}/review", openapi_extra=_audio_review_openapi())
@app.patch("/projects/{project_id}/audio/events/{event_id}/review", openapi_extra=_audio_review_openapi())
async def audio_review_apply(project_id: str, event_id: str, request: Request, workspace: str | None = None):
    return await _audio_review_dispatch(request, project_id, "/events/" + quote(event_id, safe="") + "/review", workspace)


async def _export_dispatch(
    request: Request,
    project_id: str,
    tail: str,
    workspace: str | None,
):
    from .store import workspace_path
    from .workspace_api import ApiError, dispatch_api

    body = await request.body() if request.method != "GET" else b""
    try:
        status, payload = await run_in_threadpool(
            dispatch_api,
            workspace_path(workspace),
            request.method,
            f"/api/projects/{quote(project_id, safe='')}/exports{tail}",
            "",
            body,
        )
    except ApiError as exc:
        return JSONResponse(
            {"error": {"message": exc.message, "details": exc.details, "status": exc.status}},
            status_code=exc.status,
        )
    return JSONResponse(payload, status_code=status)


@app.get("/api/projects/{project_id}/exports")
@app.get("/projects/{project_id}/exports")
@app.post("/api/projects/{project_id}/exports")
@app.post("/projects/{project_id}/exports")
async def client_exports(project_id: str, request: Request, workspace: str | None = None):
    return await _export_dispatch(request, project_id, "", workspace)


@app.post("/api/projects/{project_id}/exports/cancel")
@app.post("/projects/{project_id}/exports/cancel")
async def client_export_cancel(project_id: str, request: Request, workspace: str | None = None):
    return await _export_dispatch(request, project_id, "/cancel", workspace)


@app.get("/api/projects/{project_id}/exports/state")
@app.get("/projects/{project_id}/exports/state")
async def client_export_state(project_id: str, request: Request, workspace: str | None = None):
    return await _export_dispatch(request, project_id, "/state", workspace)


@app.post("/api/projects/{project_id}/exports/save")
@app.post("/projects/{project_id}/exports/save")
async def client_export_save(project_id: str, request: Request, workspace: str | None = None):
    return await _export_dispatch(request, project_id, "/save", workspace)


@app.post("/api/projects/{project_id}/exports/recover")
@app.post("/projects/{project_id}/exports/recover")
async def client_export_recover(project_id: str, request: Request, workspace: str | None = None):
    return await _export_dispatch(request, project_id, "/recover", workspace)


@app.delete("/api/projects/{project_id}/exports/saved/{version_id}")
@app.delete("/projects/{project_id}/exports/saved/{version_id}")
async def client_export_delete(
    project_id: str,
    version_id: str,
    request: Request,
    workspace: str | None = None,
):
    return await _export_dispatch(
        request,
        project_id,
        "/saved/" + quote(version_id, safe=""),
        workspace,
    )


@app.post("/projects/{project_id}/report")
def report(project_id: str, request: ProjectRequest) -> dict[str, Any]:
    return run_report(project_id, workspace=request.workspace).model_dump(mode="json")


@app.post("/projects/{project_id}/analyze/vision")
def analyze_vision(project_id: str, request: VisionRequest) -> dict[str, Any]:
    return run_vision(
        project_id,
        workspace=request.workspace,
        model=request.model,
        limit=request.limit,
        provider=request.provider.value if request.provider is not None else None,
    ).model_dump(mode="json")
