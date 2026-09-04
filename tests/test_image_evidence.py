from __future__ import annotations

import io
import struct
import unittest
import zlib

from PIL import Image

from video_analysis_mvp.image_evidence import inspect_image_bytes


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    checksum = zlib.crc32(kind)
    checksum = zlib.crc32(payload, checksum) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", checksum)


def _header_only_png() -> bytes:
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr) + _png_chunk(b"IDAT", b"") + _png_chunk(b"IEND", b"")


class ImageEvidenceTest(unittest.TestCase):
    def test_complete_png_and_jpeg_rasters_are_accepted(self) -> None:
        for image_format, expected_media_type in (("PNG", "image/png"), ("JPEG", "image/jpeg")):
            with self.subTest(image_format=image_format):
                output = io.BytesIO()
                Image.new("RGB", (2, 3), (24, 48, 72)).save(output, format=image_format)
                evidence = inspect_image_bytes(output.getvalue())
                self.assertEqual(expected_media_type, evidence.media_type)
                self.assertEqual((2, 3), (evidence.width, evidence.height))

    def test_png_with_empty_idat_is_rejected_even_when_container_is_valid(self) -> None:
        with self.assertRaisesRegex(ValueError, "raster"):
            inspect_image_bytes(_header_only_png())

    def test_jpeg_with_sof_but_no_scan_data_is_rejected(self) -> None:
        header_only_jpeg = bytes.fromhex("ffd8ffc00008080001000100ffd9")
        with self.assertRaisesRegex(ValueError, "raster"):
            inspect_image_bytes(header_only_jpeg)


if __name__ == "__main__":
    unittest.main()
