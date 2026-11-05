"""Tiny PNG codec (8-bit RGB/RGBA, non-interlaced) so the plugin can bake an alpha
channel into the engine's heat maps without depending on Pillow inside KiCad's
Python. Reference images in the IPC API have no opacity property (Spike 0 #4)."""

from __future__ import annotations

import struct
import zlib

_SIG = b"\x89PNG\r\n\x1a\n"


def _chunks(data: bytes):
    pos = len(_SIG)
    while pos < len(data):
        n, kind = struct.unpack(">I4s", data[pos:pos + 8])
        yield kind, data[pos + 8:pos + 8 + n]
        pos += 12 + n


def decode(data: bytes) -> tuple[int, int, int, bytearray]:
    """-> (width, height, channels, pixels row-major, 8 bit). Grey/RGB/RGBA/grey+alpha."""
    if not data.startswith(_SIG):
        raise ValueError("not a PNG")
    idat = b""
    w = h = depth = ctype = interlace = 0
    for kind, body in _chunks(data):
        if kind == b"IHDR":
            w, h, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat += body
        elif kind == b"IEND":
            break
    if depth != 8 or interlace:
        raise ValueError(f"unsupported PNG (depth {depth}, interlace {interlace})")
    ch = {0: 1, 2: 3, 4: 2, 6: 4}[ctype]
    raw = zlib.decompress(idat)
    stride = w * ch
    out = bytearray(h * stride)
    prev = bytearray(stride)
    pos = 0
    for y in range(h):
        f = raw[pos]; pos += 1
        line = bytearray(raw[pos:pos + stride]); pos += stride
        if f == 1:
            for i in range(ch, stride):
                line[i] = (line[i] + line[i - ch]) & 255
        elif f == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif f == 3:
            for i in range(stride):
                a = line[i - ch] if i >= ch else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 255
        elif f == 4:
            for i in range(stride):
                a = line[i - ch] if i >= ch else 0
                b = prev[i]
                c = prev[i - ch] if i >= ch else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 255
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return w, h, ch, out


def encode_rgba(w: int, h: int, rgba: bytes | bytearray) -> bytes:
    stride = w * 4
    raw = b"".join(b"\x00" + bytes(rgba[y * stride:(y + 1) * stride]) for y in range(h))

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
    return (_SIG + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b""))


def heatmap_with_alpha(data: bytes, opacity: float = 0.65, floor: float = 0.08, flip_rows: bool = False) -> bytes:
    """RGB heat map -> RGBA: alpha = opacity, fading to 0 where the map is at its floor (dark
    background pixels), so the copper stays readable under the overlay."""
    w, h, ch, px = decode(data)
    out = bytearray(w * h * 4)
    rows = range(h - 1, -1, -1) if flip_rows else range(h)
    o = 0
    for y in rows:
        base = y * w * ch
        for x in range(w):
            i = base + x * ch
            if ch >= 3:
                r, g, b = px[i], px[i + 1], px[i + 2]
            else:
                r = g = b = px[i]
            lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255.0
            a = 0.0 if lum < floor else min(1.0, (lum - floor) / (0.35 - floor) if lum < 0.35 else 1.0)
            out[o:o + 4] = bytes((r, g, b, int(255 * opacity * a)))
            o += 4
    return encode_rgba(w, h, out)
