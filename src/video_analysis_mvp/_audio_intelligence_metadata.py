from __future__ import annotations

import math
import re
import unicodedata
from typing import Any

MAX_TEXT_BYTES = 16 * 1024
MAX_DIAGNOSTIC_BYTES = 4 * 1024
MAX_PARAMETER_NODES = 1_000
MAX_PARAMETER_DEPTH = 8

PARAMETER_KEY_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,127}")
ABSOLUTE_PATH_PATTERN = re.compile(
    r"(?i)(?:"
    r"file:(?://)?/[A-Za-z0-9~._-]\S*"
    r"|(?<![A-Za-z0-9])~[/\\]\S*"
    r"|(?<![A-Za-z0-9/])/(?!/)(?:[^\s/]+/)*[^\s,;)]*"
    r"|(?<![A-Za-z0-9])[A-Za-z]:[/\\]\S*"
    r"|(?<![A-Za-z0-9])\\\\[^\\\s]+\\\S*"
    r")"
)
SENSITIVE_VALUE_PATTERN = re.compile(
    r"(?i)(?:"
    r"\b(?:api[ _-]*key|access[ _-]*(?:key|token)|auth(?:orization|[ _-]*header)?|"
    r"client[ _-]*secret|password|private[ _-]*key|secret(?:[ _-]*key)?|token)\b[\"']?\s*[:=]\s*\S+"
    r"|\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{6,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|\bgh[pousr]_[A-Za-z0-9]{20,}\b"
    r"|\bsk-(?:proj-)?[A-Za-z0-9_-]{16,}\b"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
    r")"
)
FORBIDDEN_PARAMETER_KEYS_COMPACT = frozenset(
    {
        "apikey",
        "accesskey",
        "accesstoken",
        "auth",
        "authorization",
        "authheader",
        "clientsecret",
        "credential",
        "credentials",
        "password",
        "privatekey",
        "secret",
        "secretkey",
        "token",
    }
)


def validate_parameters(value: Any) -> dict[str, Any]:
    if type(value) is not dict:
        raise ValueError("audio intelligence parameters must be an object")
    count = [0]
    normalized = _validate_json_value(value, depth=0, count=count, path="parameters")
    if type(normalized) is not dict:
        raise ValueError("audio intelligence parameters must be an object")
    return normalized


def bounded_text(
    value: Any,
    label: str,
    *,
    maximum: int = MAX_TEXT_BYTES,
    allow_empty: bool = True,
    forbid_private_path: bool = False,
    forbid_sensitive_value: bool = False,
) -> str:
    if (
        type(value) is not str
        or "\x00" in value
        or len(value.encode("utf-8")) > maximum
    ):
        raise ValueError(f"{label} is invalid")
    if not allow_empty and not value.strip():
        raise ValueError(f"{label} must not be empty")
    inspection_value = unicodedata.normalize("NFKC", value).translate(
        str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'"})
    )
    if forbid_private_path and ABSOLUTE_PATH_PATTERN.search(inspection_value):
        raise ValueError(f"{label} must not contain a private absolute path")
    if forbid_sensitive_value and SENSITIVE_VALUE_PATTERN.search(inspection_value):
        raise ValueError(f"{label} must not contain credential-shaped data")
    return value


def optional_metadata(value: Any, label: str) -> str | None:
    if value is None:
        return None
    return bounded_text(
        value,
        f"audio source {label}",
        maximum=512,
        allow_empty=False,
        forbid_private_path=True,
        forbid_sensitive_value=True,
    )


def _validate_json_value(value: Any, *, depth: int, count: list[int], path: str) -> Any:
    count[0] += 1
    if count[0] > MAX_PARAMETER_NODES or depth > MAX_PARAMETER_DEPTH:
        raise ValueError("audio intelligence parameters exceed structural limits")
    if value is None or type(value) is bool or type(value) is int:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"audio intelligence parameter is non-finite: {path}")
        return value
    if type(value) is str:
        return bounded_text(
            value,
            f"audio intelligence parameter {path}",
            forbid_private_path=True,
            forbid_sensitive_value=True,
        )
    if type(value) is list:
        return [
            _validate_json_value(
                item, depth=depth + 1, count=count, path=f"{path}[{index}]"
            )
            for index, item in enumerate(value)
        ]
    if type(value) is dict:
        result: dict[str, Any] = {}
        if any(
            type(key) is not str
            or PARAMETER_KEY_PATTERN.fullmatch(
                unicodedata.normalize("NFKC", key).strip()
            )
            is None
            for key in value
        ):
            raise ValueError(f"audio intelligence parameter key is invalid: {path}")
        for key in sorted(value):
            compact_key = _compact_parameter_key(key)
            if any(
                compact_key == forbidden or compact_key.endswith(forbidden)
                for forbidden in FORBIDDEN_PARAMETER_KEYS_COMPACT
            ):
                raise ValueError(
                    f"audio intelligence parameter may contain a secret: {key}"
                )
            result[key] = _validate_json_value(
                value[key],
                depth=depth + 1,
                count=count,
                path=f"{path}.{key}",
            )
        return result
    raise ValueError(f"audio intelligence parameter type is unsupported: {path}")


def _compact_parameter_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip().lower()
    return re.sub(r"[^a-z0-9]+", "", normalized)
