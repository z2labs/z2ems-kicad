"""Port discovery and suggestions.

A port is a `Z2EMS_PORT` footprint instance (two pads: SIG, REF). This module
reads them into plain `Port` records and proposes *suggested* port locations
from RF connectors, 50-ohm net classes and dangling RF trace ends. Suggestions
are never applied automatically (docs/DESIGN.md section 6.2).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from kipy.board import Board
from kipy.board_types import FootprintInstance, Pad
from kipy.proto.board.board_types_pb2 import BoardLayer

from . import (FIELD_DIR, FIELD_EXCITE, FIELD_LAYER, FIELD_REFPLANE, FIELD_TYPE, FIELD_Z0,
               PORT_TYPES)
from .ipc import find_port_footprints, footprint_fields, mm

RF_CONNECTOR_TOKENS = ["SMA", "SMB", "MMCX", "MCX", "u\\.?FL", "IPEX", "IPX", "MHF\\d?", "N[- ]?Type",
                       "BNC", "TNC", "RP[- ]?SMA", "Coax(ial)?", "SMP", "SMPM", "2\\.92mm", "2\\.4mm"]
# '_' counts as a separator (KiCad names use it), so plain \b is not enough
RF_CONNECTOR_PATTERNS = [rf"(?<![A-Za-z0-9]){t}(?![A-Za-z0-9])" for t in RF_CONNECTOR_TOKENS]
_RF_RE = re.compile("|".join(RF_CONNECTOR_PATTERNS), re.IGNORECASE)
# references that are never RF connectors even if the package name says "SMA"
_NON_CONNECTOR_REF = re.compile(r"^(D|R|C|L|Q|U|IC|FB|Y|X|TP|F|T|K|SW)\d", re.IGNORECASE)
GND_NET_RE = re.compile(r"^(GND|AGND|DGND|PGND|RFGND|GNDA|GNDD|VSS|0V)(\d+|_(RF|A|D|PWR|IO|[0-9]+))?$", re.IGNORECASE)


@dataclass
class Port:
    uuid: str
    reference: str                 # P1, P2, ...
    z0: float = 50.0
    type: str = "lumped"
    excite: bool = True
    direction: str = "auto"
    refplane_mm: float = 0.0
    layer: str = "auto"
    sig_net: str = ""
    ref_net: str = ""
    x_mm: float = 0.0
    y_mm: float = 0.0
    issues: List[str] = field(default_factory=list)


@dataclass
class PortSuggestion:
    reason: str                    # "connector J3 (SMA)", "net class RF50 end", ...
    x_mm: float
    y_mm: float
    net: str
    layer: str
    footprint_ref: Optional[str] = None
    pad_number: Optional[str] = None
    score: float = 0.5


def read_ports(board: Board) -> List[Port]:
    ports: List[Port] = []
    for fp in find_port_footprints(board):
        f = footprint_fields(fp)
        p = Port(uuid=str(fp.id.value), reference=fp.reference_field.text.value)
        p.z0 = _float(f.get(FIELD_Z0), 50.0, p.issues, "Z0")
        p.type = (f.get(FIELD_TYPE) or "lumped").strip().lower()
        if p.type not in PORT_TYPES:
            p.issues.append(f"unknown port type '{p.type}'")
        p.excite = (f.get(FIELD_EXCITE) or "yes").strip().lower() in ("yes", "1", "true", "y")
        p.direction = (f.get(FIELD_DIR) or "auto").strip().lower()
        p.refplane_mm = _float(f.get(FIELD_REFPLANE), 0.0, p.issues, "RefPlane")
        p.layer = (f.get(FIELD_LAYER) or "auto").strip()
        p.x_mm, p.y_mm = mm(fp.position.x), mm(fp.position.y)
        pads: Dict[str, Pad] = {pd.number: pd for pd in fp.definition.pads}
        sig, ref = pads.get("1") or pads.get("SIG"), pads.get("2") or pads.get("REF")
        if sig is not None:
            p.sig_net = net_name(sig)
        if ref is not None:
            p.ref_net = net_name(ref)
        if not p.sig_net:
            p.issues.append("SIG pad has no net (port not connected to a signal)")
        if not p.ref_net:
            p.issues.append("REF pad has no net (port not touching ground)")
        elif not GND_NET_RE.match(p.ref_net):
            p.issues.append(f"REF pad net '{p.ref_net}' does not look like ground")
        if p.sig_net and p.sig_net == p.ref_net:
            p.issues.append("SIG and REF are on the same net (shorted port)")
        ports.append(p)
    ports.sort(key=lambda q: _natural(q.reference))
    return ports


def _float(v: Optional[str], default: float, issues: List[str], label: str) -> float:
    if v is None or not str(v).strip():
        return default
    try:
        return float(str(v).replace(",", ".").split()[0])
    except ValueError:
        issues.append(f"{label}: cannot parse '{v}'")
        return default


def _natural(s: str):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


# --- suggestions ---------------------------------------------------------------

def suggest_ports(board: Board, ground_nets: Sequence[str] = ()) -> List[PortSuggestion]:
    """Heuristic port suggestions. Cheap, read-only."""
    gnd = set(ground_nets)
    out: List[PortSuggestion] = []
    existing = {(p.sig_net) for p in read_ports(board)}

    # 1. RF connectors: an RF-connector-looking footprint that has at least one
    #    ground pad (shield); the signal pad is the non-ground one.
    #    "SMA"/"SMB"/"SMC" are also DO-214 diode packages, so passive/diode refs
    #    and Diode_* libraries are excluded explicitly.
    for fp in board.get_footprints():
        if _is_port_fp(fp):
            continue
        ref = fp.reference_field.text.value
        name = _fp_lib_name(fp)
        value = fp.value_field.text.value
        if _NON_CONNECTOR_REF.match(ref) or re.search(r"Diode|D_SM[ABC]|DO-214", name, re.IGNORECASE):
            continue
        text = f"{name} {value} {ref}"
        if not _RF_RE.search(text):
            continue
        pads = list(fp.definition.pads)
        nets = [net_name(pd) for pd in pads]
        has_gnd = any(n and (n in gnd or GND_NET_RE.match(n)) for n in nets)
        # single-pad launch pads (test coupons) have no shield pad: keep them, lower score
        if not has_gnd and len(pads) > 1:
            continue
        score = 0.9 if has_gnd else 0.7
        seen_nets: set[str] = set()
        for pad in pads:
            n = net_name(pad)
            if not n or n in gnd or GND_NET_RE.match(n) or n in existing or n in seen_nets:
                continue
            seen_nets.add(n)
            out.append(PortSuggestion(
                reason=f"RF connector {ref} ({value or name})",
                x_mm=mm(pad.position.x), y_mm=mm(pad.position.y), net=n,
                layer=_pad_layer(board, pad), footprint_ref=ref,
                pad_number=pad.number, score=score))

    # 2. nets in impedance-controlled / RF-looking net classes
    try:
        nets = board.get_nets()
        classes = board.get_netclass_for_nets(nets)
        for net in nets:
            nc = classes.get(net.name)
            if nc is None:
                continue
            ncname = nc.name
            if re.search(r"50|RF|coax|imped", ncname, re.IGNORECASE) and net.name not in existing:
                if any(s.net == net.name for s in out):
                    continue
                out.append(PortSuggestion(reason=f"net class '{ncname}'", x_mm=0.0, y_mm=0.0,
                                          net=net.name, layer="", score=0.5))
    except Exception:
        pass   # net class lookup is optional

    out.sort(key=lambda s: -s.score)
    return out


def net_name(item) -> str:
    """Net name of a pad or zone; "" when it has none (rule areas / keepouts, unconnected pads)."""
    n = getattr(item, "net", None)
    return (getattr(n, "name", "") or "") if n is not None else ""


def cu_layer_name(board, layer) -> str:
    """Canonical copper layer name ('F.Cu', 'In1.Cu', 'B.Cu') also when the layer has a user name
    ('In1_GND', 'Top'): the engine and the .kicad_pcb file use the canonical names. Other layers: the
    name KiCad shows."""
    try:
        n = BoardLayer.Name(int(layer))
    except Exception:  # noqa: BLE001
        n = ""
    m = re.match(r"BL_(F|B|In\d+)_Cu$", n)
    if m:
        return f"{m.group(1)}.Cu"
    try:
        return board.get_layer_name(layer)
    except Exception:  # noqa: BLE001
        return n


def detect_ground_nets(board: Board) -> List[str]:
    names = [n.name for n in board.get_nets()]
    gnd = [n for n in names if GND_NET_RE.match(n)]
    # prefer nets that actually have zones (planes)
    zone_nets = {net_name(z) for z in board.get_zones()} - {""}
    gnd.sort(key=lambda n: (n not in zone_nets, n))
    return gnd


def _is_port_fp(fp: FootprintInstance) -> bool:
    from .ipc import is_port_footprint
    return is_port_footprint(fp)


def _fp_lib_name(fp: FootprintInstance) -> str:
    try:
        return f"{fp.definition.id.library}:{fp.definition.id.name}"
    except Exception:
        return ""


def _pad_layer(board: Board, pad: Pad) -> str:
    try:
        layers = list(pad.padstack.layers)
        return cu_layer_name(board, layers[0]) if layers else ""
    except Exception:
        return ""
