import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import importlib  # noqa: E402

try:
    mp = importlib.import_module("z2ems.mesh_preview")
except Exception:  # noqa: BLE001  - real kipy missing: test the functions from source instead
    src = (ROOT / "z2ems" / "mesh_preview.py").read_text(encoding="utf-8")
    ns = {}
    for fn in ("frame_flip", "to_kicad", "quality_text"):
        start = src.index(f"def {fn}(")
        end = src.index("\n\n\n", start)
        exec(src[start:end], ns)  # noqa: S102
    mp = types.SimpleNamespace(**ns)

MESH = dict(x=[-10, 0, 5, 10, 20, 30], y=[-10, 0, 2, 4, 14, 20], z=[0, 1.6], res_mm=0.5, bbox=[0, 0, 20, 14],
            ports=[dict(number=1, ref="J1", start=[1, -3, 0], stop=[1, -4, 0.5], axis=0)],
            quality=dict(cells=1000000, min_mm=[0.1, 0.1, 0.02], max_mm=[1, 1, 0.5], grading=1.5, aspect=20, cells_per_wavelength=20, grade="ok"))


def test_flip_detected_from_port_plane():
    pads = {("J1", "1"): (1.0, 3.5)}          # KiCad y down: pad at +3.5, engine port at -3.5 -> mirrored
    assert mp.frame_flip(MESH, pads) is True
    pads = {("J1", "1"): (1.0, -3.5)}
    assert mp.frame_flip(MESH, pads) is False


def test_to_kicad_mirrors_and_clips():
    xs, ys, bbox = mp.to_kicad(MESH, True)
    assert xs == [0, 5, 10, 20]               # -10 and 30 are air/PML lines
    assert ys == [-14, -4, -2, 0]            # -20 (mirrored air line) is clipped too
    assert bbox == (0, -14, 20, 0)
    xs, ys, bbox = mp.to_kicad(MESH, False)
    assert ys == [0, 2, 4, 14] and bbox == (0, 0, 20, 14)


def test_quality_text_mentions_grade():
    t = mp.quality_text(MESH, 4, 5)
    assert "Quality: good" in t and "drawn on the board: 4 x 5" in t


def test_size_text_warns_for_huge_dense_mesh():
    if not hasattr(mp, "size_text"):
        return                                  # source-only fallback (no kipy): covered on the GUI boxes
    small = mp.size_text(MESH, 2, (0, 0, 20, 14), 4, 5, 3)
    assert "WARNING" not in small and "tint" not in small
    big = dict(MESH, x=list(range(2545)), y=list(range(2489)), z=list(range(89)))
    t = mp.size_text(big, 4, (-90, -90, 90, 90), 2515, 2459, 3)
    assert "WARNING" in t and "days" in t and "tint" in t
