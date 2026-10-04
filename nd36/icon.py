"""
The app icon, drawn in pure Python.

No Pillow, no iconutil, no checked-in binary. A PNG is a zlib-compressed
run of scanlines with a filter byte in front of each, and an .icns is just a
magic word followed by typed chunks that may themselves be PNGs -- so both
are a few dozen lines, and the project keeps its promise of having no
dependencies and nothing opaque in the repo.

The picture is the step explorer in miniature: a few dim code lines with one
highlighted, which is what the app is for.
"""

import struct
import zlib

BG = (16, 17, 20, 255)          # near-black, like the dark theme surface
DIM = (62, 66, 76, 255)         # the lines that are not running
HOT = (58, 135, 229, 255)       # the line that is -- series slot 1
MARK = (235, 104, 52, 255)      # the cursor -- series slot 2


def _rounded(width, height):
    """A rounded-square mask, antialiased by 3x3 supersampling.

    macOS draws app icons on their own, so a hard-edged square looks broken
    next to everything else in the Dock.
    """
    radius = width * 0.225
    inset = width * 0.055
    mask = [[0.0] * width for _ in range(height)]
    for y in range(height):
        for x in range(width):
            hits = 0
            for sy in range(3):
                for sx in range(3):
                    px = x + (sx + 0.5) / 3.0
                    py = y + (sy + 0.5) / 3.0
                    lo, hi = inset, width - inset
                    cx = min(max(px, lo + radius), hi - radius)
                    cy = min(max(py, lo + radius), hi - radius)
                    if (px - cx) ** 2 + (py - cy) ** 2 <= radius ** 2:
                        hits += 1
            mask[y][x] = hits / 9.0
    return mask


def _blend(dst, src, alpha):
    return tuple(int(round(d + (s - d) * alpha))
                 for d, s in zip(dst, src))


def draw(size):
    """-> rows of (r, g, b, a) tuples."""
    mask = _rounded(size, size)
    rows = [[(0, 0, 0, 0)] * size for _ in range(size)]

    # The code lines: six bars, the fourth one lit.
    unit = size / 32.0
    bars = []
    for i in range(6):
        top = unit * (7 + i * 3.1)
        left = unit * (7 + (2.6 if i in (2, 3) else 0))
        width = unit * (14 - (3.5 if i in (1, 4) else 0)
                        - (2.0 if i in (2, 3) else 0))
        bars.append((top, top + unit * 1.7, left, left + width, i == 3))

    for y in range(size):
        for x in range(size):
            cover = mask[y][x]
            if cover <= 0:
                continue
            colour = BG
            for top, bottom, left, right, lit in bars:
                if top <= y < bottom and left <= x < right:
                    colour = HOT if lit else DIM
                    break
            # The cursor: a thin bar down the left of the lit line.
            lit_bar = bars[3]
            if (lit_bar[0] - unit * 0.5 <= y < lit_bar[1] + unit * 0.5
                    and unit * 4.2 <= x < unit * 5.4):
                colour = MARK
            rows[y][x] = _blend((0, 0, 0, 0), colour, cover) \
                if cover < 1 else colour
            if cover < 1:
                r, g, b, _a = colour
                rows[y][x] = (r, g, b, int(round(255 * cover)))
    return rows


def png_bytes(size):
    rows = draw(size)
    raw = bytearray()
    for row in rows:
        raw.append(0)                       # filter type 0: none
        for r, g, b, a in row:
            raw += bytes((r, g, b, a))

    def chunk(tag, data):
        body = tag + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


#: icns chunk type -> pixel size. These are the PNG-bearing types.
ICNS_TYPES = [(b"icp5", 32), (b"icp6", 64), (b"ic07", 128),
              (b"ic08", 256), (b"ic09", 512)]


def icns_bytes():
    """An .icns file: b'icns', total length, then typed chunks."""
    chunks = b""
    for tag, size in ICNS_TYPES:
        data = png_bytes(size)
        chunks += tag + struct.pack(">I", len(data) + 8) + data
    return b"icns" + struct.pack(">I", len(chunks) + 8) + chunks


def write_icns(path):
    with open(path, "wb") as fh:
        fh.write(icns_bytes())
    return path


def write_png(path, size=256):
    with open(path, "wb") as fh:
        fh.write(png_bytes(size))
    return path
