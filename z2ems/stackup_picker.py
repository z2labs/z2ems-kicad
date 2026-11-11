"""`z2ems-stackup` action: pick a fab stackup preset and apply it to the open
board in one click (save -> patch file -> revert, see stackup_file.py).

Uses wxPython, which ships with KiCad's bundled Python. Run from a terminal
for a text fallback: `python z2ems/stackup_picker.py --preset <id>`.
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from z2ems.ipc import connect  # noqa: E402
from z2ems.jlc_import import parse_jlc_table, save_preset  # noqa: E402
from z2ems.stackup import USER_PRESET_DIR, list_presets, read_stackup  # noqa: E402
from z2ems.stackup_file import apply_preset_to_board  # noqa: E402


def preset_table(p: dict) -> str:
    lines = [f"{p['id']}  ({p['fab']}, {p['copper_layers']} copper layers, nominal {p['total_thickness_mm']} mm)",
             p.get("description", ""), ""]
    for L in p["layers"]:
        t = L["type"]
        sub = L.get("sublayers", 1)
        extra = ""
        if t in ("core", "prepreg", "soldermask"):
            extra = f"  er={L.get('epsilon_r')}  tand={L.get('loss_tangent')}"
        name = L.get("name") or L.get("material") or ""
        thick = f"{L['thickness_mm']:.4f} mm" + (f" x{sub}" if sub > 1 else "")
        lines.append(f"  {t:10s} {name:10s} {thick:>14s}{extra}")
    lines += ["", "Source: " + p.get("source", "")]
    return "\n".join(lines)


def current_table(board) -> str:
    rep = read_stackup(board)
    lines = [f"Current board stackup: {len(rep.layers)} entries, total {rep.total_thickness_mm:.3f} mm", ""]
    for l in rep.layers:
        extra = f"  er={l.epsilon_r} tand={l.loss_tangent}" if l.kind == "dielectric" else ""
        lines.append(f"  {l.kind:10s} {l.name:12s} {l.thickness_mm:7.4f} mm{extra}")
    if rep.issues:
        lines += ["", "ISSUES:"] + [f"  - {i}" for i in rep.issues]
    return "\n".join(lines)


def run_gui() -> int:
    import wx

    app = wx.App(False)
    try:
        s = connect()
    except Exception as e:  # noqa: BLE001
        print(f"Cannot connect to KiCad (Preferences > Plugins > Enable KiCad API): {e}", file=sys.stderr, flush=True)
        wx.MessageBox(f"Cannot connect to KiCad (Preferences > Plugins > Enable KiCad API):\n{e}", "Z2Labs EMS", wx.ICON_ERROR | wx.STAY_ON_TOP)
        return 1
    board = s.board
    copper = board.get_copper_layer_count()
    presets = list_presets()

    dlg = wx.Dialog(None, title=f"Z2Labs EMS - fab stackup for {s.board_path.name} ({copper} copper layers)",
                    size=(1180, 660), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER | wx.STAY_ON_TOP)
    root = wx.BoxSizer(wx.VERTICAL)
    top = wx.BoxSizer(wx.HORIZONTAL)

    lst = wx.ListBox(dlg, style=wx.LB_SINGLE)
    mono = wx.Font(9, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL)
    detail = wx.TextCtrl(dlg, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.HSCROLL)
    detail.SetFont(mono)

    def refresh_list(select_id: str | None = None):
        nonlocal presets
        presets = list_presets()
        lst.Clear()
        for p in presets:
            fit = p["copper_layers"] == copper
            lst.Append(("  " if fit else "x ") + f"{p['id']}  - {p['fab']} {p['copper_layers']}L  {p.get('description','')[:70]}")
        if presets:
            idx = 0
            for i, p in enumerate(presets):
                if select_id and p["id"] == select_id:
                    idx = i; break
                if not select_id and p["copper_layers"] == copper:
                    idx = i; break
            lst.SetSelection(idx)
            show(idx)

    def show(idx: int):
        if 0 <= idx < len(presets):
            detail.SetValue(preset_table(presets[idx]) + "\n\n" + "-" * 70 + "\n" + current_table(board))
            apply_btn.Enable(presets[idx]["copper_layers"] == copper)

    lst.Bind(wx.EVT_LISTBOX, lambda e: show(e.GetSelection()))
    top.Add(lst, 1, wx.EXPAND | wx.ALL, 6)
    top.Add(detail, 2, wx.EXPAND | wx.ALL, 6)
    root.Add(top, 1, wx.EXPAND)

    note = wx.StaticText(dlg, label=("Apply = save board, write the (stackup ...) block into the .kicad_pcb, reload in KiCad. "
                                     "A timestamped backup is kept next to the board. 'x' = copper count does not match."))
    root.Add(note, 0, wx.ALL, 6)

    btns = wx.BoxSizer(wx.HORIZONTAL)
    import_btn = wx.Button(dlg, label="Import JLC table text...")
    apply_btn = wx.Button(dlg, label="Apply to board")
    close_btn = wx.Button(dlg, wx.ID_CANCEL, label="Close")
    btns.Add(import_btn, 0, wx.ALL, 6)
    btns.AddStretchSpacer()
    btns.Add(apply_btn, 0, wx.ALL, 6)
    btns.Add(close_btn, 0, wx.ALL, 6)
    root.Add(btns, 0, wx.EXPAND)
    dlg.SetSizer(root)

    def on_import(_):
        ted = wx.TextEntryDialog(dlg, "Paste the rows of the JLCPCB 'Layer Stackup' table\n"
                                      "(e.g. 'Prepreg  2116*1  0.109mm', one row per line):",
                                 "Import JLC stackup", style=wx.TextEntryDialogStyle | wx.TE_MULTILINE)
        ted.SetSize((700, 500))
        if ted.ShowModal() != wx.ID_OK:
            return
        text = ted.GetValue()
        pid = wx.GetTextFromUser("Preset id (as shown in the JLC dialog, e.g. JLC061611-3313):", "Preset id", parent=dlg)
        if not pid:
            return
        try:
            preset = parse_jlc_table(text, pid)
            path = save_preset(preset, USER_PRESET_DIR)
        except Exception as e:  # noqa: BLE001
            wx.MessageBox(f"Import failed:\n{e}", "Z2Labs EMS", wx.ICON_ERROR | wx.STAY_ON_TOP, dlg)
            return
        wx.MessageBox(f"Saved {path}\nReview Dk/Df values in the file if needed.", "Z2Labs EMS", wx.ICON_INFORMATION | wx.STAY_ON_TOP, dlg)
        refresh_list(select_id=pid)

    def on_apply(_):
        p = presets[lst.GetSelection()]
        if wx.MessageBox(f"Apply {p['id']} to {s.board_path.name}?\n\nThe board will be saved first; "
                         f"a backup copy is kept next to it.", "Z2Labs EMS",
                         wx.YES_NO | wx.ICON_QUESTION | wx.STAY_ON_TOP, dlg) != wx.YES:
            return
        try:
            bak = apply_preset_to_board(board, s.board_path, p)
        except Exception:  # noqa: BLE001
            wx.MessageBox("Apply failed:\n" + traceback.format_exc()[-2500:], "Z2Labs EMS", wx.ICON_ERROR | wx.STAY_ON_TOP, dlg)
            return
        wx.MessageBox(f"{p['id']} applied and board reloaded.\nBackup: {bak}", "Z2Labs EMS", wx.ICON_INFORMATION | wx.STAY_ON_TOP, dlg)
        show(lst.GetSelection())

    import_btn.Bind(wx.EVT_BUTTON, on_import)
    apply_btn.Bind(wx.EVT_BUTTON, on_apply)
    refresh_list()
    dlg.ShowModal()
    dlg.Destroy()
    return 0


def run_cli(preset_id: str) -> int:
    from z2ems.stackup import load_preset
    s = connect()
    p = load_preset(preset_id)
    bak = apply_preset_to_board(s.board, s.board_path, p)
    print(f"{p['id']} applied to {s.board_path}; backup {bak}")
    return 0


if __name__ == "__main__":
    if "--preset" in sys.argv:
        sys.exit(run_cli(sys.argv[sys.argv.index("--preset") + 1]))
    if "--list" in sys.argv:
        for p in list_presets():
            print(f"{p['id']:20s} {p['copper_layers']}L  {p.get('description','')}")
        sys.exit(0)
    try:
        sys.exit(run_gui())
    except Exception:  # noqa: BLE001
        if os.name == "nt":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, traceback.format_exc()[-3000:], "Z2Labs EMS - error", 0x10)
        raise
