#!/usr/bin/env python3
"""Generate the brand assets (design-handoff §0 "Mark (15g)") from one pixel grid.

The mark is a rounded tile with a 1px ink border holding a four-band ordered-dither ramp in royal on white:
solid, 50% checker, 25% (one pixel in four), 6.25% (one pixel in sixteen). Everything is authored on a 16x16
integer grid so the SVG stays crisp at any size and the PNGs are plain nearest-neighbour rasters of the same
grid. Pure stdlib (zlib + struct) — no Pillow.

Usage: uv run python scripts/gen_brand.py [--out DIR]
"""
from __future__ import annotations

import argparse
import struct
import zlib
from pathlib import Path

INK = (0x1E, 0x1D, 0x1A, 255)  # --ink
ACCENT = (0x2F, 0x4B, 0xD6, 255)  # --accent (royal)
SURFACE = (0xFF, 0xFF, 0xFF, 255)  # --surface
TRANSPARENT = (0, 0, 0, 0)

INK_HEX = "#1e1d1a"
ACCENT_HEX = "#2f4bd6"
SURFACE_HEX = "#ffffff"

GRID = 16  # logical grid: 16 cells wide/tall
BAND = 4  # each dither band is 4 cells tall
RADIUS = 3  # tile corner radius in cells (prototype renders 3px on a 16px box)


def ink_cell(x: int, y: int) -> bool:
    """True when the 1x1 cell at grid (x, y) is painted royal. Bands top to bottom (design-handoff §0)."""
    band, ly = divmod(y, BAND)
    lx = x
    if band == 0:
        return True  # solid
    if band == 1:
        return (lx + ly) % 2 == 0  # 50% checkerboard
    if band == 2:
        return lx % 2 == 0 and ly % 2 == 0  # 25%: one pixel in four
    return lx % 4 == 0 and ly % 4 == 0  # 6.25%: one pixel in sixteen


def _svg_recs(cells: list[tuple[int, int]]) -> str:
    return "".join(f'<rect x="{x}" y="{y}" width="1" height="1"/>' for x, y in sorted(cells))


def svg(size: int = GRID, radius: int = RADIUS, tile_inset: float = 0.5) -> str:
    """The mark as SVG `<rect>` pixels on an integer grid; `radius` is tuned per asset (§0)."""
    cells = [(x, y) for y in range(GRID) for x in range(GRID) if ink_cell(x, y)]
    # The dither lives inside the 1px border: clip to the inner edge of the stroke (x/y = 1, side = 14).
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {GRID} {GRID}"'
        f' width="{size}" height="{size}"\n'
        f'     role="img" aria-label="Inbox Sift">\n'
        f'  <defs>\n'
        f'    <clipPath id="in"><rect x="1" y="1" width="{GRID - 2}" height="{GRID - 2}"'
        f' rx="{max(0, radius - 1)}"/></clipPath>\n'
        f'  </defs>\n'
        f'  <rect x="{tile_inset}" y="{tile_inset}" width="{GRID - 2 * tile_inset}"'
        f' height="{GRID - 2 * tile_inset}" rx="{radius}" fill="{SURFACE_HEX}" stroke="{INK_HEX}"'
        f' stroke-width="1"/>\n'
        f'  <g clip-path="url(#in)" fill="{ACCENT_HEX}" shape-rendering="crispEdges">'
        f'{_svg_recs(cells)}</g>\n'
        f'</svg>\n'
    )


def _rounded(x: float, y: float, size: int, radius: float, inset: float) -> bool:
    """Inside a rounded square of side `size` inset by `inset` with corner `radius` (device pixels)."""
    lo, hi, r = inset, size - inset, radius
    if not (lo <= x <= hi and lo <= y <= hi):
        return False
    cx = min(max(x, lo + r), hi - r)
    cy = min(max(y, lo + r), hi - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def raster(px: int) -> list[bytes]:
    """Nearest-neighbour raster of the grid at `px`x`px`, including the 1px-scaled ink border."""
    scale = px / GRID
    border = scale
    rows: list[bytes] = []
    for dy in range(px):
        row = bytearray()
        for dx in range(px):
            cx, cy = dx + 0.5, dy + 0.5
            if not _rounded(cx, cy, px, RADIUS * scale, 0.0):
                row += bytes(TRANSPARENT)
                continue
            outer_band = _rounded(cx, cy, px, RADIUS * scale, border)
            if not outer_band:
                row += bytes(INK)  # the 1px (scaled) ink border
                continue
            gx, gy = int(dx / scale), int(dy / scale)
            row += bytes(ACCENT if ink_cell(gx, gy) else SURFACE)
        rows.append(bytes(row))
    return rows


def png(px: int) -> bytes:
    """Encode an RGBA PNG with filter 0 (None) per scanline. No third-party deps."""
    rows = raster(px)
    raw = b"".join(bytes(1) + r for r in rows)  # one filter-type byte (0 = None) per scanline

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", px, px, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def main() -> None:
    here = Path(__file__).resolve().parent
    out = here.parent / "src" / "inbox_sift" / "static" / "brand"
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=out, help="output directory (default: static/brand)")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    (args.out / "mark.svg").write_text(svg(size=16, radius=3), encoding="utf-8")
    # Favicon: same grid, tighter corner radius so the tile reads as a square at 16px.
    (args.out / "favicon.svg").write_text(svg(size=16, radius=2), encoding="utf-8")
    for px in (16, 32):
        (args.out / f"favicon-{px}.png").write_bytes(png(px))
    for name in ("mark.svg", "favicon.svg", "favicon-16.png", "favicon-32.png"):
        print(f"wrote {args.out / name} ({(args.out / name).stat().st_size} bytes)")


if __name__ == "__main__":
    main()
