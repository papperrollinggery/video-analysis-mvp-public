from __future__ import annotations

import base64
import json
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .config import load_runtime_config
from .paths import ProjectPaths
from .schemas import Shot, StatusEnvelope, dump_json, load_json


VISION_SYSTEM_PROMPT = """You are a professional film-analysis assistant for AI video creators.
Analyze one frame from a video shot. Return ONLY valid JSON.
Do not invent lens or equipment when it is not visually inferable.
Use concise production vocabulary. The output should be useful as a TapNow-style shot breakdown and reusable video-generation prompt.
"""


def annotate_project_with_vision(
    paths: ProjectPaths,
    model: str | None = None,
    base_url: str | None = None,
    limit: int | None = None,
    provider: str | None = None,
) -> StatusEnvelope:
    runtime_config = load_runtime_config(paths.root.parent)
    selected_provider = (
        provider or os.getenv("VIDEO_ANALYSIS_VISION_PROVIDER") or runtime_config.vision_provider or "openai"
    ).strip().lower()
    if selected_provider in {"minimax", "minimax_mcp", "minimax-mcp"}:
        return annotate_project_with_minimax_mcp(paths, limit=limit)
    api_key = os.getenv("OPENAI_API_KEY") or runtime_config.openai_api_key
    if not api_key:
        return StatusEnvelope(
            status="error",
            summary="Vision annotation requires OPENAI_API_KEY.",
            next_actions=["Set OPENAI_API_KEY and rerun `analyze-video vision <project-id>`."],
            artifacts={"shots": str(paths.data / "shots.json")},
            error="OPENAI_API_KEY is missing",
        )
    shots = [Shot.model_validate(item) for item in load_json(paths.data / "shots.json")]
    selected = shots[:limit] if limit else shots
    for shot in selected:
        frame_path = paths.keyframes / shot.frame_ref
        if not frame_path.exists():
            continue
        data = analyze_frame(
            frame_path,
            shot,
            api_key,
            model=model or runtime_config.openai_model,
            base_url=base_url or runtime_config.openai_base_url,
        )
        apply_vision_data(shot, data)
    dump_json(paths.data / "shots.json", shots)
    dump_json(paths.data / "vision_annotations.json", [shot.model_dump(mode="json") for shot in selected])
    return StatusEnvelope(
        status="success",
        summary=f"Annotated {len(selected)} shots with vision analysis.",
        next_actions=["Regenerate the report to refresh TapNow-style tables."],
        artifacts={
            "shots": str(paths.data / "shots.json"),
            "vision_annotations": str(paths.data / "vision_annotations.json"),
        },
    )


def annotate_project_with_minimax_mcp(paths: ProjectPaths, limit: int | None = None) -> StatusEnvelope:
    runtime_config = load_runtime_config(paths.root.parent)
    api_key = os.getenv("MINIMAX_API_KEY") or runtime_config.minimax_api_key or _load_minimax_config_key()
    if not api_key:
        return StatusEnvelope(
            status="error",
            summary="MiniMax MCP vision annotation requires MINIMAX_API_KEY.",
            next_actions=[
                "Set MINIMAX_API_KEY.",
                "For China keys, set MINIMAX_API_HOST=https://api.minimaxi.com.",
                "Rerun `analyze-video vision <project-id> --provider minimax_mcp`.",
            ],
            artifacts={"shots": str(paths.data / "shots.json")},
            error="MINIMAX_API_KEY is missing",
        )
    shots = [Shot.model_validate(item) for item in load_json(paths.data / "shots.json")]
    selected = shots[:limit] if limit else shots
    for shot in selected:
        frame_path = paths.keyframes / shot.frame_ref
        if not frame_path.exists():
            continue
        data = analyze_frame_with_minimax_mcp(frame_path, shot, api_key, runtime_config.minimax_api_host)
        apply_vision_data(shot, data)
        shot.review_notes = "MiniMax MCP vision annotated; review before final client delivery"
    dump_json(paths.data / "shots.json", shots)
    dump_json(paths.data / "vision_annotations.json", [shot.model_dump(mode="json") for shot in selected])
    return StatusEnvelope(
        status="success",
        summary=f"Annotated {len(selected)} shots with MiniMax MCP image understanding.",
        next_actions=["Regenerate the report to refresh shot tables."],
        artifacts={
            "shots": str(paths.data / "shots.json"),
            "vision_annotations": str(paths.data / "vision_annotations.json"),
        },
    )


def analyze_frame(frame_path: Path, shot: Shot, api_key: str, model: str | None, base_url: str | None) -> dict[str, Any]:
    endpoint = (base_url or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/") + "/chat/completions"
    selected_model = model or os.getenv("VIDEO_ANALYSIS_VISION_MODEL") or "gpt-5.4-mini"
    image_url = "data:image/jpeg;base64," + base64.b64encode(frame_path.read_bytes()).decode("ascii")
    user_prompt = {
        "shot_no": shot.shot_no,
        "timecode": shot.timecode,
        "required_json_fields": [
            "content_summary",
            "scene_type",
            "shot_scale",
            "camera_angle",
            "camera_motion",
            "composition",
            "subject",
            "action",
            "location",
            "int_ext",
            "props",
            "lighting_vfx",
            "style_notes",
            "prompt_en",
            "prompt_zh",
            "confidence",
        ],
        "notes": "If uncertain, use 'uncertain' and lower confidence. Do not claim lens/equipment unless visible.",
    }
    payload = {
        "model": selected_model,
        "messages": [
            {"role": "system", "content": VISION_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": json.dumps(user_prompt, ensure_ascii=False)},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            },
        ],
        "response_format": {"type": "json_object"},
    }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Vision API request failed: {exc.code} {body}") from exc
    content = result["choices"][0]["message"]["content"]
    return json.loads(content)


def analyze_frame_with_minimax_mcp(frame_path: Path, shot: Shot, api_key: str, api_host: str | None = None) -> dict[str, Any]:
    prompt = {
        "role": "professional film shot analyst",
        "task": "Analyze this single keyframe from a video shot. Return ONLY valid JSON.",
        "shot_no": shot.shot_no,
        "timecode": shot.timecode,
        "required_json_fields": [
            "content_summary",
            "scene_type",
            "shot_scale",
            "camera_angle",
            "camera_motion",
            "composition",
            "subject",
            "action",
            "location",
            "int_ext",
            "props",
            "lighting_vfx",
            "style_notes",
            "prompt_en",
            "prompt_zh",
            "confidence",
        ],
        "rules": [
            "Do not invent lens or equipment when not visually inferable.",
            "Use concise production vocabulary.",
            "If uncertain, use 'uncertain' and lower confidence.",
            "The output should be useful as a TapNow-style shot breakdown.",
        ],
    }
    response = _call_minimax_understand_image(
        str(frame_path.resolve()),
        json.dumps(prompt, ensure_ascii=False),
        api_key,
        api_host=api_host,
    )
    return _parse_jsonish(response)


def _call_minimax_understand_image(image_source: str, prompt: str, api_key: str, api_host: str | None = None) -> str:
    host = os.getenv("MINIMAX_API_HOST") or api_host or "https://api.minimaxi.com"
    env = {
        **os.environ,
        "MINIMAX_API_KEY": api_key,
        "MINIMAX_API_HOST": host,
        "MINIMAX_MCP_BASE_PATH": os.getenv(
            "MINIMAX_MCP_BASE_PATH",
            str(Path.home() / ".openclaw" / "workspace" / "minimax-output"),
        ),
    }
    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "video-analysis-mvp", "version": "1.0"},
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "understand_image",
                "arguments": {"image_source": image_source, "prompt": prompt},
            },
        },
    ]
    proc = subprocess.Popen(
        ["uvx", "minimax-coding-plan-mcp", "-y"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )
    assert proc.stdin is not None
    assert proc.stdout is not None
    for request in requests:
        proc.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
        proc.stdin.flush()
    proc.stdin.close()
    tool_response: dict[str, Any] | None = None
    for line in proc.stdout:
        try:
            response = json.loads(line)
        except json.JSONDecodeError:
            continue
        if response.get("id") == 2:
            tool_response = response
            break
    stderr = proc.stderr.read() if proc.stderr else ""
    proc.wait(timeout=180)
    if not tool_response:
        raise RuntimeError(f"MiniMax MCP did not return a tool response. {stderr}".strip())
    if "error" in tool_response:
        raise RuntimeError(f"MiniMax MCP error: {tool_response['error']}")
    text = _mcp_result_text(tool_response.get("result"))
    if "Failed to perform" in text or "API Error:" in text or "invalid api key" in text.lower():
        raise RuntimeError(f"MiniMax MCP returned an error: {text}")
    return text


def _mcp_result_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        if isinstance(result.get("data"), str):
            return result["data"]
        content = result.get("content")
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
            if parts:
                return "\n".join(parts)
        return json.dumps(result, ensure_ascii=False)
    return json.dumps(result, ensure_ascii=False)


def _parse_jsonish(text: str) -> dict[str, Any]:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise


def _load_minimax_config_key() -> str | None:
    config_path = Path.home() / ".openclaw" / "config" / "minimax.json"
    if not config_path.exists():
        return None
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    key = data.get("api_key")
    return key if isinstance(key, str) and key.strip() else None


def apply_vision_data(shot: Shot, data: dict[str, Any]) -> None:
    for field in [
        "content_summary",
        "scene_type",
        "shot_scale",
        "camera_angle",
        "camera_motion",
        "composition",
        "subject",
        "action",
        "location",
        "int_ext",
        "props",
        "lighting_vfx",
        "style_notes",
        "prompt_en",
        "prompt_zh",
    ]:
        value = data.get(field)
        if isinstance(value, str) and value.strip():
            setattr(shot, field, value.strip())
    if shot.content_summary:
        shot.visual_description = shot.content_summary
    confidence = data.get("confidence")
    if isinstance(confidence, (int, float)):
        shot.confidence = max(0.0, min(1.0, float(confidence)))
    shot.review_notes = "vision annotated; review before final client delivery"
