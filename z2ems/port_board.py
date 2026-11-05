"""Ports <-> the live board: terminals from pads, ports from the KiCad selection, suggestions, markers.

Markers: one KiCad group per port ("Z2EMS port P1") on the Z2EMS.Ports user layer - a ring with + at the
signal pad, a ring with - at every ground pad and an arrow from each - to the +, or for a vertical port a
down-arrow and the reference layer name; a label "P1 50 Ω". User-layer graphics: no copper, no DRC/ERC
impact, not in the BOM or the fabrication outputs. They are redrawn from the port definitions; moving a
footprint and redrawing moves its marker.
"""

from __future__ import annotations

import math
from typing import Iterable, Optional, Sequence, Tuple

from .portdefs import PortDef, Terminal
from .ports import GND_NET_RE, cu_layer_name, net_name

MARKER_GROUP_PREFIX = "Z2EMS port "


# --- pads and terminals ---------------------------------------------------------------------------

def _ref(fp) -> str:
    return fp.reference_field.text.value


def pad_layer_name(board, pad) -> str:
    try:
        layers = list(pad.padstack.layers)
        names = [cu_layer_name(board, l) for l in layers]
        cu = [n for n in names if n.endswith(".Cu")]
        if "F.Cu" in cu:
            return "F.Cu"
        return cu[0] if cu else (names[0] if names else "")
    except Exception:
        return ""


def is_ground(net: str, ground_nets: Sequence[str] = ()) -> bool:
    return bool(net) and (net in ground_nets or bool(GND_NET_RE.match(net)))


def terminal(board, fp, pad) -> Terminal:
    layer = pad_layer_name(board, pad)
    try:
        same = sum(1 for q in fp.definition.pads
                   if str(q.number) == str(pad.number) and pad_layer_name(board, q) == layer)
    except Exception:  # noqa: BLE001
        same = 1
    return Terminal(ref=_ref(fp), pad=str(pad.number), net=net_name(pad), layer=layer, count=max(1, same))


def footprint_of(board, item):
    """The footprint a selected pad belongs to (None when the item is not a pad of a board footprint)."""
    iid = getattr(getattr(item, "id", None), "value", None)
    par = getattr(getattr(item, "parent", None), "value", None)      # kipy >= 0.8 (KiCad 10.0.6)
    for fp in board.get_footprints():
        if par is not None and getattr(getattr(fp, "id", None), "value", None) == par:
            return fp
    for fp in board.get_footprints():
        if fp is item or getattr(getattr(fp, "id", None), "value", None) == iid:
            return fp
        for pd in fp.definition.pads:
            if iid is not None and getattr(getattr(pd, "id", None), "value", None) == iid:
                return fp
    return None


def plane_below(board, layer: str, ground_nets: Sequence[str] = ()) -> Optional[str]:
    """The nearest copper layer on the other side of the dielectric from `layer` that carries a ground zone."""
    try:
        cu = sorted({cu_layer_name(board, l) for l in board.get_enabled_layers()}, key=_cu_order)
        cu = [c for c in cu if c.endswith(".Cu")]
    except Exception:
        cu = []
    zones = []
    try:
        for z in board.get_zones():
            n = net_name(z)
            if is_ground(n, ground_nets):
                zones += [cu_layer_name(board, l) for l in getattr(z, "layers", [])]
    except Exception:
        pass
    if not cu:
        cu = sorted(set(zones) | {layer}, key=_cu_order)
    if layer not in cu:
        return None
    i = cu.index(layer)
    order = cu[i + 1:] if i == 0 else list(reversed(cu[:i]))
    for l in order:
        if l in zones:
            return l
    return order[0] if order else None


def _cu_order(name: str) -> Tuple[int, int]:
    if name == "F.Cu":
        return (0, 0)
    if name == "B.Cu":
        return (2, 0)
    try:
        return (1, int(name[2:].split(".")[0]))
    except ValueError:
        return (1, 99)


def port_for_footprint(board, fp, n: int, ground_nets: Sequence[str] = (), plus_pad=None) -> PortDef:
    """A port at a footprint: + = the given pad or its single signal pad, - = its ground pads on the same
    layer (connector shield / IC GND pins next to the + pin), else the ground plane below the pad."""
    pads = list(fp.definition.pads)
    if plus_pad is None:
        sig = [p for p in pads if net_name(p) and not is_ground(net_name(p), ground_nets)]
        plus_pad = sig[0] if len(sig) == 1 else None
    port = PortDef(n=n)
    if plus_pad is None:
        return port
    port.plus = terminal(board, fp, plus_pad)
    gnd = [p for p in pads if is_ground(net_name(p), ground_nets) and pad_layer_name(board, p) == port.plus.layer]
    if len(pads) > 6 and gnd:                         # an IC: only the GND pins next to the + pin
        gnd = nearest_pads(plus_pad, gnd, k=2)
    seen = set()
    for g in gnd:
        t = terminal(board, fp, g)
        if (t.ref, t.pad) not in seen:
            seen.add((t.ref, t.pad))
            port.minus_pads.append(t)
    if not port.minus_pads:
        port.minus_plane = plane_below(board, port.plus.layer, ground_nets)
    return port


def nearest_pads(ref_pad, pads, k=2):
    rx, ry = ref_pad.position.x, ref_pad.position.y
    return sorted(pads, key=lambda p: math.hypot(p.position.x - rx, p.position.y - ry))[:k]


def port_from_selection(board, n: int, ground_nets: Sequence[str] = ()) -> Tuple[Optional[PortDef], str]:
    """A port from what is selected in the PCB editor:
         one footprint             -> port_for_footprint (its signal pad, its GND pads / plane below)
         one pad                   -> that pad as +, the footprint's GND pads (or plane) as -
         one signal pad + GND pads -> exactly that
    Returns (port or None, message)."""
    sel = list(board.get_selection())
    if not sel:
        return None, "Nothing is selected: select a connector, a pad or a pad and its ground pads in the PCB editor."
    pads, fps = [], []
    for it in sel:
        if hasattr(it, "padstack") and hasattr(it, "number"):
            pads.append(it)
        elif hasattr(it, "definition") and hasattr(it, "reference_field"):
            fps.append(it)
    if not pads and len(fps) == 1:
        p = port_for_footprint(board, fps[0], n, ground_nets)
        if p.plus is None:
            return p, f"{_ref(fps[0])} has more than one signal pad: select the pad for +."
        return p, f"{p.name}: + {p.plus_text()},  - {p.minus_text()}"
    if not pads:
        return None, "Select a footprint or pads (tracks are not port terminals yet)."
    sig = [p for p in pads if not is_ground(net_name(p), ground_nets)]
    gnd = [p for p in pads if is_ground(net_name(p), ground_nets)]
    if not sig and gnd:
        # only ground pads selected - usually a click on a connector's shield pad: take the whole footprint
        owners = {_ref(f): f for f in (footprint_of(board, g) for g in gnd) if f is not None}
        if len(owners) == 1:
            fp = next(iter(owners.values()))
            p = port_for_footprint(board, fp, n, ground_nets)
            if p.plus is None:
                return p, f"{_ref(fp)} has more than one signal pad: select the pad for +."
            return p, f"{p.name} from {_ref(fp)}: + {p.plus_text()},  - {p.minus_text()}"
    if len(sig) != 1:
        return None, "Select exactly one signal pad for + (plus any ground pads for -)."
    fp = footprint_of(board, sig[0])
    if fp is None:
        return None, "The selected pad belongs to no footprint."
    if not gnd:
        p = port_for_footprint(board, fp, n, ground_nets, plus_pad=sig[0])
    else:
        p = PortDef(n=n, plus=terminal(board, fp, sig[0]))
        seen = set()
        for g in gnd:
            gfp = footprint_of(board, g)
            if gfp is None:
                continue
            t = terminal(board, gfp, g)
            if (t.ref, t.pad) not in seen:
                seen.add((t.ref, t.pad))
                p.minus_pads.append(t)
    return p, f"{p.name}: + {p.plus_text()},  - {p.minus_text()}"


def set_minus_from_selection(board, port: PortDef, ground_nets: Sequence[str] = ()) -> str:
    """'Set - here': the selected ground pads become the reference of `port`."""
    sel = [it for it in board.get_selection() if hasattr(it, "padstack") and hasattr(it, "number")]
    if not sel:
        return "Select the ground pad(s) for - in the PCB editor."
    new = []
    for g in sel:
        fp = footprint_of(board, g)
        if fp is not None:
            new.append(terminal(board, fp, g))
    if not new:
        return "The selected pads belong to no footprint."
    port.minus_pads = new
    port.minus_plane = None
    return f"{port.name}: - {port.minus_text()}"


def set_plus_from_selection(board, port: PortDef) -> str:
    sel = [it for it in board.get_selection() if hasattr(it, "padstack") and hasattr(it, "number")]
    if len(sel) != 1:
        return "Select exactly one pad for + in the PCB editor."
    fp = footprint_of(board, sel[0])
    if fp is None:
        return "The selected pad belongs to no footprint."
    port.plus = terminal(board, fp, sel[0])
    return f"{port.name}: + {port.plus_text()}"


def find_pads(board, term: Terminal) -> list:
    for fp in board.get_footprints():
        if _ref(fp) == term.ref:
            return [p for p in fp.definition.pads if str(p.number) == str(term.pad)
                    and (not term.layer or pad_layer_name(board, p) == term.layer)]
    return []


def select_port(board, port: PortDef) -> int:
    """Select the port's pads in the PCB editor (the 'Show' button)."""
    items = []
    if port.plus:
        items += find_pads(board, port.plus)
    for m in port.minus_pads:
        items += find_pads(board, m)
    board.clear_selection()
    if items:
        board.add_to_selection(items)
    return len(items)


# --- markers ---------------------------------------------------------------------------------------

def marker_geometry(port: PortDef, plus_xy: Tuple[float, float], minus_xy: Sequence[Tuple[float, float]],
                    size_mm: float = 0.6) -> dict:
    """Marker shapes in board millimetres (pure, for tests and for drawing):
    circles [(x, y, r)], segments [((x0, y0), (x1, y1))], texts [(x, y, text, height)]."""
    r = size_mm
    circles, segs, texts = [(plus_xy[0], plus_xy[1], r)], [], []
    texts.append((plus_xy[0], plus_xy[1], "+", r * 1.2))
    for (mx, my) in minus_xy:
        circles.append((mx, my, r))
        texts.append((mx, my, "-", r * 1.2))
        dx, dy = plus_xy[0] - mx, plus_xy[1] - my
        d = math.hypot(dx, dy)
        if d > 2.2 * r:
            ux, uy = dx / d, dy / d
            a = (mx + ux * r * 1.1, my + uy * r * 1.1)
            b = (plus_xy[0] - ux * r * 1.1, plus_xy[1] - uy * r * 1.1)
            segs.append((a, b))
            hx, hy = -uy, ux                                  # arrow head at b
            segs.append((b, (b[0] - ux * r * 0.7 + hx * r * 0.45, b[1] - uy * r * 0.7 + hy * r * 0.45)))
            segs.append((b, (b[0] - ux * r * 0.7 - hx * r * 0.45, b[1] - uy * r * 0.7 - hy * r * 0.45)))
    if not minus_xy and port.minus_plane:                     # vertical: a down arrow and the layer name
        x, y = plus_xy[0] + r * 1.8, plus_xy[1]
        segs += [((x, y - r), (x, y + r)), ((x, y + r), (x - r * 0.4, y + r * 0.45)), ((x, y + r), (x + r * 0.4, y + r * 0.45)),
                 ((x - r * 0.5, y + r * 1.15), (x + r * 0.5, y + r * 1.15))]
        texts.append((x + r * 0.6, y + r * 2.0, port.minus_plane, r * 0.8))
    # above the usual reference-designator spot (~1.5 mm over the pad), so "P1 50 Ω" does not sit on "J1"
    texts.append((plus_xy[0] + r * 1.6, plus_xy[1] - r * 4.2, f"{port.name} {port.z0:g} Ω", r * 1.3))
    return dict(circles=circles, segments=segs, texts=texts)


def clear_markers(board, only: Optional[Iterable[str]] = None) -> int:
    names = None if only is None else {MARKER_GROUP_PREFIX + n for n in only}
    victims = []
    for g in board.get_groups():
        if g.name.startswith(MARKER_GROUP_PREFIX) and (names is None or g.name in names):
            victims += list(g.items) + [g]
    if victims:
        board.remove_items(victims)
    return len(victims)


def draw_markers(board, ports: Sequence[PortDef], layer: int, line_width_mm: float = 0.12) -> int:
    """(Re)draw all port markers on `layer`, one group per port."""
    from kipy.board_types import BoardCircle, BoardSegment, BoardText, Group
    from kipy.geometry import Vector2
    from .ipc import mm, nm

    clear_markers(board)
    drawn = 0
    for port in ports:
        if port.plus is None:
            continue
        pp = find_pads(board, port.plus)
        if not pp:
            continue
        pxy = (mm(pp[0].position.x), mm(pp[0].position.y))
        mxy = []
        for m in port.minus_pads:
            for q in find_pads(board, m):
                mxy.append((mm(q.position.x), mm(q.position.y)))
        g = marker_geometry(port, pxy, mxy)
        items = []
        for (x, y, r) in g["circles"]:
            c = BoardCircle()
            c.layer = layer
            c.center = Vector2.from_xy_mm(x, y)
            c.radius_point = Vector2.from_xy_mm(x + r, y)
            c.attributes.stroke.width = nm(line_width_mm)
            items.append(c)
        for (a, b) in g["segments"]:
            s = BoardSegment()
            s.layer = layer
            s.start = Vector2.from_xy_mm(*a)
            s.end = Vector2.from_xy_mm(*b)
            s.attributes.stroke.width = nm(line_width_mm)
            items.append(s)
        for (x, y, text, h) in g["texts"]:
            t = BoardText()
            t.layer = layer
            t.value = text
            t.position = Vector2.from_xy_mm(x, y)
            t.attributes.size = Vector2.from_xy_mm(h, h)
            t.attributes.stroke_width = nm(max(0.08, h * 0.12))
            items.append(t)
        created = list(board.create_items(items))
        grp = Group()
        grp._proto.name = MARKER_GROUP_PREFIX + port.name   # noqa: SLF001 - no public setter in kipy 0.8
        grp.items = created
        board.create_items([grp])
        drawn += 1
    return drawn
