"""Fields of a run on the KiCad board: surface current (heat map + current lines) and layer voltage
(signed map + iso-voltage lines), for one driven port, frequency and phase.

The engine (POST /api/field_overlay) renders the pictures and traces the lines from the run's
field export (fields version 2: every excitation, 1 W incident). Here each copper layer the user picks
gets its own user layer `Z2EMS.Field.<tag>` holding
  - the map as a locked reference image (KiCad draws those under the copper: the view is switched to
    high contrast with the other layers dimmed), and
  - the lines as a locked group of segments (drawn above the copper), line width growing with the
    field strength, with chevrons pointing along the current.
Nothing touches copper, nets or the schematic; Clear overlays removes all of it.
"""

from __future__ import annotations

import base64
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

LINES_GROUP_PREFIX = "Z2EMS field "


@dataclass
class FieldChoice:
    exc: int = 1
    k: int = 0
    quantity: str = "J"                 # "J" surface current, "V" voltage to the next layer
    layers: List[str] = field(default_factory=list)
    phase: Optional[float] = None       # None: the instant of the strongest field
    image: bool = True
    lines: bool = True


def latest_v2_run(board_path: Path) -> Optional[Tuple[Path, dict]]:
    """(run dir, result) of the newest run with field data version 2."""
    runs = sorted((board_path.parent / f"{board_path.stem}_twin").glob("*/result.json"))
    for rj in reversed(runs):
        try:
            j = json.loads(rj.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        f = j.get("fields") or {}
        if f.get("version", 1) >= 2 and f.get("excitations"):
            return rj.parent, j
    return None


def port_label(res: dict, e: int) -> str:
    p = (res.get("ports") or [{}] * e)[e - 1] if e - 1 < len(res.get("ports") or []) else {}
    return f"P{e}  {p.get('ref', '')}" + (f" pad {p['pad_no']}" if p.get("pad_no") else "")


def tag(choice: FieldChoice, layer: str, f_ghz: float) -> str:
    q = "J" if choice.quantity == "J" else "V"
    return f"{q}_{layer.replace('.', '_')}_{int(round(f_ghz * 1000))}MHz_P{choice.exc}"


# ---------------------------------------------------------------- geometry (pure)
def to_kicad_pts(pts: List[List[float]], flip: bool) -> List[Tuple[float, float]]:
    return [(float(x), -float(y) if flip else float(y)) for x, y in pts]


def simplify(pts: List[Tuple[float, float]], tol: float) -> List[Tuple[float, float]]:
    """Ramer-Douglas-Peucker on a polyline (iterative)."""
    if len(pts) < 3 or tol <= 0:
        return list(pts)
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        ax, ay = pts[a]
        bx, by = pts[b]
        dx, dy = bx - ax, by - ay
        L = math.hypot(dx, dy) or 1e-12
        best, bi = -1.0, -1
        for i in range(a + 1, b):
            px, py = pts[i]
            d = abs(dy * (px - ax) - dx * (py - ay)) / L
            if d > best:
                best, bi = d, i
        if best > tol and bi > 0:
            keep[bi] = True
            stack += [(a, bi), (bi, b)]
    return [p for p, k in zip(pts, keep) if k]


def line_segments(lines: List[dict], flip: bool, w_min: float, w_max: float, tol: float,
                  chevron_every: float, chevron_len: float, arrows: bool = True) -> List[Tuple[Tuple[float, float], Tuple[float, float], float]]:
    """[(a, b, width)] for KiCad segments: simplified polylines, width by the relative field strength
    (`mag` 0..1 of each line, signed for iso-voltage lines), chevrons along current lines."""
    out = []
    for ln in lines:
        pts = to_kicad_pts(ln["pts"], flip)
        if len(pts) < 2:
            continue
        m = min(1.0, abs(float(ln.get("mag", 1.0))))
        w = w_min + (w_max - w_min) * math.sqrt(m)
        sp = simplify(pts, tol)
        for a, b in zip(sp, sp[1:]):
            if a != b:
                out.append((a, b, w))
        if not arrows:
            continue
        run = 0.0
        for a, b in zip(pts, pts[1:]):
            d = math.hypot(b[0] - a[0], b[1] - a[1])
            run += d
            if run >= chevron_every and d > 0:
                run = 0.0
                ux, uy = (b[0] - a[0]) / d, (b[1] - a[1]) / d
                for s in (1, -1):
                    c = (b[0] - chevron_len * (ux + s * 0.55 * uy), b[1] - chevron_len * (uy - s * 0.55 * ux))
                    out.append((b, c, w))
    return out


# ---------------------------------------------------------------- KiCad side
def clear_field_lines(board) -> int:
    removed = 0
    for grp in board.get_groups():
        if not (grp.name or "").startswith(LINES_GROUP_PREFIX):
            continue
        members = list(grp.items)
        board.remove_items(members + [grp])
        removed += len(members)
    return removed


def draw_lines(board, layer: int, segs, name: str):
    from kipy.board_types import BoardSegment, Group
    from kipy.geometry import Vector2
    from z2ems.ipc import nm
    items = []
    for a, b, w in segs:
        s = BoardSegment()
        s.layer = layer
        s.start = Vector2.from_xy_mm(*a)
        s.end = Vector2.from_xy_mm(*b)
        s.attributes.stroke.width = nm(w)
        items.append(s)
    created = []
    for i in range(0, len(items), 500):
        created += list(board.create_items(items[i:i + 500]))
    if not created:
        return None
    grp = Group()
    grp._proto.name = LINES_GROUP_PREFIX + name   # noqa: SLF001
    grp.items = created
    board.create_items([grp])
    for it in created:
        it.locked = True
    for i in range(0, len(created), 500):
        board.update_items(created[i:i + 500])
    return grp


def apply(board, board_path: Path, setup, base: str, run_dir: Path, res: dict, choice: FieldChoice,
          api: Callable, log: Callable[[str], None] = lambda m: None) -> Tuple[List[str], Optional[int]]:
    """Put the chosen fields on the board. Returns (description lines, the user layer to show)."""
    from z2ems.field_overlay import show_overlay
    from z2ems.layers import ROLE_NAMES, ensure_user_layers, resolve_role_layers
    from z2ems.mesh_preview import frame_flip, pad_positions
    from z2ems.overlays import clear_field_overlays, place_field_overlay

    idx = res["fields"]
    f_ghz = float(idx["f_GHz"][choice.k])
    flip = frame_flip(dict(ports=res.get("ports", [])), pad_positions(board))
    tags = [tag(choice, l, f_ghz) for l in choice.layers]
    # the previous field pictures go, and their user layers are reused for the new ones
    old = resolve_role_layers(board, setup.layers, field_tags=list(setup.layers.fields), allocate=False)
    clear_field_overlays(board, [l for r, l in old.items() if r.startswith("field:")])
    clear_field_lines(board)
    prev = list(setup.layers.fields.values())
    setup.layers.fields = {t: prev[i] for i, t in enumerate(tags) if i < len(prev)}
    ensure_user_layers(board, len(ROLE_NAMES) + len(tags) + 1)
    roles = resolve_role_layers(board, setup.layers, field_tags=tags)
    rel = (run_dir / "result.json").resolve().relative_to(board_path.parent.resolve()).as_posix()
    grid = idx.get("grid") or {}
    span = max(idx["bbox"][2] - idx["bbox"][0], idx["bbox"][3] - idx["bbox"][1])
    w_min, w_max = max(0.01, span / 6000), max(0.03, span / 900)
    out, shown = [], None
    for layer_name, t in zip(choice.layers, tags):
        ul = roles.get(f"field:{t}")
        if ul is None:
            continue
        r = api(base, "/api/field_overlay", dict(result=rel, exc=choice.exc, k=choice.k, quantity=choice.quantity,
                                                 layer=layer_name, phase=choice.phase, image=choice.image,
                                                 lines=choice.lines, row0="max" if flip else "min"), timeout=180)
        x0, y0, x1, y1 = r["bbox"]
        if choice.image and r.get("png"):
            tmp = Path(setup_dir(board_path)) / f"overlay_{t}.png"
            tmp.write_bytes(base64.b64decode(r["png"]))
            nx = int(r["width"])
            px = (x1 - x0) / max(nx - 1, 1)                 # pixel i is centred on x0 + i * px
            top = -y1 if flip else y0                       # KiCad y grows down
            place_field_overlay(board, ul, tmp, (x0 - px / 2, top - px / 2), nx * px, nx)
        n = 0
        if choice.lines and r.get("lines"):
            dx = float(grid.get("dx", span / 900))
            segs = line_segments(r["lines"], flip, w_min, w_max, tol=dx * 0.6,
                                 chevron_every=span / 25, chevron_len=span / 250, arrows=choice.quantity == "J")
            draw_lines(board, ul, segs, t)
            n = len(segs)
        out.append(f"  {layer_name}: {r['legend']['text']}"
                   + (f", {len(r.get('lines') or [])} {'current' if choice.quantity == 'J' else 'iso-voltage'} lines ({n} segments)" if choice.lines else "")
                   + f" -> layer {board.get_layer_name(ul) if hasattr(board, 'get_layer_name') else ul}")
        if shown is None:
            shown = ul
    setup_save(setup, board_path)
    if shown is not None and choice.image:
        out.append("")
        out.append(show_overlay(board, shown, dim=True).strip())
    return out, shown


def setup_dir(board_path: Path) -> Path:
    from z2ems import SIDECAR_SUFFIX
    d = board_path.parent / (board_path.stem + SIDECAR_SUFFIX)
    d.mkdir(parents=True, exist_ok=True)
    return d


def setup_save(setup, board_path: Path) -> None:
    from z2ems import SETUP_FILENAME
    setup.save(setup_dir(board_path) / SETUP_FILENAME)


# ---------------------------------------------------------------- dialog
def ask(parent, res: dict, run_name: str, last: Optional[FieldChoice] = None) -> Optional[FieldChoice]:
    import wx
    idx = res["fields"]
    names = [l["name"] for l in idx.get("layers", [])]
    pairs = [p[0] for p in (idx.get("V") or {}).get("pairs", [])]
    dlg = wx.Dialog(parent, title="Z2Labs EMS - fields on the board", style=wx.DEFAULT_DIALOG_STYLE | wx.STAY_ON_TOP)
    s = wx.BoxSizer(wx.VERTICAL)
    s.Add(wx.StaticText(dlg, label=f"Run {run_name}: fields for 1 W incident at the driven port."), 0, wx.ALL, 10)
    g = wx.FlexGridSizer(0, 2, 6, 10)
    exc = wx.Choice(dlg, choices=[port_label(res, e) for e in idx["excitations"]])
    frq = wx.Choice(dlg, choices=[f"{f:.2f} GHz" for f in idx["f_GHz"]])
    qty = wx.Choice(dlg, choices=["Surface current Js (heat map + current lines)", "Voltage to the next layer (map + iso-voltage lines)"])
    if not pairs:
        qty.Delete(1)
    lay = wx.CheckListBox(dlg, choices=names, size=(-1, 90))
    ph = wx.Choice(dlg, choices=["Instant of the strongest field"] + [f"{d} deg" for d in range(0, 360, 30)])
    for lbl, c in (("Driven port", exc), ("Frequency", frq), ("Show", qty), ("Copper layers", lay), ("Phase", ph)):
        g.Add(wx.StaticText(dlg, label=lbl), 0, wx.ALIGN_CENTER_VERTICAL)
        g.Add(c, 1, wx.EXPAND)
    s.Add(g, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, 10)
    img = wx.CheckBox(dlg, label="Colour map (under the copper: the view switches to high contrast)")
    lin = wx.CheckBox(dlg, label="Lines (above the copper; width = field strength, chevrons = current direction)")
    s.Add(img, 0, wx.LEFT | wx.TOP, 10)
    s.Add(lin, 0, wx.LEFT | wx.TOP, 10)
    s.Add(dlg.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.ALL | wx.ALIGN_RIGHT, 10)
    dlg.SetSizerAndFit(s)
    c = last or FieldChoice()
    exc.SetSelection(max(0, idx["excitations"].index(c.exc)) if c.exc in idx["excitations"] else 0)
    frq.SetSelection(min(c.k, len(idx["f_GHz"]) - 1) if last else min(1, len(idx["f_GHz"]) - 1))
    qty.SetSelection(1 if (c.quantity == "V" and pairs) else 0)
    sel = c.layers or names[:2]

    def fill_layers(_=None):
        v = qty.GetSelection() == 1
        lay.Set(pairs if v else names)
        for i, n in enumerate(pairs if v else names):
            lay.Check(i, n in sel)
    fill_layers()
    qty.Bind(wx.EVT_CHOICE, fill_layers)
    ph.SetSelection(0 if c.phase is None else 1 + int(c.phase // 30))
    img.SetValue(c.image)
    lin.SetValue(c.lines)
    if dlg.ShowModal() != wx.ID_OK:
        dlg.Destroy()
        return None
    v = qty.GetSelection() == 1
    out = FieldChoice(exc=idx["excitations"][exc.GetSelection()], k=frq.GetSelection(), quantity="V" if v else "J",
                      layers=[n for i, n in enumerate(pairs if v else names) if lay.IsChecked(i)],
                      phase=None if ph.GetSelection() == 0 else 30.0 * (ph.GetSelection() - 1),
                      image=img.GetValue(), lines=lin.GetValue())
    dlg.Destroy()
    return out
