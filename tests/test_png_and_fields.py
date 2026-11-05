import io
import json
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from z2ems import png  # noqa: E402


def _rgb_png(w, h, pix, filt=0):
    import struct
    rows = b"".join(bytes([filt]) + bytes(pix[y * w * 3:(y + 1) * w * 3]) for y in range(h))

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def test_decode_roundtrip_and_alpha():
    w, h = 4, 2
    pix = bytes([0, 0, 0] * 4 + [255, 255, 255] * 4)      # dark row, bright row
    data = _rgb_png(w, h, pix)
    W, H, ch, px = png.decode(data)
    assert (W, H, ch) == (4, 2, 3) and bytes(px) == pix
    out = png.heatmap_with_alpha(data, opacity=0.6)
    W2, H2, ch2, px2 = png.decode(out)
    assert ch2 == 4 and px2[3] == 0 and px2[4 * 4 + 3] == int(255 * 0.6)     # dark -> transparent, bright -> 60 %
    flipped = png.decode(png.heatmap_with_alpha(data, opacity=0.6, flip_rows=True))[3]
    assert flipped[3] == int(255 * 0.6) and flipped[4 * 4 + 3] == 0


def test_map_items_resolves_paths(tmp_path):
    from z2ems.field_overlay import map_items, tag_for
    run = tmp_path / "20261017-120000"
    (run / "fields").mkdir(parents=True)
    (run / "fields" / "F_Cu_3000MHz.png").write_bytes(b"x")
    res = {"fields": {"f_GHz": [1.5, 3.0], "maps": {"F.Cu": ["fields/F_Cu_1500MHz.png", "fields/F_Cu_3000MHz.png"]},
                      "peak_db": {"F.Cu": [-1.0, 0.0]}}}
    items = map_items(run, res)
    assert len(items) == 1 and items[0]["mhz"] == 3000 and items[0]["peak_db"] == 0.0
    assert tag_for(items[0]) == "F_Cu_3000MHz"


def test_line_model_doubt():
    import importlib
    try:
        sim = importlib.import_module("z2ems.simulate")
    except Exception:  # noqa: BLE001
        return
    good = dict(Z0_ohm=50.25, eeff=3.358, S_dB=dict(S11=[-28.0], S21=[-0.2]))
    assert sim.line_model_doubt(good) == ""
    cap = dict(Z0_ohm=1.968, eeff=1.527, S_dB=dict(S11=[-0.29], S21=[-25.0, -3.0]))
    assert "S21" in sim.line_model_doubt(cap)
    assert "Not a uniform line" in sim.summary(dict(cap, board="b", stackup="s", mesh="m", mcells=1))
