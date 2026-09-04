"""Self-contained client print HTML and explicit Playwright PDF rendering."""

from __future__ import annotations

import base64
import hashlib
import html
import io
import json
import os
import tempfile
import unicodedata
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PIL import ImageFont

from .client_export_dataset import (
    ClientExportDatasetError,
    _canonical_digest,
    validate_client_export_dataset,
)
from .export_templates import ExportTemplateError, preflight_client_layout
from .image_evidence import inspect_image_bytes
from .safe_io import read_regular_bytes
from .utils import ProcessCancelledError, ToolError, run_command

HTML_RECEIPT_SCHEMA = "html-render-receipt/v1"
PDF_RECEIPT_SCHEMA = "pdf-render-receipt/v1"
MAX_OUTPUT_BYTES = 512 * 1024 * 1024
MAX_RENDERER_LOG_BYTES = 2 * 1024 * 1024
DRIVER_PATH = Path(__file__).parent / "templates" / "client" / "render_pdf.cjs"
DRIVER_SHA256 = "d725a851d56a86684fd26adcdff9a5493c85a0a03207394926cd58e39b7615fc"


class PdfExportError(ValueError):
    pass


def render_client_html(
    dataset: dict[str, Any],
    output_path: Path,
    *,
    settings: dict[str, Any] | None = None,
    project_root: Path,
    available_fonts: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    output = _new_output(output_path, ".html")
    dataset, plan = _prepare(dataset, settings, project_root, available_fonts, output_format="html")
    rendered, stats = _html_document(dataset, plan, Path(project_root).resolve())
    payload = rendered.encode("utf-8")
    _write_exclusive(output, payload)
    base = {
        "schema_id": HTML_RECEIPT_SCHEMA,
        "template_id": plan["template"]["template_id"],
        "template_version": plan["template"]["template_version"],
        "template_digest": plan["template"]["template_digest"],
        "preflight_digest": plan["preflight_digest"],
        "dataset_digest": dataset["dataset_digest"],
        "settings": plan["settings"],
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
        "self_contained": True,
        "section_count": 7,
        **stats,
    }
    return {**base, "receipt_digest": _canonical_digest(base)}


def render_client_pdf(
    dataset: dict[str, Any],
    output_path: Path,
    *,
    settings: dict[str, Any] | None = None,
    project_root: Path,
    available_fonts: list[str] | tuple[str, ...] = (),
    node_executable: Path,
    node_modules_path: Path,
    browser_executable: Path,
    font_path: Path | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    output = _new_output(output_path, ".pdf")
    node = Path(node_executable)
    modules = Path(node_modules_path)
    browser = Path(browser_executable)
    if not node.is_file() or not os.access(node, os.X_OK) or not modules.is_dir() or not (modules / "playwright").is_dir() or not browser.is_file():
        raise PdfExportError("Playwright, Node.js, and an explicit existing Chromium browser are required; runtime download is disabled")
    try:
        from pypdf import PdfReader, PdfWriter
        from pypdf.generic import NameObject, TextStringObject
    except ImportError as exc:
        raise PdfExportError("PDF export extra is not installed; install video-analysis-mvp[pdf]") from exc
    driver = read_regular_bytes(DRIVER_PATH, root=DRIVER_PATH.parent, max_bytes=1024 * 1024)
    if hashlib.sha256(driver).hexdigest() != DRIVER_SHA256:
        raise PdfExportError("Playwright PDF driver does not match the fixed renderer version")
    dataset, plan = _prepare(dataset, settings, project_root, available_fonts, output_format="pdf")
    font_bytes = None
    font_digest = None
    font_family = None
    verified_cjk_glyphs = 0
    if plan["font_plan"]["cjk_required"]:
        if font_path is None:
            raise PdfExportError("an explicit local CJK font file is required for PDF rendering")
        font_candidate = Path(font_path).resolve()
        try:
            font_bytes = read_regular_bytes(font_candidate, root=font_candidate.parent, max_bytes=20 * 1024 * 1024)
        except (OSError, ValueError) as exc:
            raise PdfExportError("the configured CJK font file is unavailable or unsafe") from exc
        font_family, verified_cjk_glyphs = _validate_cjk_font(font_bytes, dataset)
        font_digest = hashlib.sha256(font_bytes).hexdigest()
    rendered, stats = _html_document(dataset, plan, Path(project_root).resolve(), embedded_font=font_bytes)
    generated = datetime.now(UTC).replace(microsecond=0)
    with tempfile.TemporaryDirectory(prefix=".vew-pdf-", dir=output.parent) as directory:
        staging = Path(directory)
        html_path = staging / "client.html"
        raw_pdf = staging / "browser.pdf"
        config_path = staging / "config.json"
        html_path.write_text(rendered, encoding="utf-8")
        config_path.write_text(
            json.dumps(
                {
                    "browserExecutable": str(browser),
                    "shortTitle": _text(dataset["project"]["title"])[:60],
                    "watchdogMs": 170_000,
                    "requireEmbeddedFont": font_bytes is not None,
                }
            ),
            encoding="utf-8",
        )
        env = {"PATH": str(node.parent), "NODE_PATH": str(modules), "HOME": str(staging), "TMPDIR": str(staging)}
        try:
            process = run_command(
                [str(node), str(DRIVER_PATH), str(html_path), str(raw_pdf), str(config_path)],
                timeout=180,
                environment=env,
                cancelled=cancelled,
                max_output_bytes=MAX_RENDERER_LOG_BYTES,
            )
        except ProcessCancelledError as exc:
            message = (
                "Playwright PDF rendering was cancelled and its process group was terminated"
                if exc.cleanup_verified is not False
                else "Playwright PDF rendering was cancelled but process-group cleanup could not be verified"
            )
            error = PdfExportError(message)
            error.process_group_cleanup_verified = exc.cleanup_verified
            raise error from exc
        except ToolError as exc:
            message = str(exc)
            if "timed out" in message:
                reason = "timed out"
            elif "output exceeded" in message:
                reason = "exceeded its renderer log limit"
            else:
                reason = "failed"
            raise PdfExportError(
                f"Playwright PDF rendering {reason}; no browser or dependency was downloaded"
            ) from None
        if not raw_pdf.is_file():
            raise PdfExportError("Playwright PDF rendering failed; no browser or dependency was downloaded")
        try:
            driver_result = json.loads(process.stdout)
        except ValueError as exc:
            raise PdfExportError("Playwright PDF renderer returned an invalid receipt") from exc
        font_loaded = driver_result.get("fontLoaded") is True
        if font_bytes is not None and not font_loaded:
            raise PdfExportError("Playwright did not load the configured embedded CJK font")
        reader = PdfReader(raw_pdf)
        writer = PdfWriter()
        writer.clone_document_from_reader(reader)
        writer.add_metadata({
            "/Title": _text(dataset["project"]["title"]),
            "/Author": "Video Evidence Workbench",
            "/Creator": "Video Evidence Workbench",
            "/Producer": "Video Evidence Workbench",
            "/Subject": "Evidence-bound storyboard, VO, music, SFX and rhythm breakdown",
            "/Keywords": "video evidence, storyboard, VO, music, SFX, rhythm",
            "/CreationDate": generated.strftime("D:%Y%m%d%H%M%S+00'00'"),
        })
        writer.root_object[NameObject("/Lang")] = TextStringObject(_document_language(plan["settings"]["language"]))
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                writer.write(stream)
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            output.unlink(missing_ok=True)
            raise
    try:
        validation = _validate_pdf(output, dataset)
        raw = read_regular_bytes(output, root=output.parent, max_bytes=MAX_OUTPUT_BYTES)
    except Exception:
        output.unlink(missing_ok=True)
        raise
    base = {
        "schema_id": PDF_RECEIPT_SCHEMA,
        "template_id": plan["template"]["template_id"],
        "template_version": plan["template"]["template_version"],
        "template_digest": plan["template"]["template_digest"],
        "preflight_digest": plan["preflight_digest"],
        "dataset_digest": dataset["dataset_digest"],
        "settings": plan["settings"],
        "font_plan": {
            **plan["font_plan"],
            "identity_verified": font_digest is not None and font_loaded,
            "embedding_mode": "explicit-font-data-uri" if font_digest is not None else "embedded-glyphs-verified",
            "font_sha256": font_digest,
            "resolved_font_family": font_family,
            "verified_cjk_glyphs": verified_cjk_glyphs,
        },
        "generated_at_utc": generated.isoformat(),
        "renderer": {
            "name": driver_result.get("renderer"),
            "browser_version": driver_result.get("browserVersion"),
            "driver_sha256": DRIVER_SHA256,
            "font_loaded": font_loaded,
        },
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
        **stats,
        **validation,
    }
    return {**base, "receipt_digest": _canonical_digest(base)}


def _prepare(
    dataset: dict[str, Any],
    settings: dict[str, Any] | None,
    project_root: Path,
    available_fonts: list[str] | tuple[str, ...],
    *,
    output_format: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        dataset = validate_client_export_dataset(dataset)
        options = dict(settings or {})
        options["formats"] = [output_format]
        plan = preflight_client_layout(dataset, options, project_root=Path(project_root), available_fonts=available_fonts)
    except (ClientExportDatasetError, ExportTemplateError) as exc:
        raise PdfExportError(str(exc)) from exc
    return dataset, plan


def _validate_cjk_font(font_bytes: bytes, dataset: dict[str, Any]) -> tuple[str, int]:
    try:
        font = ImageFont.truetype(io.BytesIO(font_bytes), size=12)
        family = str(font.getname()[0]).strip()
    except (OSError, ValueError) as exc:
        raise PdfExportError("the configured CJK font cannot be decoded as an OTF/TTF font") from exc
    required_text = json.dumps(dataset, ensure_ascii=False) + "项目概览叙事声音弧线逐镜分镜画面文字音乐音效节奏转场证据限制仅供草稿审核"
    required = sorted({character for character in required_text if _is_cjk_character(character)})
    missing_signature = _glyph_signature(font, "\U0010ffff")
    missing = [character for character in required if _glyph_signature(font, character) == missing_signature]
    if missing:
        preview = "".join(missing[:8])
        raise PdfExportError(f"the configured CJK font lacks required CJK glyphs: {preview}")
    return family, len(required)


def _glyph_signature(font: ImageFont.FreeTypeFont, character: str) -> tuple[tuple[int, int], bytes]:
    mask = font.getmask(character)
    return mask.size, bytes(mask)


def _is_cjk_character(character: str) -> bool:
    codepoint = ord(character)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x20000 <= codepoint <= 0x2FA1F
    )


def _html_document(
    dataset: dict[str, Any],
    plan: dict[str, Any],
    root: Path,
    *,
    embedded_font: bytes | None = None,
) -> tuple[str, dict[str, int]]:
    lang = plan["settings"]["language"]
    title = _text(dataset["project"]["title"])
    subtitle = _text(plan["settings"]["project_subtitle"])
    frame_images: dict[str, str] = {}
    for shot in dataset["shots"]:
        frame = shot["frame"]
        if not frame["present"]:
            continue
        candidate = (root / frame["path"]).resolve()
        try:
            candidate.relative_to(root)
            raw = read_regular_bytes(candidate, root=root, max_bytes=16 * 1024 * 1024)
            actual = inspect_image_bytes(raw, max_bytes=16 * 1024 * 1024).receipt_fields()
        except (OSError, ValueError) as exc:
            raise PdfExportError(f"frame evidence for {shot['shot_id']} is unavailable or unsafe") from exc
        expected = {key: frame[key] for key in ("sha256", "size_bytes", "media_type", "width", "height")}
        if actual != expected:
            raise PdfExportError(f"frame evidence for {shot['shot_id']} changed after dataset binding")
        frame_images[shot["shot_id"]] = f"data:{frame['media_type']};base64,{base64.b64encode(raw).decode('ascii')}"
    logo_uri = None
    logo = plan["settings"]["logo"]
    if logo is not None:
        candidate = (root / logo["path"]).resolve()
        try:
            candidate.relative_to(root)
            raw = read_regular_bytes(candidate, root=root, max_bytes=16 * 1024 * 1024)
            actual = inspect_image_bytes(raw, max_bytes=16 * 1024 * 1024).receipt_fields()
        except (OSError, ValueError) as exc:
            raise PdfExportError("client logo is unavailable or unsafe") from exc
        expected = {key: logo[key] for key in ("sha256", "size_bytes", "media_type", "width", "height")}
        if actual != expected:
            raise PdfExportError("client logo changed after template preflight")
        logo_uri = f"data:{logo['media_type']};base64,{base64.b64encode(raw).decode('ascii')}"
    density = 4 if plan["settings"]["density"] == "client" else 8
    shot_cards = [_shot_card(shot, frame_images.get(shot["shot_id"]), lang) for shot in dataset["shots"]]
    storyboard_pages = [shot_cards[index:index + density] for index in range(0, len(shot_cards), density)] or [[]]
    timed_voice_rows = []
    for event in dataset["audio"]["events"]:
        if event["kind"] != "voice":
            continue
        proposal = event["effective_proposal"] or event["original_proposal"]
        timed_voice_rows.append((event["start_seconds"], event["end_seconds"], event["event_id"], [event["event_id"], _range(event), _text(proposal["text"]), proposal["verification"], event["evidence_reference"]]))
    for shot in dataset["shots"]:
        onscreen = _text(shot["text"]["onscreen_text"])
        if onscreen:
            timed_voice_rows.append((shot["start_seconds"], shot["end_seconds"], shot["shot_id"], [shot["shot_id"], _text(shot["timecode"]), onscreen, shot["verification"]["annotation_verification"], shot["evidence_reference"]]))
    timed_voice_rows.sort(key=lambda item: (item[0], item[1], item[2]))
    voice_rows = [item[3] for item in timed_voice_rows]
    audio_rows = []
    for event in dataset["audio"]["events"]:
        proposal = event["effective_proposal"] or event["original_proposal"]
        audio_rows.append([event["event_id"], event["kind"], _range(event), _join([_text(proposal["label"]), _text(proposal["text"])]), _num(proposal["energy"]), _num(proposal["estimated_bpm"]), proposal["verification"]])
    evidence_rows = [["dataset", dataset["dataset_digest"]], ["template", plan["template"]["template_digest"]]]
    evidence_rows += [["limitation", _text(item)] for item in dataset["limitations"]]
    evidence_rows += [[f"unresolved:{item['scope']}", _text(item["reason"])] for item in dataset["unresolved_items"]]
    logo_html = f'<img class="logo" src="{logo_uri}" alt="client logo">' if logo_uri else ""
    sections = [
        f'<section class="page cover" id="cover">{logo_html}<p class="eyebrow">VIDEO EVIDENCE WORKBENCH</p><h1>{_e(title)}</h1><p>{_e(subtitle)}</p><div class="status">{_label(lang, "仅供草稿审核", "DRAFT ONLY") if dataset["delivery_status"]["state"] == "draft_only" else _e(dataset["delivery_status"]["state"])}</div></section>',
        f'<section class="page" id="overview"><h2>{_label(lang, "项目概览", "Executive overview")}</h2><div class="metrics"><b>{len(dataset["shots"])} shots</b><b>{dataset["project"]["duration_seconds"]:.2f} s</b><b>{_e(dataset["project"]["resolution"])}</b><b>{len(dataset["audio"]["events"])} audio events</b></div><p class="digest">{dataset["dataset_digest"]}</p></section>',
        f'<section class="page" id="narrative-audio"><h2>{_label(lang, "叙事与声音弧线", "Narrative and audio arc")}</h2>{_scene_html(dataset, lang)}<p>{_e(_text(dataset["limitations"][0]) if dataset["limitations"] else "")}</p></section>',
    ]
    for index, cards in enumerate(storyboard_pages):
        ident = "storyboard" if index == 0 else f"storyboard-{index + 1}"
        sections.append(f'<section class="page storyboard" id="{ident}"><h2>{_label(lang, "逐镜分镜", "Storyboard")} {index + 1}/{len(storyboard_pages)}</h2><div class="shot-grid">{"".join(cards)}</div></section>')
    sections.append(f'<section class="page" id="voice-text"><h2>{_label(lang, "VO 与画面文字", "VO and on-screen text")}</h2>{_table(["ID", "Range", "Text", "Verification", "Evidence"], voice_rows)}</section>')
    sections.append(f'<section class="page" id="audio-rhythm"><h2>{_label(lang, "音乐、音效与节奏", "Music, SFX and rhythm")}</h2>{_table(["ID", "Kind", "Range", "Label / text", "Energy", "BPM", "Verification"], audio_rows)}</section>')
    sections.append(f'<section class="page" id="evidence"><h2>{_label(lang, "证据与限制", "Evidence and limitations")}</h2>{_table(["Category", "Details"], evidence_rows)}</section>')
    declared_font = "VEW Embedded CJK" if embedded_font is not None else plan["font_plan"]["selected_cjk_font"] or "Noto Sans CJK"
    css = _css(plan["settings"]["accent_color"], declared_font, embedded_font)
    document_lang = _document_language(lang)
    rendered = f'<!doctype html><html lang="{document_lang}"><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src data:; font-src data:; style-src \'unsafe-inline\'"><title>{_e(title)}</title><style>{css}</style></head><body>{"".join(sections)}</body></html>'
    return rendered, {"shot_count": len(dataset["shots"]), "audio_event_count": len(dataset["audio"]["events"]), "embedded_image_count": len(frame_images) + int(logo_uri is not None), "storyboard_page_count": len(storyboard_pages)}


def _shot_card(shot: dict[str, Any], image: str | None, lang: str) -> str:
    summary = _join([_text(shot["text"]["content_summary_zh"]), _text(shot["text"]["content_summary"]), _text(shot["text"]["visual_description"])])
    media = f'<img src="{image}" alt="shot {shot["shot_no"]}">' if image else f'<div class="missing">{_e(_text(shot["frame"]["failure"]))}</div>'
    summary_preview, summary_rest = _preview(summary)
    camera = _join(_text(value) for value in shot["camera"].values())
    transition = " -> ".join(value for value in (_text(shot["text"]["transition_in"]), _text(shot["text"]["transition_out"])) if value)
    fields = [
        ("VO", _text(shot["text"]["dialogue"])),
        (_label(lang, "画面文字", "On-screen text"), _text(shot["text"]["onscreen_text"])),
        (_label(lang, "机位", "Camera"), camera),
        (_label(lang, "声音", "Sound"), _join([_text(shot["text"]["music_state"]), _text(shot["text"]["sound_design"])])),
        (_label(lang, "节奏 / 转场", "Rhythm / transition"), _join([_text(shot["text"]["rhythm_notes"]), transition])),
    ]
    previews, remainder = [], [summary_rest] if summary_rest else []
    for label, value in fields:
        preview, rest = _preview(value)
        previews.append(f"<dt>{_e(label)}</dt><dd>{_e(preview)}</dd>")
        if rest:
            remainder.append(f"{label}: {rest}")
    details = "<dl>" + "".join(previews) + "</dl>"
    main = f'<article class="shot-card" data-shot-id="{_e(shot["shot_id"])}"><header><b>#{shot["shot_no"]:04d}</b><span>{_e(_text(shot["timecode"]))}</span></header>{media}<p>{_e(summary_preview)}</p>{details}<footer>{_e(shot["verification"]["annotation_verification"])} · {_e(shot["evidence_reference"])}</footer></article>'
    continuation_chunks = [chunk for value in remainder for chunk in (value[index:index + 1200] for index in range(0, len(value), 1200))]
    continuations = "".join(f'<article class="shot-continuation"><header><b>#{shot["shot_no"]:04d} · {_label(lang, "续", "continued")}</b><span>{_e(_text(shot["timecode"]))}</span></header><p>{_e(chunk)}</p></article>' for chunk in continuation_chunks)
    return main + continuations


def _scene_html(dataset: dict[str, Any], lang: str) -> str:
    rows = [[scene["scene_id"], f"{scene['start_seconds']:.2f}-{scene['end_seconds']:.2f}", _text(scene["function"]), _text(scene["pace"])] for scene in dataset["scenes"]]
    return _table(["Section", "Range", _label(lang, "功能", "Function"), _label(lang, "节奏", "Pace")], rows)


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    body = rows or [["—"]]
    return "<table><thead><tr>" + "".join(f"<th>{_e(item)}</th>" for item in headers) + "</tr></thead><tbody>" + "".join("<tr>" + "".join(f"<td>{_e(item)}</td>" for item in row) + "</tr>" for row in body) + "</tbody></table>"


def _css(accent: str, declared_font: str, embedded_font: bytes | None) -> str:
    font_face = ""
    if embedded_font is not None:
        encoded = base64.b64encode(embedded_font).decode("ascii")
        font_face = f"@font-face{{font-family:'VEW Embedded CJK';src:url(data:font/otf;base64,{encoded}) format('opentype');font-weight:100 900;}}"
    return f"""{font_face}@page {{ size: A4 landscape; margin: 12mm 12mm 16mm; }}
*{{box-sizing:border-box}}html{{font-size: 8.5pt}}body{{margin:0;color:#111827;font-family:'{declared_font}','Noto Sans CJK','Microsoft YaHei',Arial,sans-serif;font-size: 8.5pt;line-height:1.2;-webkit-print-color-adjust:exact;print-color-adjust:exact}}.page{{break-before:page;min-height:174mm;padding:6mm 10mm}}.page:first-child{{break-before:auto}}h1{{font-size:28pt;margin:16mm 0 4mm}}h2{{font-size:17pt;border-bottom:2px solid {accent};padding-bottom:3mm;margin:0 0 4mm}}.cover{{background:#0B1020;color:white}}.logo{{max-width:40mm;max-height:18mm;object-fit:contain;float:right}}.eyebrow{{letter-spacing:.18em}}.status{{display:inline-block;background:{accent};padding:2mm 4mm}}.metrics{{display:grid;grid-template-columns:repeat(4,1fr);gap:4mm}}.metrics b{{background:#F7F9FC;padding:5mm}}.digest{{font-family:monospace;word-break:break-all}}.shot-grid{{display:grid;grid-template-columns:1fr 1fr;gap:3mm 5mm}}.shot-card,.shot-continuation{{border:1px solid #D6DDEA;padding:1.5mm;break-inside:avoid;min-height:0}}.shot-continuation{{grid-column:1/-1}}.shot-card header,.shot-continuation header{{display:flex;justify-content:space-between;border-bottom:1px solid #D6DDEA;padding-bottom:1mm}}.shot-card img,.missing{{width:100%;height:16mm;object-fit:contain;background:#F7F9FC;margin:1mm 0}}.missing{{display:flex;align-items:center;justify-content:center;color:#667085}}dl{{display:grid;grid-template-columns:20mm 1fr;margin:0}}dt{{font-weight:bold}}dd{{margin:0 0 .5mm}}.shot-card footer{{font-size: 8.5pt;color:#667085;word-break:break-all}}table{{width:100%;border-collapse:collapse;table-layout:fixed}}th{{background:#237A70;color:white;text-align:left}}th,td{{padding:2mm;border-bottom:1px solid #D6DDEA;vertical-align:top;overflow-wrap:anywhere}}tr{{break-inside:avoid}}"""


def _validate_pdf(path: Path, dataset: dict[str, Any]) -> dict[str, Any]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise PdfExportError("PDF validation dependency is unavailable") from exc
    reader = PdfReader(path)
    if not reader.pages:
        raise PdfExportError("PDF contains no pages")
    first = reader.pages[0]
    width, height = float(first.mediabox.width), float(first.mediabox.height)
    if width <= height or abs(width - 841.9) > 3 or abs(height - 595.3) > 3:
        raise PdfExportError("PDF page size is not A4 landscape")
    text = _normalized_pdf_text("\n".join(page.extract_text() or "" for page in reader.pages))
    required = [_text(dataset["project"]["title"]), dataset["dataset_digest"]]
    required.extend(shot["shot_id"] for shot in dataset["shots"])
    required.extend(_text(shot["text"]["onscreen_text"]) for shot in dataset["shots"] if _text(shot["text"]["onscreen_text"]))
    for event in dataset["audio"]["events"]:
        if event["kind"] == "voice":
            required.append(_text((event["effective_proposal"] or event["original_proposal"])["text"]))
            break
    if any(value and _normalized_pdf_text(value) not in text for value in required):
        raise PdfExportError("PDF searchable text is incomplete")
    embedded = _fonts_embedded(reader)
    if not embedded:
        raise PdfExportError("PDF fonts are not verifiably embedded")
    return {"page_count": len(reader.pages), "page_size": "A4-landscape", "searchable_text": True, "embedded_fonts": True, "tagged": bool(reader.trailer["/Root"].get("/MarkInfo"))}


def _fonts_embedded(reader: Any) -> bool:
    seen = []
    for page in reader.pages:
        fonts = (page.get("/Resources") or {}).get("/Font") or {}
        for reference in fonts.values():
            font = reference.get_object()
            if font.get("/Subtype") == "/Type3":
                seen.append(bool(font.get("/CharProcs")))
                continue
            descendants = font.get("/DescendantFonts") or [font]
            for descendant_ref in descendants:
                descendant = descendant_ref.get_object() if hasattr(descendant_ref, "get_object") else descendant_ref
                descriptor_ref = descendant.get("/FontDescriptor")
                if descriptor_ref is None:
                    seen.append(False)
                    continue
                descriptor = descriptor_ref.get_object() if hasattr(descriptor_ref, "get_object") else descriptor_ref
                seen.append(any(key in descriptor for key in ("/FontFile", "/FontFile2", "/FontFile3")))
    return bool(seen) and all(seen)


def _new_output(value: Path, suffix: str) -> Path:
    path = Path(value)
    if path.suffix.lower() != suffix or path.exists() or path.is_symlink() or not path.parent.is_dir():
        raise PdfExportError(f"new {suffix} output path in an existing directory is required")
    return path


def _write_exclusive(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _text(value: dict[str, Any]) -> str:
    result = value.get("text")
    if type(result) is not str:
        raise PdfExportError("renderer received an invalid text cell")
    return result


def _e(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _join(values: Any) -> str:
    return "\n".join(dict.fromkeys(value for value in values if value))


def _preview(value: str, limit: int = 180) -> tuple[str, str]:
    if len(value) <= limit:
        return value, ""
    return value[:limit].rstrip() + "…", value[limit:]


def _range(event: dict[str, Any]) -> str:
    return f"{event['start_seconds']:.3f}-{event['end_seconds']:.3f}"


def _num(value: Any) -> str:
    return "—" if value is None else f"{float(value):.2f}"


def _label(language: str, zh: str, en: str) -> str:
    return zh if language == "zh" else en if language == "en" else f"{zh} / {en}"


def _document_language(language: str) -> str:
    return "zh-Hans" if language in {"zh", "bilingual"} else "en"


def _normalized_pdf_text(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value).split())


__all__ = ["PdfExportError", "render_client_html", "render_client_pdf"]
