"""Hub action "Clear overlays": remove the mesh preview and all field overlays and put the
view back to normal (field_overlay switches to high contrast so the maps are visible).

The notices used to say "--clear to remove", which a GUI user cannot do.
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from z2ems import SETUP_FILENAME  # noqa: E402
from z2ems.field_overlay import restore_view  # noqa: E402
from z2ems.ipc import connect  # noqa: E402
from z2ems.layers import resolve_role_layers  # noqa: E402
from z2ems.model import Setup  # noqa: E402
from z2ems.overlays import clear_field_overlays, clear_mesh  # noqa: E402
from z2ems.ui import notify  # noqa: E402


def run_gui() -> int:
    import wx  # noqa: F401  (MessageBox fallback on non-Windows)
    app = wx.App(False)  # noqa: F841
    try:
        s = connect()
    except Exception as e:  # noqa: BLE001
        notify("Z2Labs EMS - error", f"Cannot connect to KiCad: {e}", "error")
        return 1
    board = s.board
    setup = Setup.load(s.sidecar_dir / SETUP_FILENAME)
    try:
        n_mesh = clear_mesh(board)
        roles = resolve_role_layers(board, setup.layers, field_tags=list(setup.layers.fields), allocate=False)
        n_img = clear_field_overlays(board, [l for r, l in roles.items() if r.startswith("field:")])
        restore_view(board)
    except Exception:  # noqa: BLE001
        notify("Z2Labs EMS - error", "Could not clear the overlays:\n" + traceback.format_exc()[-1500:], "error", s.sidecar_dir)
        return 1
    if os.environ.get("Z2EMS_AUTO") != "1":
        notify("Z2Labs EMS - overlays", f"Removed {n_mesh} mesh lines and {n_img} field overlays; normal view restored.",
               "info", s.sidecar_dir)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(run_gui())
    except Exception:  # noqa: BLE001
        if os.name == "nt":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, traceback.format_exc()[-3000:], "Z2Labs EMS - error", 0x10)
        raise
