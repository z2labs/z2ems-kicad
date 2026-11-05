"""Entry point for the `z2ems-open` IPC action.

Milestone 1 scope: no dialog yet. Connects to the open board, reads stackup,
ground nets, ports and suggestions, resolves overlay layers, writes/updates
setup.json in the sidecar folder and prints a status report to stdout (KiCad
shows plugin stdout in its plugin log). The dialog (docs/DESIGN.md section 6) replaces
the print-out later; the logic below stays.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import traceback
from contextlib import redirect_stdout
from pathlib import Path

# allow running from a plugin install dir without pip install
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from z2ems import SETUP_FILENAME  # noqa: E402
from z2ems.ipc import connect  # noqa: E402
from z2ems.layers import ROLE_NAMES, ensure_user_layers, resolve_role_layers  # noqa: E402
from z2ems.model import PortExtra, Setup, board_file_hash  # noqa: E402
from z2ems.ports import detect_ground_nets, read_ports, suggest_ports  # noqa: E402
from z2ems.stackup import list_presets, preset_matches_board, read_stackup  # noqa: E402


def main() -> int:
    s = connect()
    board = s.board
    print(f"Z2Labs EMS - board: {s.board_path}")
    setup_path = s.sidecar_dir / SETUP_FILENAME
    setup = Setup.load(setup_path)

    # stackup
    rep = read_stackup(board)
    print(f"\nStackup: {len(rep.layers)} layers, total {rep.total_thickness_mm:.3f} mm")
    for l in rep.layers:
        extra = f" er={l.epsilon_r} tand={l.loss_tangent}" if l.kind == "dielectric" else ""
        print(f"  {l.kind:10s} {l.name:12s} {l.thickness_mm:7.4f} mm{extra}")
    if rep.issues:
        print("  ISSUES:")
        for i in rep.issues:
            print(f"   - {i}")
        fits = [p["id"] for p in list_presets() if preset_matches_board(p, board)]
        if fits:
            print(f"  Presets matching {board.get_copper_layer_count()} copper layers: {', '.join(fits)}")
    setup.stackup.issues = rep.issues
    setup.stackup.validated = rep.ok

    # ground
    gnd = detect_ground_nets(board)
    if not setup.nets.ground:
        setup.nets.ground = gnd[:1]
    print(f"\nGround nets detected: {gnd or 'none'}  (using {setup.nets.ground})")

    # ports
    ports = read_ports(board)
    print(f"\nPorts ({len(ports)}):")
    for p in ports:
        flag = " !! " + "; ".join(p.issues) if p.issues else ""
        print(f"  {p.reference:4s} {p.type:6s} Z0={p.z0:g} excite={p.excite} "
              f"sig={p.sig_net or '-'} ref={p.ref_net or '-'} @({p.x_mm:.2f},{p.y_mm:.2f}){flag}")
        extra = setup.ports.get(p.uuid) or PortExtra()
        extra.last_seen = {"ref": p.reference, "sig": p.sig_net, "refnet": p.ref_net}
        setup.ports[p.uuid] = extra
    orphans = setup.orphaned_ports([p.uuid for p in ports])
    if orphans:
        print(f"  orphaned port settings (footprint gone): {orphans}")
    if not setup.sim.excite:
        setup.sim.excite = [p.reference for p in ports if p.excite]

    sugg = suggest_ports(board, setup.nets.ground)
    if sugg:
        conn = [sg for sg in sugg if sg.footprint_ref]
        byclass: dict[str, list] = {}
        for sg in sugg:
            if not sg.footprint_ref:
                byclass.setdefault(sg.reason, []).append(sg.net)
        print(f"\nPort suggestions ({len(conn)} connectors), not applied:")
        for sg in conn[:40]:
            print(f"  [{sg.score:.1f}] {sg.reason}: net {sg.net} @({sg.x_mm:.2f},{sg.y_mm:.2f}) {sg.layer}")
        for reason, nets in byclass.items():
            print(f"  [0.5] {reason}: {len(nets)} nets (candidates for internal ports, e.g. {', '.join(nets[:3])})")

    # overlay layers (enable user layers if the board has too few)
    ensure_user_layers(board, len(ROLE_NAMES) + 1)
    roles = resolve_role_layers(board, setup.layers)
    print("\nOverlay layers:")
    for role, name in ROLE_NAMES.items():
        l = roles.get(role)
        print(f"  {name:14s} -> {board.get_layer_name(l) if l is not None else 'NONE (enable more user layers in Board Setup)'}")

    setup.board_hash = board_file_hash(s.board_path) if s.board_path.exists() else None
    setup.save(setup_path)
    print(f"\nsetup.json written: {setup_path}")
    return 0


def _notify(title: str, text: str, error: bool = False) -> None:
    """Minimal UI until the real dialog exists: native message box per OS, else stderr."""
    if os.name == "nt":
        import ctypes
        flags = 0x10 if error else 0x40   # MB_ICONERROR / MB_ICONINFORMATION
        ctypes.windll.user32.MessageBoxW(None, text, title, flags)
    elif sys.platform == "darwin":
        icon = "stop" if error else "note"
        body = text.replace("\\", "\\\\").replace('"', '\\"')
        subprocess.run(["osascript", "-e",
                        f'display dialog "{body}" with title "{title}" buttons {{"OK"}} default button 1 with icon {icon}'],
                       check=False)
    else:
        try:
            subprocess.run(["zenity", "--info" if not error else "--error", "--title", title, "--text", text,
                            "--width", "600"], check=False)
        except FileNotFoundError:
            print(text, file=sys.stderr)


def run_as_plugin() -> int:
    """Launched by KiCad (pythonw, no console): capture the report, save it in the
    sidecar folder, open it in the default text editor and pop a short summary."""
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            rc = main()
    except Exception as e:  # noqa: BLE001
        from z2ems.ipc import NotReadyError, _is_busy
        if isinstance(e, NotReadyError) or _is_busy(e):
            _notify("Z2Labs EMS", "KiCad is still loading the board or is busy (zone fill, "
                    "open dialog). Wait a few seconds and run Z2Labs EMS again.\n\n"
                    f"({str(e)[:300]})", error=True)
            return 2
        msg = buf.getvalue() + "\n" + traceback.format_exc()
        _notify("Z2Labs EMS - error", msg[-3000:], error=True)
        return 1
    report = buf.getvalue()
    lines = [l for l in report.splitlines() if l.startswith(("Ports (", "Ground nets", "Port suggestions", "setup.json"))]
    stack = [l for l in report.splitlines() if l.startswith("Stackup:")]
    issues = "  ISSUES:" in report
    summary = "\n".join(stack + lines) + ("\n\nStackup has issues - see report." if issues else "")
    report_path = None
    try:
        from z2ems.ipc import connect
        report_path = connect().sidecar_dir / "last_report.txt"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report, encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    if not hub(summary or report[-1500:], report_path):
        _notify("Z2Labs EMS", summary or report[-1500:])
    return rc


# Actions reachable from the hub. Only three of them have toolbar buttons: on a 1366 px
# laptop screen KiCad drops toolbar buttons that do not fit, without an overflow chevron
# (on a 1366 px wide screen open/stackup/simulate were invisible with five buttons).
HUB_ACTIONS = (("&Fab stackup...", "stackup_picker.py"), ("&Mesh preview", "mesh_preview.py"),
               ("&Simulate...", "simulate.py"), ("F&ield overlay", "field_overlay.py"),
               ("Clear &overlays", "clear_overlays.py"), ("&Engine...", "engine_setup.py"))


def hub(summary: str, report_path) -> bool:
    """Z2Labs EMS hub dialog: the board summary plus one button per action. A button starts
    the action in a new process of this same interpreter (KiCad's plugin venv, same API
    socket/token environment) and closes the hub. Returns False when wx is unavailable."""
    try:
        import wx
    except ImportError:
        return False
    app = wx.App(False)  # noqa: F841
    dlg = wx.Dialog(None, title="Z2Labs EMS", style=wx.DEFAULT_DIALOG_STYLE | wx.STAY_ON_TOP)
    root = wx.BoxSizer(wx.VERTICAL)
    txt = wx.StaticText(dlg, label=summary)
    root.Add(txt, 0, wx.ALL, 12)
    row = wx.BoxSizer(wx.HORIZONTAL)
    here = Path(__file__).resolve().parent
    chosen = {}
    for label, script in HUB_ACTIONS:
        b = wx.Button(dlg, label=label)
        def on(evt, script=script):
            chosen["script"] = script
            dlg.EndModal(wx.ID_OK)
        b.Bind(wx.EVT_BUTTON, on)
        row.Add(b, 0, wx.RIGHT, 6)
    root.Add(row, 0, wx.LEFT | wx.RIGHT, 12)
    row2 = wx.BoxSizer(wx.HORIZONTAL)
    if report_path is not None:
        rb = wx.Button(dlg, label="Full &report")
        def on_report(evt):
            if os.name == "nt":
                os.startfile(report_path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-t", str(report_path)])
            else:
                subprocess.Popen(["xdg-open", str(report_path)])
        rb.Bind(wx.EVT_BUTTON, on_report)
        row2.Add(rb, 0, wx.RIGHT, 6)
    cb = wx.Button(dlg, wx.ID_CANCEL, "Close")
    row2.Add(cb, 0)
    root.Add(row2, 0, wx.ALL | wx.ALIGN_RIGHT, 12)
    dlg.SetSizerAndFit(root)
    dlg.CentreOnScreen()
    dlg.Raise()
    dlg.ShowModal()
    dlg.Destroy()
    if chosen.get("script"):
        kw = {"creationflags": 0x08000000} if os.name == "nt" else {"start_new_session": True}
        subprocess.Popen([sys.executable, str(here / chosen["script"])], stdin=subprocess.DEVNULL, **kw)
    return True


if __name__ == "__main__":
    # KiCad runs plugins with pythonw (no stdout); a terminal run keeps plain printing.
    # Set Z2EMS_CLI=1 (or pass --cli) to force plain printing when piping from a terminal.
    cli = "--cli" in sys.argv or os.environ.get("Z2EMS_CLI") == "1"
    if not cli and (sys.stdout is None or not sys.stdout.isatty()):
        sys.exit(run_as_plugin())
    sys.exit(main())
