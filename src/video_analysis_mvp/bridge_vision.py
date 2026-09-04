"""Explicit loopback BridgeDeck Responses transport; no credential discovery."""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from typing import Any

from .config import validate_bridgedeck_config
from .image_evidence import MAX_IMAGE_BYTES, inspect_image_bytes

BRIDGE_TIMEOUT_SECONDS = 120
MAX_BRIDGE_RESPONSE_BYTES = 2 * 1024 * 1024
BRIDGE_PROVIDER_CONTRACT = {
    "protocol": "responses",
    "authentication": "local_bridge_owned",
    "upstream_token_limit_enforced": False,
    "model_identity": "response_field_matches_requested",
}


class BridgeDeckError(ValueError):
    """Controlled, credential-free compatibility or transport diagnostic."""


def analyze_bridgedeck_image(
    *,
    base_url: str,
    model: str,
    image_bytes: bytes,
    media_type: str,
    instructions: str,
    prompt: dict[str, Any],
    required_fields: list[str],
) -> dict[str, Any]:
    endpoint, selected_model = validate_bridgedeck_config(base_url, model)
    if (
        type(image_bytes) is not bytes
        or not image_bytes
        or len(image_bytes) > MAX_IMAGE_BYTES
    ):
        raise BridgeDeckError("BridgeDeck image input must be a validated PNG or JPEG")
    try:
        image = inspect_image_bytes(image_bytes)
    except ValueError:
        raise BridgeDeckError(
            "BridgeDeck image input is not a complete valid raster"
        ) from None
    if image.media_type != media_type:
        raise BridgeDeckError("BridgeDeck image media type does not match its bytes")
    body = build_bridgedeck_request(
        model=selected_model,
        image_bytes=image_bytes,
        media_type=media_type,
        instructions=instructions,
        prompt=prompt,
        required_fields=required_fields,
    )
    request = urllib.request.Request(
        endpoint + "/responses",
        data=json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    # Numeric loopback must not be redirected or routed through ambient proxies.
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _NoRedirectHandler()
    )
    try:
        with opener.open(request, timeout=BRIDGE_TIMEOUT_SECONDS) as response:  # nosec B310
            raw = response.read(MAX_BRIDGE_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        raise BridgeDeckError(f"BridgeDeck request failed with HTTP {code}") from None
    except (urllib.error.URLError, OSError):
        raise BridgeDeckError(
            "BridgeDeck loopback request could not complete"
        ) from None
    if len(raw) > MAX_BRIDGE_RESPONSE_BYTES:
        raise BridgeDeckError("BridgeDeck response exceeds the bounded size")
    return parse_bridgedeck_response(raw, selected_model)


def build_bridgedeck_request(
    *,
    model: str,
    image_bytes: bytes,
    media_type: str,
    instructions: str,
    prompt: dict[str, Any],
    required_fields: list[str],
) -> dict[str, Any]:
    properties = {
        field: {"type": "string", "minLength": 1}
        for field in required_fields
        if field != "confidence"
    }
    properties["confidence"] = {"type": "number", "minimum": 0, "maximum": 1}
    return {
        "model": model,
        "instructions": instructions,
        "store": False,
        "stream": False,
        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": json.dumps(prompt, ensure_ascii=False),
                    },
                    {
                        "type": "input_image",
                        "image_url": f"data:{media_type};base64,{base64.b64encode(image_bytes).decode('ascii')}",
                        "detail": "auto",
                    },
                ],
            }
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "shot_observation",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": properties,
                    "required": required_fields,
                    "additionalProperties": False,
                },
            }
        },
    }


def parse_bridgedeck_response(raw: bytes, requested_model: str) -> dict[str, Any]:
    response = _strict_object(raw)
    if (
        response.get("status") != "completed"
        or response.get("error") is not None
        or response.get("incomplete_details") is not None
    ):
        raise BridgeDeckError("BridgeDeck response is incomplete or failed")
    if response.get("model") != requested_model:
        raise BridgeDeckError("BridgeDeck returned a different or unreported model")
    output = response.get("output")
    if type(output) is not list:
        raise BridgeDeckError("BridgeDeck response output must be an array")
    messages: list[dict[str, Any]] = []
    for item in output:
        if type(item) is not dict or item.get("type") not in {"reasoning", "message"}:
            raise BridgeDeckError("BridgeDeck returned an unexpected output item")
        if item["type"] == "message":
            messages.append(item)
    if (
        len(messages) != 1
        or messages[0].get("role") != "assistant"
        or messages[0].get("status") != "completed"
    ):
        raise BridgeDeckError(
            "BridgeDeck must return one completed assistant observation"
        )
    content = messages[0].get("content")
    if type(content) is not list or not content:
        raise BridgeDeckError("BridgeDeck assistant content is missing")
    text: list[str] = []
    for part in content:
        if (
            type(part) is not dict
            or part.get("type") != "output_text"
            or type(part.get("text")) is not str
        ):
            raise BridgeDeckError(
                "BridgeDeck returned a refusal or unsupported content"
            )
        text.append(part["text"])
    return _strict_object("".join(text).encode("utf-8"))


def _strict_object(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, ValueError):
        raise BridgeDeckError("BridgeDeck response must contain strict JSON") from None
    if type(value) is not dict:
        raise BridgeDeckError("BridgeDeck response must be a JSON object")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("non-finite JSON number")


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise urllib.error.HTTPError(
            req.full_url, code, "Redirects are disabled", headers, fp
        )
