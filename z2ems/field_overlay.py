"""`z2ems-fields` action: put the current-density maps of the last run on the board
as reference images on `Z2EMS.Field.<layer>_<MHz>` user layers (DESIGN M4, section 4.1).

The engine writes one PNG per copper layer and frequency into
<board>_twin/<run>/fields/ (board coordinates, row 0 = top, bbox in result.json).
Here each map gets an alpha channel (opacity baked in, dark background fades
out), is mirrored to KiCad's y-down frame when the engine frame is mirrored, and
is placed as a locked reference image spanning exactly the board bounding box.
Run again to replace the overlays, `--clear` to remove them.

Z2EMS_AUTO=1: no dialog, the middle frequency of every layer.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from z2ems import SETUP_FILENAME  # noqa: E402
from z2ems.ipc import connect  # noqa: E402
from z2ems.layers import ROLE_NAMES, ensure_user_layers, resolve_role_layers  # noqa: E402
from z2ems.mesh_preview import frame_flip, pad_positions  # noqa: E402
from z2ems.model import Setup  # noqa: E402
from z2ems.overlays import clear_field_overlays, place_field_overlay  # noqa: E402
from z2ems.png import heatmap_with_alpha  # noqa: E402
from z2ems.ui import notify  # noqa: E402


def latest_run(board_path: Path) -> tuple[Path, dict] | None:
    runs = sorted((board_path.parent / f"{board_path.stem}_twin").glob("*/result.json"))
    for rj in reversed(runs):
        try:
            j = json.loads(rj.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if j.get("fields") and j["fields"].get("maps"):
            return rj.parent, j
    return None


def map_items(run_dir: Path, res: dict) -> list[dict]:
    """[{layer, f_GHz, mhz, png, peak_db}] for every map that exists on disk."""
    f = res["fields"]
    out = []
    for layer, paths in f["maps"].items():
        for k, rel in enumerate(paths):
            p = run_dir / Path(rel).name if not (run_dir / rel).exists() else run_dir / rel
            if not p.exists():
                p = run_dir / "fields" / Path(rel).name
            if p.exists():
                fg = f["f_GHz"][k] if k < len(f.get("f_GHz", [])) else 0.0
                out.append(dict(layer=layer, f_GHz=fg, mhz=int(round(fg * 1000)), png=p,
                                peak_db=(f.get("peak_db", {}).get(layer) or [None] * (k + 1))[k]))
    return out


def tag_for(item: dict) -> str:
    return f"{item['layer'].replace('.', '_')}_{item['mhz']}MHz"


def layer_label(board, layer: int) -> str:
    """The name KiCad shows in its layer selector ('User.5'), not the enum ('BL_User_5')."""
    try:
        return board.get_layer_name(layer)
    except Exception:  # noqa: BLE001
        from z2ems.layers import _canonical_user_name
        return _canonical_user_name(layer)


def strongest_inner(chosen: list[dict]) -> str:
    """Copper layer whose map is the most telling on its own: the first inner layer (return
    current spreading in the reference plane), else the first chosen layer."""
    inner = [i["layer"] for i in chosen if i["layer"].startswith("In")]
    return inner[0] if inner else chosen[0]["layer"]


def show_overlay(board, layer: int, dim: bool = False) -> str:
    """KiCad draws reference images UNDER all copper, so a map behind a filled ground zone is
    invisible (on the coupon the overlays were placed but the In1 GND
    fill covered them; visible only with copper hidden). Make the overlay layer active and
    switch high-contrast to 'hide other layers' - the one view that shows the map."""
    try:
        from kipy.proto.board import board_commands_pb2 as bc
        board.set_active_layer(layer)
        ap = board.get_editor_appearance_settings()
        ap.inactive_layer_display = bc.ILDM_DIMMED if dim else bc.ILDM_HIDDEN
        board.set_editor_appearance_settings(ap)
    except Exception as e:  # noqa: BLE001
        return (f"(Could not switch the view: {e}. Hide the copper layers in Appearance > Layers - "
                "KiCad draws reference images under the copper.)\n\n")
    if dim:
        return (f"View: layer {layer_label(board, layer)} is active and the other layers are dimmed (high contrast), "
                "so the map shows through the copper. Pick another field layer in the layer selector for its "
                "map; Ctrl+H cycles the high-contrast mode back to normal.\n\n")
    return (f"View: layer {layer_label(board, layer)} is active and the other layers are hidden (high contrast), "
            "because KiCad draws reference images under the copper. Pick another field layer in the "
            "layer selector to see its map; Ctrl+H cycles the high-contrast mode back to normal.\n\n")


VIEW_SAVE = "view_before_fields.json"


def show_field_layers(board, active: int, keep: list, save_dir) -> str:
    """Show a field map: KiCad draws reference images under all copper (even in dimmed high
    contrast the copper and the filled ground zones cover them), so only the field layer, the
    board outline and the port markers stay visible. The layers that were visible before are
    saved next to the board and come back with Clear overlays."""
    import json
    from pathlib import Path
    from kipy.proto.board import board_commands_pb2 as bc
    from kipy.proto.board.board_types_pb2 import BoardLayer
    save = Path(save_dir) / VIEW_SAVE
    try:
        if not save.exists():
            save.write_text(json.dumps({"visible": [int(l) for l in board.get_visible_layers()],
                                        "active": int(board.get_active_layer())}))
        board.set_visible_layers(sorted({int(active), int(BoardLayer.BL_Edge_Cuts), *[int(k) for k in keep]}))
        board.set_active_layer(active)
        ap = board.get_editor_appearance_settings()
        ap.inactive_layer_display = bc.ILDM_NORMAL
        board.set_editor_appearance_settings(ap)
    except Exception as e:  # noqa: BLE001
        return (f"(Could not switch the view: {e}. Hide the copper layers in Appearance > Layers - "
                "KiCad draws reference images under the copper.)")
    return (f"View: only {layer_label(board, active)}, the board outline and the port markers are shown, "
            "because KiCad draws pictures under the copper. Tick the other field layers (the user layers the plugin named in the notice) in "
            "Appearance > Layers to compare; Clear overlays brings the copper layers back. "
            "If the colours look pale, raise Appearance > Objects > Images (the slider sets the opacity of pictures).")


def restore_view(board, save_dir=None) -> None:
    import json
    from pathlib import Path
    try:
        from kipy.proto.board import board_commands_pb2 as bc
        from kipy.proto.board.board_types_pb2 import BoardLayer
        ap = board.get_editor_appearance_settings()
        ap.inactive_layer_display = bc.ILDM_NORMAL
        board.set_editor_appearance_settings(ap)
        save = Path(save_dir) / VIEW_SAVE if save_dir else None
        if save is not None and save.exists():
            v = json.loads(save.read_text())
            board.set_visible_layers(v["visible"])
            board.set_active_layer(v.get("active", BoardLayer.BL_F_Cu))
            save.unlink()
        else:
            board.set_active_layer(BoardLayer.BL_F_Cu)
    except Exception:  # noqa: BLE001
        pass


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
    roles_now = resolve_role_layers(board, setup.layers, field_tags=list(setup.layers.fields), allocate=False)
    field_layers = [l for r, l in roles_now.items() if r.startswith("field:")]
    if clear_only:
        n = clear_field_overlays(board, field_layers)
        restore_view(board)
        if os.environ.get("Z2EMS_AUTO") != "1":
            notify("Z2Labs EMS - fields", f"Removed {n} field overlays.", "info", s.sidecar_dir)
        return 0
    found = latest_run(s.board_path)
    if not found:
        notify("Z2Labs EMS - error", "No run with field maps under "
               f"{s.board_path.stem}_twin/ - run a simulation first (maps are written for 0.25/0.5/0.9 x fmax).",
               "error", s.sidecar_dir)
        return 1
    run_dir, res = found
    items = map_items(run_dir, res)
    if not items:
        notify("Z2Labs EMS - error", f"Run {run_dir.name} lists maps but the PNG files are missing.", "error", s.sidecar_dir)
        return 1
    auto = os.environ.get("Z2EMS_AUTO") == "1"
    if auto:
        freqs = sorted({i["f_GHz"] for i in items})
        mid = freqs[len(freqs) // 2]
        chosen = [i for i in items if i["f_GHz"] == mid]
    else:
        labels = [f"{i['layer']} @ {i['f_GHz']:.2f} GHz  (peak {i['peak_db']} dB)" for i in items]
        dlg = wx.MultiChoiceDialog(None, f"Run {run_dir.name}: pick the maps to overlay", "Z2Labs EMS - field overlay", labels)
        dlg.SetWindowStyleFlag(dlg.GetWindowStyleFlag() | wx.STAY_ON_TOP)
        freqs = sorted({i["f_GHz"] for i in items})
        mid = freqs[len(freqs) // 2]
        dlg.SetSelections([k for k, i in enumerate(items) if i["f_GHz"] == mid])
        if dlg.ShowModal() != wx.ID_OK:
            return 0
        chosen = [items[k] for k in dlg.GetSelections()]
        dlg.Destroy()
    if not chosen:
        return 0
    try:
        flip = frame_flip(dict(ports=res.get("ports", [])), pad_positions(board))
        bx0, by0, bx1, by1 = res["fields"]["bbox"]
        if flip:
            by0, by1 = -by1, -by0
        width_mm = bx1 - bx0
        tags = [tag_for(i) for i in chosen]
        ensure_user_layers(board, len(ROLE_NAMES) + len(set(list(setup.layers.fields) + tags)) + 1)
        roles = resolve_role_layers(board, setup.layers, field_tags=tags)
        clear_field_overlays(board, [l for r, l in roles.items() if r.startswith("field:")])
        placed = []
        shown = None
        for it in chosen:
            layer = roles.get(f"field:{tag_for(it)}")
            if layer is None:
                continue
            data = heatmap_with_alpha(it["png"].read_bytes(), flip_rows=not flip)
            tmp = s.sidecar_dir / f"overlay_{tag_for(it)}.png"
            tmp.write_bytes(data)
            from z2ems.png import decode
            w, _, _, _ = decode(it["png"].read_bytes())
            place_field_overlay(board, layer, tmp, (bx0, by0), width_mm, w)
            placed.append(f"  {it['layer']} @ {it['f_GHz']:.2f} GHz -> layer {layer_label(board, layer)}")
            if shown is None or it["layer"] == strongest_inner(chosen):
                shown = (layer, it)
        setup.save(s.sidecar_dir / SETUP_FILENAME)
        view_note = show_overlay(board, shown[0]) if shown else ""
    except Exception:  # noqa: BLE001
        notify("Z2Labs EMS - error", "Could not place the overlays:\n" + traceback.format_exc()[-1500:], "error", s.sidecar_dir)
        return 1
    notify("Z2Labs EMS - field overlay",
           f"Run {run_dir.name}, |J| in dB relative to the strongest current (range {res['fields'].get('db_range')} dB):\n"
           + "\n".join(placed) + "\n\n" + view_note + "Overlays are locked reference images on their own user "
           "layers. Run again to replace; Z2Labs EMS > Clear overlays removes them and restores the normal view.",
           "info", s.sidecar_dir)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(run_gui(clear_only="--clear" in sys.argv))
    except Exception:  # noqa: BLE001
        if os.name == "nt":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, traceback.format_exc()[-3000:], "Z2Labs EMS - error", 0x10)
        raise
