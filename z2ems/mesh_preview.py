"""`z2ems-mesh` action: draw the FDTD mesh the engine would use on the Z2EMS.Mesh user
layer (DESIGN M3, section 4), so the designer sees cells per trace width before a run.

Flow: ports (as in simulate) -> POST /api/mesh on the z2twin server (the same
`final_mesh()` build() hands to the engine) -> x/y lines as a locked group of
segments on the mesh layer, clipped to the board bounding box; z lines and the
quality figures go into the dialog. Run again to regenerate, `--clear` to remove.
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from z2ems import SETUP_FILENAME  # noqa: E402
from z2ems.config import load as load_config  # noqa: E402
from z2ems.ipc import connect, mm  # noqa: E402
from z2ems.layers import ROLE_NAMES, ensure_user_layers, resolve_role_layers  # noqa: E402
from z2ems.model import Setup  # noqa: E402
from z2ems.overlays import clear_mesh, draw_mesh  # noqa: E402
from z2ems.ports import detect_ground_nets  # noqa: E402
from z2ems.simulate import (BIG_RUN_MIN, api, default_threads, fmt_minutes, port_specs,  # noqa: E402
                            run_minutes, server_for)
from z2ems.ui import notify  # noqa: E402


def pad_positions(board) -> dict:
    """{(ref, pad_number): (x_mm, y_mm)} in KiCad board coordinates (y down)."""
    out = {}
    for fp in board.get_footprints():
        ref = fp.reference_field.text.value
        for pd in fp.definition.pads:
            out[(ref, pd.number)] = (mm(pd.position.x), mm(pd.position.y))
    return out


def frame_flip(mesh: dict, pads: dict) -> bool:
    """True when the engine's y axis points up (y = -y_kicad). Decided from the port planes:
    a port plane sits inside its pad, so its y matches either y_kicad or -y_kicad within a pad."""
    same = flip = 0
    for p in mesh.get("ports", []):
        k = next((k for k in pads if k[0] == p.get("ref")), None)
        if k is None:
            continue
        y = 0.5 * (p["start"][1] + p["stop"][1])
        yk = pads[k][1]
        if abs(y - yk) < 2.0:
            same += 1
        elif abs(y + yk) < 2.0:
            flip += 1
    return flip > same


def to_kicad(mesh: dict, flip: bool) -> tuple[list, list, tuple]:
    x = list(mesh["x"])
    bx0, by0, bx1, by1 = mesh["bbox"]
    if flip:
        y = sorted(-v for v in mesh["y"])
        by0, by1 = -by1, -by0
    else:
        y = list(mesh["y"])
    # draw only the lines over the board (the air/PML padding would dwarf the board)
    pad = 0.5
    xs = [v for v in x if bx0 - pad <= v <= bx1 + pad]
    ys = [v for v in y if by0 - pad <= v <= by1 + pad]
    return xs, ys, (bx0, by0, bx1, by1)


def quality_text(mesh: dict, nx: int, ny: int) -> str:
    q = mesh.get("quality", {})
    z = mesh.get("z", [])
    lines = [f"Mesh: {len(mesh['x'])} x {len(mesh['y'])} x {len(z)} lines "
             f"({q.get('cells', 0) / 1e6:.2f} MCells incl. air/PML); drawn on the board: {nx} x {ny}",
             f"Cell size: min {q.get('min_mm')} mm, max {q.get('max_mm')} mm, resolution {mesh.get('res_mm', 0):.3f} mm",
             f"Grading {q.get('grading')}, aspect {q.get('aspect')}, {q.get('cells_per_wavelength')} cells/wavelength -> {q.get('grade', '?')}"]
    lz = mesh.get("layers_z", [])
    if lz:
        lines.append("Copper z (mm): " + ", ".join(f"{d['layer']} {d['z']:.4f}" for d in lz))
    return "\n".join(lines)


# Pixels across the board at full-board zoom on a laptop screen (1366x768: the
# 180 mm array filled ~500 px). Denser lines merge into a uniform tint there.
FIT_PIXELS = 500


def size_text(mesh: dict, n_ports: int, bbox_mm, nx: int, ny: int, threads: int) -> str:
    """Run-time estimate and a readability hint for the drawn lines (GUI test:
    the 563 MCell mesh of a 180 mm 8-layer board looked like "nothing drawn" - 4974 lines at
    2.8 px/mm are a plain tint - and nothing said that this mesh is days of compute)."""
    nz = len(mesh.get("z", [])) or 1
    mc = len(mesh["x"]) * len(mesh["y"]) * nz / 1e6
    minutes = run_minutes(mc, n_ports, threads)
    out = [f"Simulation of this mesh: {mc:.1f} MCells x {n_ports} excitations, "
           f"roughly {fmt_minutes(minutes)} on {threads} threads here."]
    if minutes > BIG_RUN_MIN:
        out.append("WARNING: too big for this machine - coarsen the mesh, lower fmax, "
                   "use fewer ports or simulate a cut-out of the RF section.")
    w = max(bbox_mm[2] - bbox_mm[0], bbox_mm[3] - bbox_mm[1], 1e-9)
    if max(nx, ny) / FIT_PIXELS > 0.5:          # more than one line per two screen pixels
        out.append(f"At full-board zoom the lines are denser than the screen ({max(nx, ny) / w:.0f} lines/mm) "
                   "and show only as a tint: zoom in to see the cells.")
    return "\n".join(out)


def run_gui(clear_only: bool = False) -> int:
    import wx
    app = wx.App(False)
    try:
        s = connect()
    except Exception as e:  # noqa: BLE001
        notify("Z2Labs EMS - error", f"Cannot connect to KiCad: {e}", "error")
        return 1
    board = s.board
    setup = Setup.load(s.sidecar_dir / SETUP_FILENAME)
    if clear_only:
        n = clear_mesh(board)
        if os.environ.get("Z2EMS_AUTO") != "1":     # harness: a modal box here blocked the soak (TimeoutExpired)
            notify("Z2Labs EMS - mesh", f"Removed {n} mesh lines.", "info", s.sidecar_dir)
        return 0
    from z2ems.engine_install import ensure_engine
    if not ensure_engine("The mesh preview needs the Z2 EMS engine."):
        return 1
    cfg = load_config()
    ground = setup.nets.ground or detect_ground_nets(board)[:1]
    specs, how = port_specs(board, ground)
    if not specs:
        notify("Z2Labs EMS - error", "No ports: place Z2EMS_PORT footprints or add an RF connector.", "error", s.sidecar_dir)
        return 1
    log_lines: list[str] = []
    try:
        base = server_for(cfg, s.board_path, log_lines.append)
        mesh = api(base, "/api/mesh", dict(path=str(s.board_path), ports=specs, fmax=setup.sim.f_stop_hz,
                                           materials=setup.materials()), timeout=300)
    except RuntimeError as e:          # engine said no (e.g. no reference copper under a connector pad)
        txt = (f"The engine could not build ports for this board:\n{e}\n\nPorts tried ({how}): "
               + ", ".join(p["ref"] for p in specs)
               + "\n\nPlace Z2EMS_PORT footprints (SIG on the trace end, REF on ground) where the ports should be.")
        notify("Z2Labs EMS - error", txt, "error", s.sidecar_dir)
        return 1
    except Exception:  # noqa: BLE001
        notify("Z2Labs EMS - error", "Could not get the mesh:\n" + traceback.format_exc()[-1500:], "error", s.sidecar_dir)
        return 1
    flip = frame_flip(mesh, pad_positions(board))
    xs, ys, bbox = to_kicad(mesh, flip)
    ensure_user_layers(board, len(ROLE_NAMES) + 1)
    roles = resolve_role_layers(board, setup.layers)
    layer = roles.get("mesh")
    if layer is None:
        notify("Z2Labs EMS - error", "No free user layer for the mesh (enable one in Board Setup > Layers).", "error", s.sidecar_dir)
        return 1
    draw_mesh(board, layer, xs, ys, bbox)
    setup.save(s.sidecar_dir / SETUP_FILENAME)
    txt = (f"{s.board_path.name}: ports ({how}) " + ", ".join(p["ref"] for p in specs) +
           f", fmax {setup.sim.f_stop_hz / 1e9:.2f} GHz\n\n" + quality_text(mesh, len(xs), len(ys)) +
           "\n\n" + size_text(mesh, len(specs), bbox, len(xs), len(ys), default_threads()) +
           f"\n\nLines are a locked group on the mesh layer{' (engine y axis mirrored to KiCad)' if flip else ''}. "
           "Run again to regenerate; the group is removed before a new one is drawn.")
    notify("Z2Labs EMS - mesh preview", txt, "info", s.sidecar_dir)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(run_gui(clear_only="--clear" in sys.argv))
    except Exception:  # noqa: BLE001
        if os.name == "nt":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, traceback.format_exc()[-3000:], "Z2Labs EMS - error", 0x10)
        raise
