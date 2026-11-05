"""User-layer management for Z2EMS overlays.

Roles (docs/DESIGN.md section 4):
  ports  -> "Z2EMS.Ports"
  mesh   -> "Z2EMS.Mesh"
  notes  -> "Z2EMS.Notes"
  field  -> "Z2EMS.Field.<tag>" (one per overlay)

KiCad 9/10 offers up to 45 user layers (User.1 .. User.45) that can be renamed,
but kicad-python 0.8.0 has no API to rename a layer (only get_layer_name /
get_layer_by_name). Two strategies are implemented:

  1. `resolve_role_layers`  - find layers that are already named for a role, or
     allocate free *enabled* user layers and record the mapping in setup.json.
     Works without any rename; the user sees "User.7" until renamed.
  2. `rename_layers_via_file` - the documented workaround: save the board, patch
     the `(layers ...)` block of the .kicad_pcb on disk, then `board.revert()`
     so KiCad reloads it with the new user names. Requires user confirmation
     because revert discards unsaved edits (we save first, so none are lost).

Layer colors live in the user's color theme, not in the board file; a Z2EMS
color theme JSON is shipped separately (see `write_color_theme`).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Optional

from kipy.board import Board
from kipy.proto.board.board_types_pb2 import BoardLayer

from .ipc import USER_LAYERS, enabled_user_layers, find_layer_by_name, layer_display_name
from .model import LayerMap

ROLE_NAMES = {
    "ports": "Z2EMS.Ports",
    "mesh": "Z2EMS.Mesh",
    "notes": "Z2EMS.Notes",
}


def field_layer_name(tag: str) -> str:
    return f"Z2EMS.Field.{tag}"


def _enum_name(layer: int) -> str:
    return BoardLayer.Name(layer)


def _enum_value(name: str) -> int:
    return BoardLayer.Value(name)


def resolve_role_layers(board: Board, layer_map: LayerMap, field_tags: List[str] | None = None,
                        allocate: bool = True) -> Dict[str, int]:
    """Return {role: BoardLayer} for ports/mesh/notes and "field:<tag>" entries.

    Order of precedence per role: name already present on the board ->
    mapping recorded in setup.json -> allocate a free enabled user layer.
    Mutates `layer_map` with any new allocation.
    """
    field_tags = field_tags or []
    wanted: Dict[str, str] = dict(ROLE_NAMES)
    for t in field_tags:
        wanted[f"field:{t}"] = field_layer_name(t)

    result: Dict[str, int] = {}
    used: set[int] = set()

    # 1. layers already carrying the Z2EMS name
    for role, name in wanted.items():
        l = find_layer_by_name(board, name)
        if l is not None:
            result[role] = l
            used.add(l)

    # 2. mapping remembered in setup.json
    def recorded(role: str) -> Optional[str]:
        if role.startswith("field:"):
            return layer_map.fields.get(role.split(":", 1)[1])
        return getattr(layer_map, role, None)

    for role in wanted:
        if role in result:
            continue
        rec = recorded(role)
        if rec:
            try:
                l = _enum_value(rec)
            except ValueError:
                continue
            if l not in used:
                result[role] = l
                used.add(l)

    # 3. allocate from free enabled user layers
    if allocate:
        free = [l for l in enabled_user_layers(board)
                if l not in used and layer_display_name(board, l) == _canonical_user_name(l)]
        for role in wanted:
            if role in result:
                continue
            if not free:
                break
            l = free.pop(0)
            result[role] = l
            used.add(l)

    # record back
    for role, l in result.items():
        if role.startswith("field:"):
            layer_map.fields[role.split(":", 1)[1]] = _enum_name(l)
        else:
            setattr(layer_map, role, _enum_name(l))
    return result


def _canonical_user_name(layer: int) -> str:
    """'BL_User_7' -> 'User.7' (the name KiCad shows when not renamed)."""
    n = _enum_name(layer)
    m = re.match(r"BL_User_(\d+)$", n)
    return f"User.{m.group(1)}" if m else n


def ensure_user_layers(board: Board, needed: int) -> List[int]:
    """Enable additional User.N layers (lowest free numbers) so that at least
    `needed` user layers are enabled. Uses Board.set_enabled_layers, which only
    *adds* layers here (existing enabled layers are passed through unchanged),
    so no content is lost. Returns the enabled user layers afterwards.

    Spike 0 (KiCad 10.0.6): items created on a layer that is not enabled are
    silently dropped (create_items returns items with empty ids), so this must
    run before any overlay is drawn."""
    enabled = list(board.get_enabled_layers())
    have = [l for l in USER_LAYERS if l in enabled]
    if len(have) >= needed:
        return have
    copper = board.get_copper_layer_count()
    to_add = [l for l in USER_LAYERS if l not in enabled][: needed - len(have)]
    non_copper = [l for l in enabled if not _is_copper(l)] + to_add
    board.set_enabled_layers(copper, non_copper)
    return [l for l in USER_LAYERS if l in board.get_enabled_layers()]


def _is_copper(layer: int) -> bool:
    n = _enum_name(layer)
    return n in ("BL_F_Cu", "BL_B_Cu") or n.startswith("BL_In")


def missing_user_layers(board: Board, needed: int) -> int:
    """How many more user layers must be enabled in Board Setup to satisfy `needed`."""
    free = len(enabled_user_layers(board))
    return max(0, needed - free)


# --- rename workaround (file patch + revert) ----------------------------------

_LAYER_LINE = re.compile(r'\(\s*(\d+)\s+"(User\.\d+)"\s+user(?:\s+"([^"]*)")?\s*\)')


def patch_layer_names(pcb_text: str, renames: Dict[str, str]) -> str:
    """Rewrite the `(layers ...)` block: renames maps canonical 'User.N' -> new user name.
    Pure function so it can be unit-tested."""

    def repl(m: re.Match) -> str:
        lid, canon, _old = m.group(1), m.group(2), m.group(3)
        if canon in renames:
            return f'({lid} "{canon}" user "{renames[canon]}")'
        return m.group(0)

    return _LAYER_LINE.sub(repl, pcb_text)


def rename_layers_via_file(board: Board, board_path: Path, renames: Dict[int, str]) -> None:
    """Save, patch the file on disk, revert (reload). `renames` is {BoardLayer: name}."""
    board.save()
    text = board_path.read_text(encoding="utf-8")
    canon_renames = {_canonical_user_name(l): name for l, name in renames.items()}
    new_text = patch_layer_names(text, canon_renames)
    if new_text != text:
        board_path.write_text(new_text, encoding="utf-8")
        board.revert()


# --- color theme ----------------------------------------------------------------

DEFAULT_COLORS = {
    "Z2EMS.Ports": "rgb(255, 64, 160)",
    "Z2EMS.Mesh": "rgba(120, 200, 255, 0.45)",
    "Z2EMS.Notes": "rgb(255, 200, 0)",
}


def write_color_theme(path: Path, role_layers: Dict[str, int], board: Board) -> None:
    """Write a minimal KiCad color-theme fragment for the Z2EMS layers.

    The user merges/activates it under Preferences > PCB Editor > Colors.
    Keys follow KiCad's theme schema (board.user_N)."""
    colors: Dict[str, str] = {}
    for role, l in role_layers.items():
        name = ROLE_NAMES.get(role) or field_layer_name(role.split(":", 1)[1])
        key = _canonical_user_name(l).lower().replace(".", "_")   # "user_7"
        colors[key] = DEFAULT_COLORS.get(name, "rgba(255, 255, 255, 0.5)")
    theme = {"meta": {"name": "Z2EMS overlay", "version": 5}, "board": colors}
    path.write_text(json.dumps(theme, indent=2) + "\n", encoding="utf-8")
