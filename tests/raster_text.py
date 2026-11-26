"""Paint a short Latin line into a PNG, with no image library.

The neutrality scan's OCR test uses this so a deny-list token can be put in
a raster without committing that token into the repository.
"""

from __future__ import annotations

import struct
import zlib

# 5x7 glyphs. One character per pixel, rows top to bottom.
_GLYPHS = {
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "I": ("11111", "00100", "00100", "00100", "00100", "00100", "11111"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    " ": ("00000", "00000", "00000", "00000", "00000", "00000", "00000"),
}


def _png(width: int, height: int, rows: list[bytes]) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" + row for row in rows)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def paint(text: str, scale: int = 10, margin: int = 28) -> bytes:
    """Black text on white. ``scale`` is pixels per glyph pixel."""
    glyphs = []
    for char in text.upper():
        rows = _GLYPHS.get(char)
        if rows is None:
            raise ValueError(f"no glyph for {char!r}")
        glyphs.append(rows)
    gap = 2
    glyph_w = 5 * scale
    width = margin * 2 + len(glyphs) * glyph_w + max(0, len(glyphs) - 1) * gap * scale
    height = margin * 2 + 7 * scale
    white = b"\xff\xff\xff"
    black = b"\x14\x22\x26"
    canvas = [bytearray(white * width) for _ in range(height)]
    x0 = margin
    for index, rows in enumerate(glyphs):
        for row_index, bits in enumerate(rows):
            for col_index, bit in enumerate(bits):
                if bit != "1":
                    continue
                for dy in range(scale):
                    y = margin + row_index * scale + dy
                    start = (x0 + col_index * scale) * 3
                    canvas[y][start : start + scale * 3] = black * scale
        x0 += glyph_w + gap * scale
    return _png(width, height, [bytes(row) for row in canvas])


def blank(width: int = 64, height: int = 32) -> bytes:
    white = b"\xff\xff\xff" * width
    return _png(width, height, [white for _ in range(height)])
