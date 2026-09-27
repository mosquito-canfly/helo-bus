"""Generate the favicon assets from one shared bus-silhouette drawing.

No image library in this project (Pillow would be a new dependency for a
one-time asset) — a solid black-on-white silhouette only needs rect/circle
membership tests, so this hand-rolls a minimal PNG encoder with stdlib zlib
instead. A stroke-outline icon (like the header's Lucide glyph) thins out to
nothing at 16x16; a solid silhouette stays a recognisable blob, which is the
actual goal here.

Run: python scripts/make_favicon.py
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"


def _rounded_rect_mask(size: int, x0: float, y0: float, x1: float, y1: float, r: float):
    def inside(px: float, py: float) -> bool:
        if not (x0 - r <= px <= x1 + r and y0 - r <= py <= y1 + r):
            return False
        cx = min(max(px, x0 + r), x1 - r)
        cy = min(max(py, y0 + r), y1 - r)
        return (px - cx) ** 2 + (py - cy) ** 2 <= r * r if (px < x0 + r or px > x1 - r) and (py < y0 + r or py > y1 - r) else (x0 <= px <= x1 and y0 <= py <= y1)

    return inside


def draw_bus(size: int) -> list[list[bool]]:
    """True = black. A bold, simple bus silhouette: rounded body, three
    window notches, two wheels — legible down to 16x16."""
    grid = [[False] * size for _ in range(size)]

    body = _rounded_rect_mask(size, 0.12 * size, 0.26 * size, 0.88 * size, 0.72 * size, 0.07 * size)
    window_y0, window_y1 = 0.34 * size, 0.50 * size
    window_xs = [(0.20, 0.38), (0.44, 0.62), (0.68, 0.80)]
    wheel_r = 0.11 * size
    wheels = [(0.30 * size, 0.72 * size), (0.70 * size, 0.72 * size)]

    for py in range(size):
        for px in range(size):
            x, y = px + 0.5, py + 0.5
            black = body(x, y)
            if black:
                for wx0, wx1 in window_xs:
                    if wx0 * size <= x <= wx1 * size and window_y0 <= y <= window_y1:
                        black = False
                        break
            for wx, wy in wheels:
                if (x - wx) ** 2 + (y - wy) ** 2 <= wheel_r * wheel_r:
                    black = True
            grid[py][px] = black

    return grid


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))


def encode_png(grid: list[list[bool]]) -> bytes:
    size = len(grid)
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)  # 8-bit RGB
    raw = bytearray()
    for row in grid:
        raw.append(0)  # no filter
        for black in row:
            raw.extend((0, 0, 0) if black else (255, 255, 255))
    idat = zlib.compress(bytes(raw), 9)
    return b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr) + _png_chunk(b"IDAT", idat) + _png_chunk(b"IEND", b"")


def encode_ico(png_bytes: bytes, size: int) -> bytes:
    header = struct.pack("<HHH", 0, 1, 1)
    dim = size if size < 256 else 0
    entry = struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png_bytes), 22)
    return header + entry + png_bytes


def main() -> None:
    grid32 = draw_bus(32)
    # Trace each row's black runs as one rectangle subpath in a SINGLE
    # <path>, not one <rect> per run — separate abutting <rect> elements
    # each get their own edge antialiasing, which shows up as faint seams
    # between rows once the browser scales a 32x32 viewBox up to fill a
    # tab-icon-sized box. One path, one fill pass, no seams.
    path_parts = []
    for y, row in enumerate(grid32):
        run_start = None
        for x in range(32 + 1):
            black = x < 32 and row[x]
            if black and run_start is None:
                run_start = x
            elif not black and run_start is not None:
                path_parts.append(f"M{run_start},{y}H{x}V{y + 1}H{run_start}Z")
                run_start = None
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
        '<rect width="32" height="32" rx="7" fill="#ffffff"/>'
        f'<path fill="#000000" d="{"".join(path_parts)}"/>'
        "</svg>\n"
    )
    (WEB_DIR / "favicon.svg").write_text(svg, encoding="utf-8")

    ico_png = encode_png(draw_bus(32))
    (WEB_DIR / "favicon.ico").write_bytes(encode_ico(ico_png, 32))

    touch_png = encode_png(draw_bus(180))
    (WEB_DIR / "apple-touch-icon.png").write_bytes(touch_png)

    print(f"wrote {WEB_DIR / 'favicon.svg'} ({len(svg)} bytes)")
    print(f"wrote {WEB_DIR / 'favicon.ico'} ({(WEB_DIR / 'favicon.ico').stat().st_size} bytes)")
    print(f"wrote {WEB_DIR / 'apple-touch-icon.png'} ({len(touch_png)} bytes)")


if __name__ == "__main__":
    main()
