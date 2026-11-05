"""Write a stackup preset into a .kicad_pcb file (the file-patch route).

KiCad 10.0.6 has no IPC handler for UpdateBoardStackup (Spike 0), so presets are
applied by rewriting the `(stackup ...)` block inside `(setup ...)` and letting
KiCad reload the file (`board.revert()` or File > Revert). Pure functions here
are KiCad-independent and unit-tested; `apply_preset_to_board` does the
save -> patch -> revert dance through IPC.

KiCad stackup syntax (KiCad 7..10):
  (stackup
    (layer "F.SilkS" (type "Top Silk Screen"))
    (layer "F.Paste" (type "Top Solder Paste"))
    (layer "F.Mask" (type "Top Solder Mask") (thickness 0.01))
    (layer "F.Cu" (type "copper") (thickness 0.035))
    (layer "dielectric 1" (type "prepreg") (thickness 0.1) (material "FR4") (epsilon_r 4.5) (loss_tangent 0.02)
      addsublayer (thickness 0.1) (material "FR4") (epsilon_r 4.5) (loss_tangent 0.02))
    ...
    (copper_finish "None")
    (dielectric_constraints no)
  )
"""

from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import List, Optional

COPPER_NAMES = ["F.Cu"] + [f"In{i}.Cu" for i in range(1, 31)] + ["B.Cu"]


def copper_layer_names(n: int) -> List[str]:
    if n < 2 or n % 2:
        raise ValueError("copper layer count must be even and >= 2")
    return ["F.Cu"] + [f"In{i}.Cu" for i in range(1, n - 1)] + ["B.Cu"]


def _f(x: float) -> str:
    s = f"{x:.6f}".rstrip("0").rstrip(".")
    return s if s else "0"


def render_stackup_block(preset: dict, copper_finish: str = "None",
                         dielectric_constraints: str = "no", extra_tail: str = "") -> str:
    """Render a complete `(stackup ...)` block from a preset dict (presets/stackups/*.json)."""
    n = preset["copper_layers"]
    names = copper_layer_names(n)
    cu = iter(names)
    out = ["\t\t(stackup",
           '\t\t\t(layer "F.SilkS" (type "Top Silk Screen"))',
           '\t\t\t(layer "F.Paste" (type "Top Solder Paste"))']
    sm = [l for l in preset["layers"] if l["type"] == "soldermask"]
    top_sm = sm[0] if sm else {"thickness_mm": 0.01}
    bot_sm = sm[-1] if sm else {"thickness_mm": 0.01}
    out.append(f'\t\t\t(layer "F.Mask" (type "Top Solder Mask") (thickness {_f(top_sm["thickness_mm"])}))')
    di = 0
    for L in preset["layers"]:
        t = L["type"]
        if t == "copper":
            out.append(f'\t\t\t(layer "{next(cu)}" (type "copper") (thickness {_f(L["thickness_mm"])}))')
        elif t in ("core", "prepreg"):
            di += 1
            sub = int(L.get("sublayers", 1))
            one = (f'(thickness {_f(L["thickness_mm"])}) (material "{L.get("material", "FR4")}") '
                   f'(epsilon_r {_f(L["epsilon_r"])}) (loss_tangent {_f(L["loss_tangent"])})')
            line = f'\t\t\t(layer "dielectric {di}" (type "{t}") {one}'
            for _ in range(sub - 1):
                line += f"\n\t\t\t\taddsublayer {one}"
            out.append(line + ")")
    out.append(f'\t\t\t(layer "B.Mask" (type "Bottom Solder Mask") (thickness {_f(bot_sm["thickness_mm"])}))')
    out.append('\t\t\t(layer "B.Paste" (type "Bottom Solder Paste"))')
    out.append('\t\t\t(layer "B.SilkS" (type "Bottom Silk Screen"))')
    out.append(f'\t\t\t(copper_finish "{copper_finish}")')
    out.append(f"\t\t\t(dielectric_constraints {dielectric_constraints})")
    if extra_tail:
        out.append(extra_tail)
    out.append("\t\t)")
    return "\n".join(out)


def find_block(text: str, start: int) -> int:
    """Index just past the matching ')' for the '(' at `start` (string-aware)."""
    assert text[start] == "("
    depth = 0
    instr = False
    i = start
    while i < len(text):
        ch = text[i]
        if instr:
            if ch == "\\":
                i += 1
            elif ch == '"':
                instr = False
        elif ch == '"':
            instr = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ValueError("unbalanced s-expression")


_STACKUP_RE = re.compile(r"\(stackup\b")
_SETUP_RE = re.compile(r"\(setup\b")


def existing_stackup_tail(block: str) -> tuple[str, str, str]:
    """Pull copper_finish, dielectric_constraints and any other non-layer entries out of an existing block."""
    finish = "None"
    constraints = "no"
    tail_lines: List[str] = []
    m = re.search(r'\(copper_finish\s+"([^"]*)"\)', block)
    if m:
        finish = m.group(1)
    m = re.search(r"\(dielectric_constraints\s+(\w+)\)", block)
    if m:
        constraints = m.group(1)
    for key in ("edge_connector", "castellated_pads", "edge_plating"):
        m = re.search(rf"\({key}\s+[^)]*\)", block)
        if m:
            tail_lines.append("\t\t\t" + m.group(0))
    return finish, constraints, "\n".join(tail_lines)


def patch_stackup(pcb_text: str, preset: dict) -> str:
    """Return the board text with the stackup block replaced (or inserted into setup)."""
    m = _STACKUP_RE.search(pcb_text)
    if m:
        end = find_block(pcb_text, m.start())
        old = pcb_text[m.start():end]
        finish, constraints, tail = existing_stackup_tail(old)
        new = render_stackup_block(preset, finish, constraints, tail).lstrip("\t")
        return pcb_text[:m.start()] + new + pcb_text[end:]
    ms = _SETUP_RE.search(pcb_text)
    if not ms:
        raise ValueError("no (setup ...) block in board file")
    insert_at = ms.end()
    new = "\n" + render_stackup_block(preset)
    return pcb_text[:insert_at] + new + pcb_text[insert_at:]


def board_copper_count(pcb_text: str) -> int:
    m = _STACKUP_RE.search(pcb_text)
    names = set(re.findall(r'\(\d+\s+"([^"]+\.Cu)"\s+(?:signal|power|mixed|jumper)', pcb_text))
    return len(names)


def apply_preset_to_file(board_path: Path, preset: dict, backup: bool = True) -> Path:
    text = board_path.read_text(encoding="utf-8")
    n = board_copper_count(text)
    if n and n != preset["copper_layers"]:
        raise ValueError(f"board has {n} copper layers, preset '{preset['id']}' has {preset['copper_layers']}")
    new = patch_stackup(text, preset)
    bak: Optional[Path] = None
    if backup:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        bak = board_path.with_name(f"{board_path.name}.bak-{stamp}")
        shutil.copy2(board_path, bak)
    board_path.write_text(new, encoding="utf-8")
    return bak or board_path


def apply_preset_to_board(board, board_path: Path, preset: dict) -> Path:
    """IPC flow: save the live board, patch the file, revert so KiCad reloads it.
    Returns the backup path."""
    board.save()
    bak = apply_preset_to_file(board_path, preset)
    board.revert()
    return bak


if __name__ == "__main__":
    import argparse
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from z2ems.stackup import load_preset, list_presets
    ap = argparse.ArgumentParser(description="Apply a fab stackup preset to the open board (or a file)")
    ap.add_argument("--preset", required=False)
    ap.add_argument("--file", type=Path, help="patch this .kicad_pcb directly instead of the open board")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    if a.list or not a.preset:
        for p in list_presets():
            print(f"{p['id']:18s} {p['copper_layers']}L  {p['description']}")
        sys.exit(0)
    preset = load_preset(a.preset)
    if a.file:
        print("backup:", apply_preset_to_file(a.file, preset))
    else:
        from z2ems.ipc import connect
        s = connect()
        print("backup:", apply_preset_to_board(s.board, s.board_path, preset))
        print("board reloaded with", preset["id"])
