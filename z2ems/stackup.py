"""Stackup read / validate / preset apply.

Reading uses `Board.get_stackup()` (kicad-python >= 0.8). Writing back uses the
`UpdateBoardStackup` IPC command, for which 0.8.0 ships the protobuf message but
no wrapper method; `apply_stackup` sends it directly. Whether the running KiCad
honours it is a Spike 0 item.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from kipy.board import Board, BoardStackup
from kipy.proto.board import board_commands_pb2, board_pb2

from .ipc import mm, nm

PRESET_DIR = Path(__file__).resolve().parent.parent / "presets" / "stackups"
# imported / hand-made presets live outside the plugin folder, so a plugin update (PCM) keeps them
USER_PRESET_DIR = Path.home() / ".z2ems" / "stackups"
LEGACY_USER_PRESET_DIR = PRESET_DIR / "user"    # before 0.1.0 (read only)


@dataclass
class StackupLayerView:
    kind: str                     # copper | dielectric | soldermask | silkscreen | other
    name: str
    thickness_mm: float
    epsilon_r: Optional[float] = None
    loss_tangent: Optional[float] = None
    material: str = ""
    sublayers: int = 1


@dataclass
class StackupReport:
    layers: List[StackupLayerView] = field(default_factory=list)
    issues: List[str] = field(default_factory=list)
    total_thickness_mm: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.issues


def read_stackup(board: Board) -> StackupReport:
    su = board.get_stackup()
    rep = StackupReport()
    for L in su.layers:
        if not L.enabled:
            continue
        t = board_pb2.BoardStackupLayerType.Name(L.type)
        if t == "BSLT_COPPER":
            rep.layers.append(StackupLayerView("copper", L.user_name or _layer_name(board, L.layer),
                                               mm(L.thickness)))
        elif t == "BSLT_DIELECTRIC" and L.dielectric is not None:
            props = L.dielectric.layers
            kind = board_pb2.BoardStackupDielectricType.Name(L.dielectric.type).replace("BSDT_", "").lower()
            # a dielectric layer may be several sub-layers (e.g. two prepregs)
            tot = sum(mm(p.thickness) for p in props)
            # KiCad 10.0.7 can report a phantom dielectric (type none, 0 mm, er 4.5) that is
            # not a physical layer (seen live on the 'dbg' board, macOS soak 2026-10-17). Skip it.
            if kind == "none" or tot <= 0:
                continue
            er = props[0].epsilon_r if props else None
            tand = props[0].loss_tangent if props else None
            mat = props[0].material_name if props else ""
            rep.layers.append(StackupLayerView("dielectric", kind, tot, er, tand, mat, len(props)))
        elif t == "BSLT_SOLDERMASK" and L.soldermask is not None:
            rep.layers.append(StackupLayerView("soldermask", _layer_name(board, L.layer),
                                               mm(L.soldermask.thickness), L.soldermask.epsilon_r,
                                               L.soldermask.loss_tangent, L.soldermask.material_name))
        elif t == "BSLT_SILKSCREEN":
            rep.layers.append(StackupLayerView("silkscreen", _layer_name(board, L.layer), 0.0))
        else:
            rep.layers.append(StackupLayerView("other", _layer_name(board, L.layer), mm(L.thickness)))
    rep.total_thickness_mm = sum(l.thickness_mm for l in rep.layers)
    rep.issues = validate(rep)
    return rep


def _layer_name(board: Board, layer: int) -> str:
    try:
        return board.get_layer_name(layer)
    except Exception:
        return str(layer)


def validate(rep: StackupReport) -> List[str]:
    issues: List[str] = []
    diel = [l for l in rep.layers if l.kind == "dielectric"]
    cu = [l for l in rep.layers if l.kind == "copper"]
    if not diel:
        issues.append("no dielectric layers in stackup")
    for d in diel:
        if d.epsilon_r is None or d.epsilon_r < 1.5 or d.epsilon_r > 12:
            issues.append(f"dielectric '{d.name}': implausible epsilon_r={d.epsilon_r}")
        if d.loss_tangent is None or d.loss_tangent <= 0 or d.loss_tangent > 0.05:
            issues.append(f"dielectric '{d.name}': implausible loss tangent={d.loss_tangent}")
        if d.thickness_mm <= 0.02:
            issues.append(f"dielectric '{d.name}': thickness {d.thickness_mm:.4f} mm too small")
    for c in cu:
        if c.thickness_mm <= 0 or c.thickness_mm > 0.2:
            issues.append(f"copper '{c.name}': implausible thickness {c.thickness_mm:.4f} mm")
    if rep.total_thickness_mm and not (0.2 <= rep.total_thickness_mm <= 4.0):
        issues.append(f"total thickness {rep.total_thickness_mm:.3f} mm outside 0.2..4.0 mm")
    # KiCad default stackups often carry the generic 4.5 / 0.02 placeholder on every layer
    if diel and all(abs((d.epsilon_r or 0) - 4.5) < 1e-6 and abs((d.loss_tangent or 0) - 0.02) < 1e-6 for d in diel):
        issues.append("all dielectrics carry KiCad default values (4.5 / 0.02) - probably never set; pick a fab preset")
    return issues


# --- presets -------------------------------------------------------------------

def list_presets() -> List[dict]:
    out = []
    for d in (PRESET_DIR, LEGACY_USER_PRESET_DIR, USER_PRESET_DIR):
        for p in sorted(d.glob("*.json")) if d.is_dir() else ():
            try:
                pr = json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            pr["_path"] = str(p)
            out.append(pr)
    return out


def load_preset(preset_id: str) -> dict:
    for p in list_presets():
        if p["id"] == preset_id:
            return p
    raise KeyError(preset_id)


def preset_matches_board(preset: dict, board: Board) -> bool:
    return preset["copper_layers"] == board.get_copper_layer_count()


def apply_preset(board: Board, preset: dict) -> BoardStackup:
    """Fill the board's existing stackup entries from a preset (same copper count).
    Returns the modified stackup; call `apply_stackup` to push it to KiCad."""
    su = board.get_stackup()
    # NOTE: kipy 0.8.0 wrappers *copy* their protos, so edits through
    # `su.layers[i]` would be lost. Work on the raw proto repeated field instead.
    proto = su._proto  # noqa: SLF001
    pres_cu = [l for l in preset["layers"] if l["type"] == "copper"]
    pres_di = [l for l in preset["layers"] if l["type"] in ("core", "prepreg")]
    pres_sm = [l for l in preset["layers"] if l["type"] == "soldermask"]
    i_cu = i_di = i_sm = 0
    for L in proto.layers:
        if not L.enabled:
            continue
        if L.type == board_pb2.BSLT_COPPER and i_cu < len(pres_cu):
            L.thickness.value_nm = nm(pres_cu[i_cu]["thickness_mm"]); i_cu += 1
        elif L.type == board_pb2.BSLT_DIELECTRIC and L.HasField("dielectric") and i_di < len(pres_di):
            src = pres_di[i_di]; i_di += 1
            L.dielectric.type = (board_pb2.BSDT_CORE if src["type"] == "core" else board_pb2.BSDT_PREPREG)
            props = L.dielectric.layer
            if len(props) == 0:
                props.add()
            p = props[0]
            p.thickness.value_nm = nm(src["thickness_mm"])
            p.epsilon_r = src["epsilon_r"]
            p.loss_tangent = src["loss_tangent"]
            p.material_name = src.get("material", "")
            if preset.get("spec_frequency_hz"):
                p.spec_frequency = float(preset["spec_frequency_hz"])
            # collapse multi-sublayer dielectrics into one
            while len(props) > 1:
                del props[-1]
        elif L.type == board_pb2.BSLT_SOLDERMASK and L.HasField("soldermask") and i_sm < len(pres_sm):
            src = pres_sm[i_sm]; i_sm += 1
            L.soldermask.thickness.value_nm = nm(src["thickness_mm"])
            L.soldermask.epsilon_r = src["epsilon_r"]
            L.soldermask.loss_tangent = src["loss_tangent"]
    return su


def apply_stackup(board: Board, stackup: BoardStackup) -> None:
    """Push a stackup back to KiCad via the raw UpdateBoardStackup command."""
    cmd = board_commands_pb2.UpdateBoardStackup()
    cmd.board.CopyFrom(board.document)
    cmd.stackup.CopyFrom(stackup._proto)   # noqa: SLF001 - no public accessor in 0.8.0
    board.client.send(cmd, board_commands_pb2.BoardStackupResponse)
