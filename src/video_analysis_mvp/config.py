from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RuntimeConfig:
    vision_provider: str = "openai"
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-5.4-mini"
    minimax_api_key: str = ""
    minimax_api_host: str = "https://api.minimaxi.com"


def config_path(workspace_root: Path) -> Path:
    return workspace_root / "_settings" / "runtime_config.json"


def load_runtime_config(workspace_root: Path) -> RuntimeConfig:
    path = config_path(workspace_root)
    if not path.exists():
        return RuntimeConfig()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return RuntimeConfig()
    return RuntimeConfig(
        vision_provider=str(data.get("vision_provider") or "openai"),
        openai_api_key=str(data.get("openai_api_key") or ""),
        openai_base_url=str(data.get("openai_base_url") or "https://api.openai.com/v1"),
        openai_model=str(data.get("openai_model") or "gpt-5.4-mini"),
        minimax_api_key=str(data.get("minimax_api_key") or ""),
        minimax_api_host=str(data.get("minimax_api_host") or "https://api.minimaxi.com"),
    )


def save_runtime_config(workspace_root: Path, updates: dict[str, str], keep_blank_secrets: bool = True) -> RuntimeConfig:
    current = load_runtime_config(workspace_root)
    data = {
        "vision_provider": updates.get("vision_provider") or current.vision_provider,
        "openai_api_key": _secret_value(updates.get("openai_api_key", ""), current.openai_api_key, keep_blank_secrets),
        "openai_base_url": updates.get("openai_base_url") or current.openai_base_url,
        "openai_model": updates.get("openai_model") or current.openai_model,
        "minimax_api_key": _secret_value(updates.get("minimax_api_key", ""), current.minimax_api_key, keep_blank_secrets),
        "minimax_api_host": updates.get("minimax_api_host") or current.minimax_api_host,
    }
    path = config_path(workspace_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return load_runtime_config(workspace_root)


def _secret_value(new_value: str, current_value: str, keep_blank: bool) -> str:
    if new_value.strip():
        return new_value.strip()
    return current_value if keep_blank else ""


def mask_secret(value: str) -> str:
    if not value:
        return "Not configured"
    if len(value) <= 10:
        return "Configured"
    return f"{value[:5]}...{value[-4:]}"
