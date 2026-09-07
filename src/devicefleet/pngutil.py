"""Minimal PNG encoder used by the stub provider (no extra image deps)."""

from __future__ import annotations

import struct
import zlib


def rgb_png(width: int, height: int, pixels: bytes) -> bytes:
    """Encode a raw RGB buffer (width*height*3 bytes) as a PNG."""
    if width < 1 or height < 1:
        raise ValueError("png dimensions must be positive")
    expected = width * height * 3
    if len(pixels) != expected:
        raise ValueError(f"expected {expected} RGB bytes, got {len(pixels)}")

    raw = bytearray()
    row_size = width * 3
    for y in range(height):
        raw.append(0)
        start = y * row_size
        raw.extend(pixels[start : start + row_size])

    def chunk(tag: bytes, data: bytes) -> bytes:
        header = tag + data
        return (
            struct.pack(">I", len(data))
            + header
            + struct.pack(">I", zlib.crc32(header) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"".join(
        [
            b"\x89PNG\r\n\x1a\n",
            chunk(b"IHDR", ihdr),
            chunk(b"IDAT", zlib.compress(bytes(raw), 9)),
            chunk(b"IEND", b""),
        ]
    )


def solid_png(
    width: int,
    height: int,
    color: tuple[int, int, int],
    mark: tuple[int, int] | None = None,
    mark_color: tuple[int, int, int] = (255, 255, 255),
    mark_radius: int = 18,
) -> bytes:
    """Build a solid-color PNG, optionally marking a tap/swipe point."""
    r, g, b = color
    buf = bytearray([r, g, b] * (width * height))
    if mark is not None:
        mx, my = mark
        mr, mg, mb = mark_color
        for y in range(max(0, my - mark_radius), min(height, my + mark_radius + 1)):
            for x in range(max(0, mx - mark_radius), min(width, mx + mark_radius + 1)):
                if (x - mx) ** 2 + (y - my) ** 2 <= mark_radius**2:
                    idx = (y * width + x) * 3
                    buf[idx : idx + 3] = bytes((mr, mg, mb))
    return rgb_png(width, height, bytes(buf))
