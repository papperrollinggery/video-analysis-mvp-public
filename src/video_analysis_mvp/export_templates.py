"""Versioned fixed client template loading and deterministic layout preflight."""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

from .client_export_dataset import (
    ClientExportDatasetError,
    _canonical_digest,
    _client_text,
    validate_client_export_dataset,
)
from .image_evidence import inspect_image_bytes
from .safe_io import read_regular_bytes

TEMPLATE_DIR = Path(__file__).parent / "templates" / "client"
TEMPLATE_FILES = ("manifest.json", "design_tokens.json", "layout.json")
EXPECTED_TEMPLATE_HASHES = {
    "manifest.json": "3e547ba87d7a8c22e807266bc28c12d07befb5cbc361fb22dd3297c63682511c",
    "design_tokens.json": "c37931165030b18f9cd0dc91fa131226e80426f8f53721a3828ca0a7ce7ded9d",
    "layout.json": "1bd023093e46155b9e4374bc35c4c7d4c47874e583e3c1bf56b71da15958e1f9",
}
PREFLIGHT_SCHEMA = "client-layout-preflight/v1"
FORMATS = frozenset({"xlsx", "pdf", "html"})
LANGUAGES = frozenset({"zh", "en", "bilingual"})
DENSITIES = frozenset({"client", "compact"})
CJK_RE = re.compile(r"[\u2e80-\u9fff\uf900-\ufaff]")


class ExportTemplateError(ValueError):
    pass


def load_client_template() -> dict[str, Any]:
    raw: dict[str, bytes] = {}
    parsed: dict[str, Any] = {}
    for name in TEMPLATE_FILES:
        path = TEMPLATE_DIR / name
        try:
            payload = read_regular_bytes(path, root=TEMPLATE_DIR, max_bytes=1024 * 1024)
        except (OSError, ValueError) as exc:
            raise ExportTemplateError(f"required template asset is unavailable: {name}") from exc
        if hashlib.sha256(payload).hexdigest() != EXPECTED_TEMPLATE_HASHES[name]:
            raise ExportTemplateError(f"template asset does not match version 1.0.0: {name}")
        try:
            value = json.loads(payload, parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)))
        except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise ExportTemplateError(f"template asset is invalid JSON: {name}") from exc
        if type(value) is not dict:
            raise ExportTemplateError(f"template asset must be an object: {name}")
        raw[name], parsed[name] = payload, value
    manifest, tokens, layout = parsed["manifest.json"], parsed["design_tokens.json"], parsed["layout.json"]
    _validate_template(manifest, tokens, layout)
    digests = {name: hashlib.sha256(raw[name]).hexdigest() for name in TEMPLATE_FILES}
    return {
        **manifest,
        "design_tokens": tokens,
        "layout": layout,
        "asset_digests": digests,
        "template_digest": _canonical_digest({"manifest": manifest, "asset_digests": digests}),
    }


def preflight_client_layout(
    dataset: dict[str, Any],
    settings: dict[str, Any] | None = None,
    *,
    project_root: Path | None = None,
    available_fonts: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    dataset = validate_client_export_dataset(dataset)
    template = load_client_template()
    options = _settings(settings or {}, template, project_root)
    layout, tokens = template["layout"], template["design_tokens"]
    limits = layout["limits"]
    shots, events = dataset["shots"], dataset["audio"]["events"]
    if len(shots) > limits["maximum_shots"] or len(events) > limits["maximum_audio_events"]:
        raise ExportTemplateError("dataset exceeds the template record limits")
    if "xlsx" in options["formats"]:
        xlsx_limits = layout["renderer_limits"]["xlsx"]
        if len(shots) > xlsx_limits["maximum_shots"] or len(events) > xlsx_limits["maximum_audio_events"]:
            raise ExportTemplateError("dataset exceeds the measured XLSX renderer capacity")
    for shot in shots:
        if (shot["frame"]["size_bytes"] or 0) > limits["maximum_embedded_image_bytes"]:
            raise ExportTemplateError(f"shot {shot['shot_id']} exceeds the per-image byte limit")
    image_bytes = sum(shot["frame"]["size_bytes"] or 0 for shot in shots)
    if image_bytes > limits["maximum_total_image_bytes"]:
        raise ExportTemplateError("embedded image budget exceeds the template limit")
    warnings: list[str] = []
    if options["logo"] is None:
        warnings.append("Optional client logo omitted.")
    cjk_required = (
        options["language"] != "en"
        or any(CJK_RE.search(cell["text"]) for cell in _text_cells(dataset))
        or bool(CJK_RE.search(options["project_subtitle"]["text"]))
    )
    font_stack = tokens["typography"]["font_stack"]
    available = sorted({str(item) for item in available_fonts})
    selected_cjk = next((font for font in tokens["typography"]["cjk_font_candidates"] if font in available), None)
    if cjk_required and "pdf" in options["formats"] and selected_cjk is None:
        raise ExportTemplateError("A declared CJK font is required for PDF layout preflight")
    if cjk_required and selected_cjk is None and "xlsx" in options["formats"]:
        warnings.append("CJK font was not verified; XLSX declares the template fallback stack and the spreadsheet app may substitute it.")

    density_variant = "client-landscape-4" if options["density"] == "client" else "compact-landscape-8"
    per_page = layout["variants"][density_variant]["shots_per_page"]
    shot_plan = []
    continuation_total = 0
    for shot in shots:
        lines = sum(_estimated_lines(cell["text"], layout) for cell in shot["text"].values())
        lines += sum(_estimated_lines(cell["text"], layout) for cell in shot["camera"].values())
        maximum = layout["variants"]["text-continuation"]["maximum_lines_per_block"]
        continuation = max(0, math.ceil(lines / maximum) - 1)
        if continuation > limits["maximum_continuation_blocks_per_record"]:
            raise ExportTemplateError(f"shot {shot['shot_id']} exceeds the continuation block limit")
        continuation_total += continuation
        shot_plan.append({
            "shot_id": shot["shot_id"],
            "primary_variant": density_variant,
            "frame_variant": density_variant if shot["frame"]["present"] else "missing-frame",
            "estimated_text_lines": lines,
            "continuation_blocks": continuation,
        })
    block_plan = []
    shot_cell_ids = {id(cell) for shot in shots for cell in [*shot["text"].values(), *shot["camera"].values()]}
    maximum = layout["variants"]["text-continuation"]["maximum_lines_per_block"]
    for index, cell in enumerate(_text_cells(dataset)):
        if id(cell) in shot_cell_ids or cell["is_blank"]:
            continue
        lines = _estimated_lines(cell["text"], layout)
        continuation = max(0, math.ceil(lines / maximum) - 1)
        if continuation > limits["maximum_continuation_blocks_per_record"]:
            raise ExportTemplateError(f"renderer text block {index} exceeds the continuation block limit")
        block_plan.append({"block_index": index, "estimated_text_lines": lines, "continuation_blocks": continuation})
    subtitle = options["project_subtitle"]
    if not subtitle["is_blank"]:
        lines = _estimated_lines(subtitle["text"], layout)
        continuation = max(0, math.ceil(lines / maximum) - 1)
        if continuation > limits["maximum_continuation_blocks_per_record"]:
            raise ExportTemplateError("project subtitle exceeds the continuation block limit")
        block_plan.append({"block_index": "project_subtitle", "estimated_text_lines": lines, "continuation_blocks": continuation})
    non_shot_continuations = sum(item["continuation_blocks"] for item in block_plan)
    page_count = math.ceil(len(shots) / per_page) + continuation_total if shots else 0
    base = {
        "schema_id": PREFLIGHT_SCHEMA,
        "template": {
            "template_id": template["template_id"],
            "template_version": template["template_version"],
            "template_digest": template["template_digest"],
            "asset_digests": template["asset_digests"],
        },
        "dataset": {"schema_id": dataset["schema_id"], "dataset_id": dataset["dataset_id"], "dataset_digest": dataset["dataset_digest"]},
        "status": "ready",
        "settings": options,
        "typography": {
            "minimum_font_pt": tokens["typography"]["minimum_pt"],
            "font_stack": font_stack,
        },
        "font_plan": {"cjk_required": cjk_required, "selected_cjk_font": selected_cjk, "available_fonts": available},
        "metrics": {
            "shot_count": len(shots), "audio_event_count": len(events), "present_frame_count": sum(shot["frame"]["present"] for shot in shots),
            "missing_frame_count": sum(not shot["frame"]["present"] for shot in shots), "embedded_image_bytes": image_bytes,
            "estimated_storyboard_pages": page_count,
            "storyboard_continuation_block_count": continuation_total,
            "non_storyboard_continuation_block_count": non_shot_continuations,
            "continuation_block_count": continuation_total + non_shot_continuations,
        },
        "shot_plan": shot_plan,
        "text_block_plan": block_plan,
        "warnings": warnings,
        "render_side_effects": False,
    }
    return {**base, "preflight_digest": _canonical_digest(base)}


def _settings(value: dict[str, Any], template: dict[str, Any], project_root: Path | None) -> dict[str, Any]:
    allowed = {"language", "density", "formats", "project_subtitle", "logo_path", "accent_color"}
    if type(value) is not dict or set(value) - allowed:
        raise ExportTemplateError("template settings contain unsupported fields")
    language = value.get("language", template["default_language"])
    density = value.get("density", template["default_density"])
    formats = value.get("formats", ["xlsx", "pdf"])
    if type(language) is not str or type(density) is not str or type(formats) is not list or any(type(item) is not str for item in formats):
        raise ExportTemplateError("template language, density or formats have invalid types")
    if language not in LANGUAGES or density not in DENSITIES or not formats or any(item not in FORMATS for item in formats) or len(formats) != len(set(formats)):
        raise ExportTemplateError("template language, density or formats are invalid")
    raw_accent = value["accent_color"] if "accent_color" in value else template["design_tokens"]["colors"]["accent"]
    if type(raw_accent) is not str or type(value.get("project_subtitle", "")) is not str:
        raise ExportTemplateError("template subtitle/accent have invalid types")
    if raw_accent == "":
        raw_accent = template["design_tokens"]["colors"]["accent"]
    accent = raw_accent.upper()
    if re.fullmatch(r"#[0-9A-F]{6}", accent) is None or min(_contrast(accent, "#FFFFFF"), _contrast(accent, template["design_tokens"]["colors"]["paper"])) < 3.0:
        raise ExportTemplateError("template accent color does not meet the minimum contrast")
    try:
        subtitle = _client_text(value.get("project_subtitle", ""), "project subtitle")
    except ClientExportDatasetError as exc:
        raise ExportTemplateError("project subtitle is invalid") from exc
    logo = _logo(value.get("logo_path"), project_root)
    return {"language": language, "density": density, "formats": sorted(formats), "project_subtitle": subtitle, "accent_color": accent, "logo": logo}


def _logo(value: Any, project_root: Path | None) -> dict[str, Any] | None:
    if value is None or value == "":
        return None
    if type(value) is not str or project_root is None:
        raise ExportTemplateError("logo requires an explicit project root")
    root, candidate = project_root.resolve(), Path(value)
    candidate = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        raise ExportTemplateError("logo must remain inside the project root") from None
    try:
        raw = read_regular_bytes(candidate, root=root, max_bytes=16 * 1024 * 1024)
    except (OSError, ValueError) as exc:
        raise ExportTemplateError("logo is missing, unsafe, or exceeds the byte limit") from exc
    try:
        evidence = inspect_image_bytes(raw, max_bytes=16 * 1024 * 1024)
    except ValueError as exc:
        raise ExportTemplateError(f"logo is not a valid local PNG/JPEG: {exc}") from exc
    return {"path": relative.as_posix(), **evidence.receipt_fields()}


def _estimated_lines(text: str, layout: dict[str, Any]) -> int:
    if not text:
        return 0
    config = layout["line_estimation"]
    total = 0
    for line in re.split(r"\r\n|\r|\n", text):
        cjk = len(CJK_RE.findall(line))
        latin = max(0, len(line) - cjk)
        total += max(1, math.ceil(cjk / config["cjk_characters_per_line"]) + math.ceil(latin / config["latin_characters_per_line"]))
    return total


def _text_cells(value: Any):
    if type(value) is dict:
        if set(value) == {"text", "spreadsheet_text", "is_blank", "formula_neutralized"}:
            yield value
        else:
            for item in value.values():
                yield from _text_cells(item)
    elif type(value) is list:
        for item in value:
            yield from _text_cells(item)


def _validate_template(manifest: dict[str, Any], tokens: dict[str, Any], layout: dict[str, Any]) -> None:
    if manifest.get("template_id") != "client-storyboard" or manifest.get("template_version") != "1.0.0" or manifest.get("compatible_dataset_schemas") != ["client-export-dataset/v1"]:
        raise ExportTemplateError("client template identity/compatibility is invalid")
    if manifest.get("required_files") != ["design_tokens.json", "layout.json"] or len(manifest.get("workbook_sheets") or []) != 5 or len(manifest.get("pdf_sections") or []) != 7:
        raise ExportTemplateError("client template sections are invalid")
    if type(tokens.get("colors")) is not dict or type(tokens.get("typography")) is not dict or type(tokens.get("spacing")) is not dict:
        raise ExportTemplateError("client design tokens are incomplete")
    if tokens["typography"].get("minimum_pt") != 8.5:
        raise ExportTemplateError("client template minimum typography must be 8.5 pt")
    if type(layout.get("variants")) is not dict or type(layout.get("limits")) is not dict or type(layout.get("fallbacks")) is not dict:
        raise ExportTemplateError("client layout rules are incomplete")
    if layout.get("renderer_limits") != {
        "xlsx": {"maximum_shots": 3200, "maximum_audio_events": 8000}
    }:
        raise ExportTemplateError("client renderer capacity limits are invalid")
    required_variants = {"client-landscape-4", "compact-landscape-8", "text-continuation", "missing-frame", "audio-timeline-wide"}
    if set(layout["variants"]) != required_variants or layout["limits"].get("minimum_font_pt") != tokens["typography"]["minimum_pt"]:
        raise ExportTemplateError("client layout variants or typography floor are invalid")


def _luminance(color: str) -> float:
    values = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4 for value in values]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(first: str, second: str) -> float:
    light, dark = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


__all__ = ["ExportTemplateError", "load_client_template", "preflight_client_layout"]
