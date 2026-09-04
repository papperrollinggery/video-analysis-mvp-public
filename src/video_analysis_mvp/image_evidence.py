from __future__ import annotations

import hashlib
import io
import zlib
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError


MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_IMAGE_DIMENSION = 32_768


@dataclass(frozen=True)
class ImageEvidence:
    """Validated bytes and a receipt for one provider-eligible image."""

    data: bytes
    sha256: str
    size_bytes: int
    media_type: str
    width: int
    height: int

    def receipt_fields(self) -> dict[str, str | int]:
        return {
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "media_type": self.media_type,
            "width": self.width,
            "height": self.height,
        }


def inspect_image_bytes(data: bytes, *, max_bytes: int = MAX_IMAGE_BYTES) -> ImageEvidence:
    """Validate an exact bounded byte snapshot as a complete PNG or JPEG.

    Callers are expected to obtain ``data`` from one already-confined file
    descriptor.  The returned digest, dimensions, and provider payload all bind
    to that same immutable byte snapshot.
    """
    if not data:
        raise ValueError("frame file is empty")
    if len(data) > max_bytes:
        raise ValueError(f"frame file exceeds the {max_bytes}-byte limit")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        media_type, width, height = _png_dimensions(data)
    elif data.startswith(b"\xff\xd8"):
        media_type, width, height = _jpeg_dimensions(data)
    else:
        raise ValueError("frame is not a supported PNG or JPEG image")
    if width <= 0 or height <= 0 or width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION:
        raise ValueError("frame dimensions are invalid or exceed the per-axis limit")
    if width * height > MAX_IMAGE_PIXELS:
        raise ValueError(f"frame exceeds the {MAX_IMAGE_PIXELS}-pixel limit")
    _decode_raster(data, media_type=media_type, width=width, height=height)
    return ImageEvidence(
        data=data,
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        media_type=media_type,
        width=width,
        height=height,
    )


def _decode_raster(data: bytes, *, media_type: str, width: int, height: int) -> None:
    """Require a complete, decodable raster in addition to a valid container.

    The structural parsers above reject polyglots and bind the declared
    dimensions before Pillow allocates image memory.  Pillow is then used to
    verify the codec stream and fully decode every pixel; header-only PNG/JPEG
    payloads must never become provider or readiness evidence.
    """
    expected_format = "PNG" if media_type == "image/png" else "JPEG"
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format != expected_format or image.size != (width, height):
                raise ValueError("frame decoder metadata does not match its container")
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            if image.format != expected_format or image.size != (width, height):
                raise ValueError("frame decoder metadata does not match its container")
            image.load()
            image.getpixel((0, 0))
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError, SyntaxError) as exc:
        raise ValueError("frame raster is incomplete or cannot be decoded") from exc


def _png_dimensions(data: bytes) -> tuple[str, int, int]:
    if len(data) < 33:
        raise ValueError("PNG frame header is invalid")
    offset = 8
    width = 0
    height = 0
    saw_ihdr = False
    saw_idat = False
    saw_iend = False
    while offset + 12 <= len(data):
        length = int.from_bytes(data[offset : offset + 4], "big")
        chunk_type = data[offset + 4 : offset + 8]
        payload_start = offset + 8
        payload_end = payload_start + length
        chunk_end = payload_end + 4
        if chunk_end > len(data):
            raise ValueError("PNG frame is truncated")
        expected_crc = int.from_bytes(data[payload_end:chunk_end], "big")
        actual_crc = zlib.crc32(chunk_type)
        actual_crc = zlib.crc32(data[payload_start:payload_end], actual_crc) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            raise ValueError("PNG chunk checksum is invalid")
        if not saw_ihdr:
            if chunk_type != b"IHDR" or length != 13:
                raise ValueError("PNG IHDR must be the first chunk")
            width = int.from_bytes(data[payload_start : payload_start + 4], "big")
            height = int.from_bytes(data[payload_start + 4 : payload_start + 8], "big")
            bit_depth = data[payload_start + 8]
            color_type = data[payload_start + 9]
            compression = data[payload_start + 10]
            filter_method = data[payload_start + 11]
            interlace = data[payload_start + 12]
            legal_depths = {
                0: {1, 2, 4, 8, 16},
                2: {8, 16},
                3: {1, 2, 4, 8},
                4: {8, 16},
                6: {8, 16},
            }
            if (
                bit_depth not in legal_depths.get(color_type, set())
                or compression != 0
                or filter_method != 0
                or interlace not in {0, 1}
            ):
                raise ValueError("PNG IHDR fields are invalid")
            saw_ihdr = True
        elif chunk_type == b"IHDR":
            raise ValueError("PNG contains multiple IHDR chunks")
        if chunk_type == b"IDAT":
            saw_idat = True
        if chunk_type == b"IEND":
            if length != 0 or saw_iend:
                raise ValueError("PNG IEND chunk is invalid")
            saw_iend = True
            if chunk_end != len(data):
                raise ValueError("PNG frame has trailing polyglot data")
            break
        offset = chunk_end
    if not saw_ihdr or not saw_idat or not saw_iend:
        raise ValueError("PNG frame is incomplete")
    return "image/png", width, height


def _jpeg_dimensions(data: bytes) -> tuple[str, int, int]:
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        raise ValueError("JPEG frame header is invalid")
    offset = 2
    width = 0
    height = 0
    in_scan = False
    sof_markers = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
    while offset < len(data):
        if not in_scan:
            if data[offset] != 0xFF:
                raise ValueError("JPEG marker stream is invalid")
            while offset < len(data) and data[offset] == 0xFF:
                offset += 1
            if offset >= len(data):
                raise ValueError("JPEG frame is truncated")
            marker = data[offset]
            offset += 1
        else:
            marker_start = data.find(b"\xff", offset)
            if marker_start < 0:
                raise ValueError("JPEG entropy stream is incomplete")
            offset = marker_start
            while offset < len(data) and data[offset] == 0xFF:
                offset += 1
            if offset >= len(data):
                raise ValueError("JPEG frame is truncated")
            marker = data[offset]
            offset += 1
            if marker == 0x00 or 0xD0 <= marker <= 0xD7:
                continue
            in_scan = False

        if marker == 0xD9:
            if offset != len(data):
                raise ValueError("JPEG frame has trailing polyglot data")
            if width <= 0 or height <= 0:
                raise ValueError("JPEG frame dimensions are missing")
            return "image/jpeg", width, height
        if marker == 0xD8:
            raise ValueError("JPEG contains a nested SOI marker")
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > len(data):
            raise ValueError("JPEG frame is truncated")
        length = int.from_bytes(data[offset : offset + 2], "big")
        if length < 2 or offset + length > len(data):
            raise ValueError("JPEG segment is truncated")
        if marker in sof_markers:
            if length < 8:
                raise ValueError("JPEG SOF segment is invalid")
            height = int.from_bytes(data[offset + 3 : offset + 5], "big")
            width = int.from_bytes(data[offset + 5 : offset + 7], "big")
        if marker == 0xDA:
            in_scan = True
        offset += length
    raise ValueError("JPEG frame is incomplete")
