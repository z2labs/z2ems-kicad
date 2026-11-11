"""The Z2Labs EMS window: ports, board, simulation in one place.

A modeless, always-on-top window next to the PCB editor (the plugin is its own process, so KiCad stays
usable: select a pad or a connector there, then click here). Ports are explicit: a + pad and a - reference
(ground pads or the plane below); nothing is simulated with a port the user did not create or tick.
Runs go to the local engine server and continue when this window is closed; the window follows a run with
a progress bar and opens the result and the 3D viewer when it is done.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import wx  # noqa: E402

from z2ems import SETUP_FILENAME, __version__  # noqa: E402
from z2ems import port_board as pb  # noqa: E402
from z2ems.config import load as load_config  # noqa: E402
from z2ems.ipc import connect, connect_error_text  # noqa: E402
from z2ems.layers import ROLE_NAMES, ensure_user_layers, resolve_role_layers  # noqa: E402
from z2ems.model import Setup  # noqa: E402
from z2ems.portdefs import PortDef, renumber, runnable  # noqa: E402
from z2ems.ports import detect_ground_nets, suggest_ports  # noqa: E402

HERE = Path(__file__).resolve().parent
PINK = wx.Colour(255, 64, 160)
OK_GREEN = wx.Colour(22, 140, 60)
WARN = wx.Colour(200, 110, 0)
GREY = wx.Colour(110, 115, 125)
POLL_S = 2.0
POLL_FAILS_MAX = 90            # ~3 min of an unreachable server before giving up on following a run


def spawn(script: str, *args: str) -> None:
    kw = {"creationflags": 0x08000000} if os.name == "nt" else {"start_new_session": True}
    subprocess.Popen([sys.executable, str(HERE / script), *args], stdin=subprocess.DEVNULL, **kw)


class MainWindow(wx.Frame):
    def __init__(self, session):
        super().__init__(None, title="Z2Labs EMS", style=wx.DEFAULT_FRAME_STYLE | wx.STAY_ON_TOP)
        self.s = session
        self.board = session.board
        self.setup_path = session.sidecar_dir / SETUP_FILENAME
        self.setup = Setup.load(self.setup_path)
        self.ground = self.setup.nets.ground or detect_ground_nets(self.board)[:1]
        self.setup.nets.ground = self.ground
        self.ports: List[PortDef] = list(self.setup.port_defs)
        self.base: Optional[str] = None
        self.job: Optional[str] = None
        self.poll_fails = 0
        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.on_poll, self.timer)
        self.SetTitle(f"Z2Labs EMS  —  {session.board_path.name}")
        self._build()
        self.refresh_ports()
        self.refresh_suggestions()
        self.refresh_board()
        self.Fit()
        self.SetMinSize(self.GetSize())
        self.CentreOnScreen()
        try:        # at the right edge of the screen: the middle of the board stays visible
            area = wx.Display(max(0, wx.Display.GetFromWindow(self))).GetClientArea()
            w, h = self.GetSize()
            self.SetPosition((area.x + max(0, area.width - w - 24), area.y + max(0, (area.height - h) // 3)))
        except Exception:  # noqa: BLE001
            pass
        if self.setup.run.last_run:
            wx.CallAfter(self.resume_job)

    # --- layout ------------------------------------------------------------------------------------
    def _section(self, parent, sizer, title):
        t = wx.StaticText(parent, label=title)
        f = t.GetFont()
        f.SetWeight(wx.FONTWEIGHT_BOLD)
        f.SetPointSize(f.GetPointSize() + 1)
        t.SetFont(f)
        sizer.Add(t, 0, wx.LEFT | wx.RIGHT | wx.TOP, 12)
        sizer.Add(wx.StaticLine(parent), 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

    def _build(self):
        p = wx.Panel(self)
        root = wx.BoxSizer(wx.VERTICAL)

        self._section(p, root, "Ports   (a port = a + pad on the RF copper and a - reference to ground)")
        self.list = wx.ListCtrl(p, style=wx.LC_REPORT | wx.LC_SINGLE_SEL, size=(860, 150))
        for i, (t, w) in enumerate((("", 40), ("+  signal (hot)", 270), ("-  reference (ground)", 300), ("Z0", 60),
                                    ("Excite", 60), ("Status", 160))):
            self.list.InsertColumn(i, t, width=w)
        self.list.Bind(wx.EVT_LIST_ITEM_SELECTED, self.on_row)
        root.Add(self.list, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 12)
        row = wx.BoxSizer(wx.HORIZONTAL)
        for label, h, tip in (("+ Port from selection", self.on_add, "Select a connector, a pad, or a pad and its ground pads in the PCB editor"),
                              ("Set + here", self.on_set_plus, "The selected pad becomes + of the highlighted port"),
                              ("Set - here", self.on_set_minus, "The selected ground pads become - of the highlighted port"),
                              ("- = plane below", self.on_plane, "Vertical port: - is the ground plane under the + pad"),
                              ("Show", self.on_show, "Select the port's pads in the PCB editor"),
                              ("Remove", self.on_remove, "")):
            b = wx.Button(p, label=label)
            b.SetToolTip(tip) if tip else None
            b.Bind(wx.EVT_BUTTON, h)
            row.Add(b, 0, wx.RIGHT, 6)
        row.AddSpacer(14)
        row.Add(wx.StaticText(p, label="Z0"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self.z0 = wx.SpinCtrlDouble(p, min=1, max=1000, inc=1, initial=50, size=(80, -1))
        self.z0.Bind(wx.EVT_SPINCTRLDOUBLE, self.on_z0)
        row.Add(self.z0, 0, wx.RIGHT, 8)
        self.excite = wx.CheckBox(p, label="excite")
        self.excite.Bind(wx.EVT_CHECKBOX, self.on_excite)
        row.Add(self.excite, 0, wx.ALIGN_CENTER_VERTICAL)
        root.Add(row, 0, wx.ALL, 12)
        hint = wx.StaticText(p, label="Select in the PCB editor, then click: a connector or an IC pin gives + and its ground pads "
                                      "as - by itself; Show highlights the port's pads.")
        hint.SetForegroundColour(GREY)
        root.Add(hint, 0, wx.LEFT | wx.RIGHT, 12)
        root.Add(wx.StaticText(p, label="Suggestions - used only when ticked and added:"), 0, wx.LEFT | wx.TOP, 12)
        srow = wx.BoxSizer(wx.HORIZONTAL)
        self.sugg = wx.CheckListBox(p, size=(700, 70))
        srow.Add(self.sugg, 1, wx.RIGHT, 8)
        b = wx.Button(p, label="Add ticked")
        b.Bind(wx.EVT_BUTTON, self.on_add_sugg)
        srow.Add(b, 0)
        root.Add(srow, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 12)

        self._section(p, root, "Board")
        g = wx.FlexGridSizer(cols=3, hgap=12, vgap=6)
        g.AddGrowableCol(1)
        self.stack_lbl = wx.StaticText(p, label="")
        b = wx.Button(p, label="Fab stackup...")
        b.Bind(wx.EVT_BUTTON, lambda e: (self.save(), spawn("stackup_picker.py")))
        g.Add(wx.StaticText(p, label="Stackup"), 0, wx.ALIGN_CENTER_VERTICAL)
        g.Add(self.stack_lbl, 0, wx.ALIGN_CENTER_VERTICAL)
        g.Add(b)
        self.gnd_lbl = wx.StaticText(p, label="")
        g.Add(wx.StaticText(p, label="Ground nets"), 0, wx.ALIGN_CENTER_VERTICAL)
        g.Add(self.gnd_lbl, 0, wx.ALIGN_CENTER_VERTICAL)
        g.Add((1, 1))
        self.markers = wx.CheckBox(p, label="show port markers on the board")
        self.markers.SetValue(True)
        self.markers.Bind(wx.EVT_CHECKBOX, lambda e: self.redraw_markers())
        self.marker_lbl = wx.StaticText(p, label="")
        g.Add(wx.StaticText(p, label="Markers"), 0, wx.ALIGN_CENTER_VERTICAL)
        g.Add(self.marker_lbl, 0, wx.ALIGN_CENTER_VERTICAL)
        g.Add(self.markers, 0, wx.ALIGN_CENTER_VERTICAL)
        root.Add(g, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 12)

        self._section(p, root, "Simulation")
        frow = wx.BoxSizer(wx.HORIZONTAL)
        frow.Add(wx.StaticText(p, label="Frequency"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.f0 = wx.SpinCtrlDouble(p, min=0.001, max=100, inc=0.1, initial=self.setup.sim.f_start_hz / 1e9, size=(90, -1))
        self.f1 = wx.SpinCtrlDouble(p, min=0.01, max=100, inc=0.5, initial=self.setup.sim.f_stop_hz / 1e9, size=(90, -1))
        for c in (self.f0, self.f1):
            c.SetDigits(2)
        frow.Add(self.f0, 0, wx.RIGHT, 4)
        frow.Add(wx.StaticText(p, label="-"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        frow.Add(self.f1, 0, wx.RIGHT, 4)
        frow.Add(wx.StaticText(p, label="GHz"), 0, wx.ALIGN_CENTER_VERTICAL)
        root.Add(frow, 0, wx.LEFT | wx.RIGHT, 12)
        m = self.setup.mat
        mrow = wx.BoxSizer(wx.HORIZONTAL)
        mrow.Add(wx.StaticText(p, label="Losses"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.m_disp = wx.CheckBox(p, label="laminate dispersion")
        self.m_disp.SetToolTip("Djordjevic-Sarkar laminate model: the stackup's tan δ stays flat over frequency and εr(f) is causal. "
                               "Slower. Off: constant εr, the loss tangent applied at mid-band.")
        self.m_cu = wx.CheckBox(p, label="copper loss and roughness")
        self.m_cu.SetToolTip("Finite conductivity of the copper with surface roughness. Off: ideal conductors, no conductor loss.")
        self.m_rq = wx.TextCtrl(p, value="" if m.roughness_um is None else f"{m.roughness_um:g}", size=(54, -1))
        self.m_rq.SetHint("auto")
        self.m_rq.SetToolTip("Copper roughness Rq in µm. Empty: from the stackup (standard foil is about 2 µm).")
        self.m_comp = wx.CheckBox(p, label="R, L, C parts")
        self.m_comp.SetToolTip("Two-pad resistors, capacitors and inductors with a value are simulated as lumped elements. "
                               "Off: their pads stay open.")
        self.m_disp.SetValue(m.dispersion)
        self.m_cu.SetValue(m.copper_loss)
        self.m_comp.SetValue(self.setup.components.default == "lumped")
        self.m_rq.Enable(m.copper_loss)
        for c in (self.m_disp, self.m_cu, self.m_comp):
            c.Bind(wx.EVT_CHECKBOX, self.on_materials)
        self.m_rq.Bind(wx.EVT_KILL_FOCUS, self.on_materials)
        mrow.Add(self.m_disp, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 12)
        mrow.Add(self.m_cu, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        mrow.Add(wx.StaticText(p, label="Rq"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        mrow.Add(self.m_rq, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        mrow.Add(wx.StaticText(p, label="µm"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 14)
        mrow.Add(self.m_comp, 0, wx.ALIGN_CENTER_VERTICAL)
        root.Add(mrow, 0, wx.LEFT | wx.RIGHT | wx.TOP, 12)
        self.est_lbl = wx.StaticText(p, label="")
        root.Add(self.est_lbl, 0, wx.LEFT | wx.RIGHT | wx.TOP, 12)
        brow = wx.BoxSizer(wx.HORIZONTAL)
        for label, h in (("Mesh preview", self.on_mesh),
                         ("Fields on board...", self.on_fields),
                         ("3D viewer", self.on_viewer),
                         ("Clear overlays", self.on_clear)):
            b = wx.Button(p, label=label)
            b.Bind(wx.EVT_BUTTON, h)
            brow.Add(b, 0, wx.RIGHT, 6)
            if label == "Mesh preview":
                self.mesh_btn = b
        brow.AddStretchSpacer()
        self.sim_btn = wx.Button(p, label="Simulate")
        f = self.sim_btn.GetFont()
        f.SetWeight(wx.FONTWEIGHT_BOLD)
        self.sim_btn.SetFont(f)
        self.sim_btn.Bind(wx.EVT_BUTTON, self.on_simulate)
        brow.Add(self.sim_btn, 0)
        root.Add(brow, 0, wx.EXPAND | wx.ALL, 12)
        self.gauge = wx.Gauge(p, range=1000, size=(-1, 10))
        root.Add(self.gauge, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 12)
        self.status = wx.StaticText(p, label="Runs go to the engine in the background: KiCad stays free, and a run continues when "
                                            "this window is closed.")
        self.status.SetForegroundColour(GREY)
        root.Add(self.status, 0, wx.ALL, 12)

        root.Add(wx.StaticLine(p), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 12)
        foot = wx.BoxSizer(wx.HORIZONTAL)
        self.eng_lbl = wx.StaticText(p, label="")
        self.eng_lbl.SetForegroundColour(GREY)
        self.refresh_engine_label()
        foot.Add(self.eng_lbl, 1, wx.ALIGN_CENTER_VERTICAL)
        b = wx.Button(p, label="Engine...")
        b.Bind(wx.EVT_BUTTON, self.on_engine)
        foot.Add(b, 0)
        root.Add(foot, 0, wx.EXPAND | wx.ALL, 12)
        p.SetSizer(root)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(p, 1, wx.EXPAND)
        self.SetSizer(outer)
        self.Bind(wx.EVT_CLOSE, self.on_close)
        from z2ems.engine_install import set_parent
        set_parent(self)        # engine dialogs in front of this always-on-top window

    def on_engine(self, _e=None) -> None:
        """Engine...: in this process, with this window as the dialogs' parent (run as a separate
        process its dialog opened behind this always-on-top window)."""
        from z2ems.engine_install import setup_dialog
        if self.job:                    # the progress line would overwrite a status message at once
            wx.MessageBox("A simulation is running: change or update the engine when it has finished.",
                          "Z2Labs EMS - engine", wx.OK | wx.ICON_INFORMATION | wx.STAY_ON_TOP, self)
            return
        btn = _e.GetEventObject() if _e is not None else None
        if btn is not None:
            btn.Disable()               # one dialog at a time
        try:
            if setup_dialog(missing=False):
                self.base = None        # the next action finds (or starts) the new engine's server
                self.say("Engine ready: the next simulation uses it.")
        finally:
            if btn is not None:
                btn.Enable()
            self.refresh_engine_label()

    def on_materials(self, e=None) -> None:
        m = self.setup.mat
        m.dispersion, m.copper_loss = self.m_disp.GetValue(), self.m_cu.GetValue()
        self.setup.components.default = "lumped" if self.m_comp.GetValue() else "remove"
        self.m_rq.Enable(m.copper_loss)
        t = self.m_rq.GetValue().strip().replace(",", ".")
        try:
            m.roughness_um = None if not t else min(20.0, max(0.0, float(t)))
        except ValueError:
            m.roughness_um = None
            self.m_rq.SetValue("")
            self.say("Rq: a number in µm, or empty for the stackup's value.", True)
        self.save()
        if e is not None:
            e.Skip()

    def box(self, text: str, title: str, icon=wx.ICON_INFORMATION) -> None:
        """A notice in front of this always-on-top window."""
        if os.environ.get("Z2EMS_AUTO") == "1":
            return
        d = wx.MessageDialog(self, text, title, wx.OK | icon | wx.STAY_ON_TOP)
        d.ShowModal()
        d.Destroy()

    def on_mesh(self, e=None) -> None:
        """Mesh preview, in this process: its dialogs (and the engine download on first use) open in front of
        this window, the button is off while it works, and the action goes on after an engine install. As a
        separate process its windows opened behind the PCB editor and a second click started it twice."""
        from z2ems import mesh_preview as mp
        from z2ems.simulate import port_specs
        self.save()
        specs, how = port_specs(self.board, self.setup)
        if not specs:
            return self.say(mp.NO_PORTS, True)
        self.mesh_btn.Disable()
        try:
            base = self.engine("The mesh preview needs the Z2 EMS engine.")
        except Exception as ex:  # noqa: BLE001
            self.mesh_btn.Enable()
            return self.say(f"Engine: {ex}", True)
        if not base:
            return self.mesh_btn.Enable()
        self.say("Mesh preview: building the mesh of this board...")

        def work():
            try:
                res = mp.fetch_mesh(base, self.s.board_path, specs, self.setup)
            except Exception as ex:  # noqa: BLE001
                res = ex
            wx.CallAfter(done, res)

        def done(res):
            try:
                if isinstance(res, Exception):
                    raise res
                txt = mp.draw_preview(self.board, self.setup, res, specs, how, self.s.board_path.name)
            except Exception as ex:  # noqa: BLE001
                self.mesh_btn.Enable()
                self.say("Mesh preview failed.", True)
                return self.box(mp.error_text(ex, specs, how), "Z2Labs EMS - error", wx.ICON_ERROR)
            self.mesh_btn.Enable()
            self.save()
            self.say("Mesh drawn on the board. " + txt.split("\n\n")[2].splitlines()[0])
            self.box(txt, "Z2Labs EMS - mesh preview")

        threading.Thread(target=work, daemon=True).start()

    def on_clear(self, e=None) -> None:
        from z2ems.field_overlay import restore_view
        from z2ems.field_view import clear_field_lines
        from z2ems.overlays import clear_field_overlays, clear_mesh
        try:
            n_mesh = clear_mesh(self.board)
            roles = resolve_role_layers(self.board, self.setup.layers, field_tags=list(self.setup.layers.fields), allocate=False)
            n_img = clear_field_overlays(self.board, [l for r, l in roles.items() if r.startswith("field:")])
            n_img += clear_field_lines(self.board)
            restore_view(self.board, self.s.sidecar_dir)
        except Exception:  # noqa: BLE001
            self.say("Could not clear the overlays.", True)
            return self.box("Could not clear the overlays:\n" + traceback.format_exc()[-1500:], "Z2Labs EMS - error", wx.ICON_ERROR)
        self.say(f"Removed {n_mesh} mesh lines and {n_img} field overlays; normal view restored.")

    # --- state <-> view ----------------------------------------------------------------------------
    def selected(self) -> Optional[PortDef]:
        i = self.list.GetFirstSelected()
        return self.ports[i] if 0 <= i < len(self.ports) else None

    def refresh_ports(self, select: Optional[int] = None):
        renumber(self.ports)
        self.list.DeleteAllItems()
        for i, port in enumerate(self.ports):
            self.list.InsertItem(i, port.name)
            self.list.SetItem(i, 1, port.plus_text())
            self.list.SetItem(i, 2, port.minus_text())
            self.list.SetItem(i, 3, f"{port.z0:g} Ω")
            self.list.SetItem(i, 4, "yes" if port.excite else "-")
            st = port.status_text()
            self.list.SetItem(i, 5, ("●  " + st))
            self.list.SetItemTextColour(i, OK_GREEN if st == "ok" else WARN)
        if select is not None and 0 <= select < len(self.ports):
            self.list.Select(select)
            self.list.EnsureVisible(select)
        n = len(runnable(self.ports))
        skipped = len(self.ports) - n
        self.sim_btn.SetLabel(f"Simulate  ({n} port{'s' if n != 1 else ''})" if n else "Simulate")
        self.sim_btn.Enable(n > 0 and self.job is None)
        self.est_lbl.SetLabel(("Add at least one complete port to simulate." if not n else
                               f"{n} excitation{'s' if n != 1 else ''}" + (f";  {skipped} incomplete port{'s' if skipped != 1 else ''} "
                                                                           "will be skipped" if skipped else "")))
        self.est_lbl.SetForegroundColour(WARN if (skipped or not n) else wx.BLACK)
        self.save()
        self.redraw_markers()

    def refresh_suggestions(self):
        used = {p.plus.ref for p in self.ports if p.plus}
        self.sugg_items = []
        self.sugg.Clear()
        seen = set()
        for sg in suggest_ports(self.board, self.ground):
            if not sg.footprint_ref or sg.footprint_ref in used or sg.footprint_ref in seen:
                continue
            seen.add(sg.footprint_ref)
            fp = next((f for f in self.board.get_footprints() if f.reference_field.text.value == sg.footprint_ref), None)
            if fp is None:
                continue
            port = pb.port_for_footprint(self.board, fp, 0, self.ground)
            if port.plus is None:
                continue
            self.sugg_items.append(port)
            self.sugg.Append(f"{sg.reason}:  + {port.plus_text()},  - {port.minus_text()}")

    def refresh_board(self):
        try:
            from z2ems.stackup import read_stackup
            rep = read_stackup(self.board)
            cu = sum(1 for l in rep.layers if l.kind == "copper")
            src = f"  ({self.setup.stackup.preset})" if self.setup.stackup.preset else ""
            txt = f"{cu} copper, {rep.total_thickness_mm:.3f} mm{src}"
            if rep.issues:
                txt += "   ·   " + rep.issues[0]
            self.stack_lbl.SetLabel(txt)
            self.stack_lbl.SetForegroundColour(WARN if rep.issues else wx.BLACK)
        except Exception as e:  # noqa: BLE001
            self.stack_lbl.SetLabel(f"cannot read the stackup: {e}")
        self.gnd_lbl.SetLabel(", ".join(self.ground) or "none found - name your ground net GND")

    def save(self):
        self.setup.port_defs = list(self.ports)
        try:
            self.setup.sim.f_start_hz = self.f0.GetValue() * 1e9
            self.setup.sim.f_stop_hz = self.f1.GetValue() * 1e9
        except AttributeError:
            pass
        self.setup.save(self.setup_path)

    def say(self, text: str, warn: bool = False):
        self.status.SetLabel(text)
        self.status.SetForegroundColour(WARN if warn else wx.BLACK)
        self.Layout()

    def redraw_markers(self):
        try:
            if not self.markers.GetValue():
                pb.clear_markers(self.board)
                self.marker_lbl.SetLabel("hidden")
                return
            ensure_user_layers(self.board, len(ROLE_NAMES) + 1)
            roles = resolve_role_layers(self.board, self.setup.layers)
            layer = roles.get("ports")
            if layer is None:
                self.marker_lbl.SetLabel("no free user layer (enable one in Board Setup > Layers)")
                return
            n = pb.draw_markers(self.board, self.ports, layer)
            self.marker_lbl.SetLabel(f"{n} on layer {self.board.get_layer_name(layer)} (user layer: no DRC/ERC, not plotted)")
        except Exception as e:  # noqa: BLE001
            self.marker_lbl.SetLabel(f"markers not drawn: {e}")

    # --- port buttons ------------------------------------------------------------------------------
    def on_row(self, e):
        p = self.selected()
        if p:
            self.z0.SetValue(p.z0)
            self.excite.SetValue(p.excite)

    def on_add(self, e):
        port, msg = pb.port_from_selection(self.board, len(self.ports) + 1, self.ground)
        if port is not None:
            self.ports.append(port)
            self.refresh_ports(select=len(self.ports) - 1)
            self.refresh_suggestions()
        self.say(msg, warn=port is None or not port.complete)

    def on_set_plus(self, e):
        p = self.selected()
        if p is None:
            return self.say("Highlight a port in the list first.", True)
        self.say(pb.set_plus_from_selection(self.board, p))
        self.refresh_ports(select=p.n - 1)

    def on_set_minus(self, e):
        p = self.selected()
        if p is None:
            return self.say("Highlight a port in the list first.", True)
        self.say(pb.set_minus_from_selection(self.board, p, self.ground))
        self.refresh_ports(select=p.n - 1)

    def on_plane(self, e):
        p = self.selected()
        if p is None or p.plus is None:
            return self.say("Highlight a port with a + pad first.", True)
        p.minus_pads = []
        p.minus_plane = pb.plane_below(self.board, p.plus.layer, self.ground)
        self.say(f"{p.name}: - = {p.minus_plane or 'no plane found'}", warn=not p.minus_plane)
        self.refresh_ports(select=p.n - 1)

    def on_show(self, e):
        p = self.selected()
        if p is None:
            return self.say("Highlight a port in the list first.", True)
        n = pb.select_port(self.board, p)
        self.say(f"{p.name}: {n} pads selected in the PCB editor.")

    def on_remove(self, e):
        p = self.selected()
        if p is None:
            return
        self.ports.remove(p)
        self.refresh_ports()
        self.refresh_suggestions()

    def on_z0(self, e):
        p = self.selected()
        if p:
            p.z0 = float(self.z0.GetValue())
            self.refresh_ports(select=p.n - 1)

    def on_excite(self, e):
        p = self.selected()
        if p:
            p.excite = self.excite.GetValue()
            self.refresh_ports(select=p.n - 1)

    def on_add_sugg(self, e):
        added = 0
        for i in self.sugg.GetCheckedItems():
            port = self.sugg_items[i]
            port.n = len(self.ports) + 1
            port.source = "suggested"
            self.ports.append(port)
            added += 1
        if added:
            self.refresh_ports(select=len(self.ports) - 1)
            self.refresh_suggestions()
        self.say(f"{added} port{'s' if added != 1 else ''} added." if added else "Tick a suggestion first.", warn=not added)

    # --- engine ------------------------------------------------------------------------------------
    def refresh_engine_label(self):
        from z2ems.engine_install import installed_release
        cfg = load_config()
        if cfg.problems():
            txt = "Engine: not installed (downloaded when you first simulate, or with Engine...)"
        else:
            rel = installed_release(cfg.fork_root)
            txt = f"Engine: {cfg.fork_root}" + (f"  ({rel.get('tag')})" if rel else "")
        self.eng_lbl.SetLabel(f"{txt}   ·   plugin {__version__}")

    def engine(self, reason: str = "Simulating needs the Z2 EMS engine.") -> Optional[str]:
        from z2ems.engine_install import ensure_engine
        from z2ems.simulate import server_for
        ok = ensure_engine(reason)
        self.refresh_engine_label()
        if not ok:
            return None
        if self.base is None:
            self.say("Starting the engine... (the first start after an install can take a minute or two while Windows scans the new files)")
            wx.Yield()
            # replaces a server left running by another or an older engine (it outlives KiCad)
            self.base = server_for(load_config(), self.s.board_path, lambda m: None)
        return self.base

    def run_script(self, script: str):
        self.save()
        spawn(script)

    def on_viewer(self, e):
        try:
            base = self.engine()
        except Exception as ex:  # noqa: BLE001
            return self.say(f"Engine: {ex}", True)
        if base:
            webbrowser.open(f"{base}/?pcb={urllib.parse.quote(str(self.s.board_path))}")

    def on_fields(self, e):
        """Surface current / layer voltage of the newest run on the board (field_view)."""
        from z2ems import field_view as fv
        from z2ems.simulate import api
        found = fv.latest_v2_run(self.s.board_path)
        if not found:
            if any((self.s.board_path.parent / f"{self.s.board_path.stem}_twin").glob("*/fields/*.png")):
                self.say("Opening the |J| maps of an older run (simulate again for currents, voltages and lines)...")
                return self.run_script("field_overlay.py")
            return self.say("No run with fields yet: Simulate first.", True)
        run_dir, res = found
        ch = fv.ask(self, res, run_dir.name, getattr(self, "field_choice", None))
        if ch is None:
            return
        if not ch.layers:
            return self.say("No copper layer ticked.", True)
        self.field_choice = ch
        try:
            base = self.engine()
        except Exception as ex:  # noqa: BLE001
            return self.say(f"Engine: {ex}", True)
        if not base:
            return
        self.say("Drawing the fields...")
        wx.BeginBusyCursor()
        try:
            lines, _ = fv.apply(self.board, self.s.board_path, self.setup, base, run_dir, res, ch, api)
        except Exception as ex:  # noqa: BLE001
            return self.say(f"Fields: {ex}", True)
        finally:
            wx.EndBusyCursor()
        self.setup = Setup.load(self.setup_path)
        self.say("Fields on the board: " + "; ".join(l.strip().split(":")[0] for l in lines if l.strip().startswith(tuple(ch.layers))))
        if os.environ.get("Z2EMS_AUTO") != "1":
            wx.MessageBox("\n".join(lines) + "\n\nClear overlays removes them.", "Z2Labs EMS - fields",
                          wx.OK | wx.ICON_INFORMATION | wx.STAY_ON_TOP, self)

    def on_simulate(self, e):
        from z2ems.simulate import api, estimate, default_threads, fmt_minutes, speed_factor
        ports = runnable(self.ports)
        if not ports:
            return self.say("No complete port.", True)
        self.on_materials()
        try:
            base = self.engine()
        except Exception as ex:  # noqa: BLE001
            return self.say(f"Engine: {ex}", True)
        if not base:
            return
        specs = [p.to_spec() for p in ports]
        excite = [i + 1 for i, p in enumerate(ports) if p.excite]
        threads = default_threads()
        self.say("Building the mesh for an estimate...")
        wx.Yield()
        size = estimate(base, self.s.board_path, specs, self.setup.sim.f_stop_hz, threads, lambda m: None, self.setup.materials())
        kind = "debye" if self.setup.mat.dispersion else "plain"
        factor, measured = speed_factor(kind)
        self.model_min = size["minutes"] * len(excite) / max(1, len(specs)) if size else 0.0
        self.run_kind = kind
        sz = (f"Mesh {size['cells']} = {size['mcells']:.1f} million cells, {len(excite)} excitation(s): about "
              f"{fmt_minutes(self.model_min * factor)} on {threads} CPU threads"
              + (" (from earlier runs on this computer)." if measured else
                 " - a first guess: it can be two or three times that; later estimates use this computer's measured speed.")
              if size else "The mesh size could not be estimated.")
        sz += "\nModel: " + self.setup.model_text() + "."
        txt = "\n".join(f"  {p.name}:  + {p.plus_text()}\n        - {p.minus_text()}   ({p.z0:g} Ω)" for p in ports)
        if os.environ.get("Z2EMS_AUTO") != "1":
            d = wx.MessageDialog(self, f"Simulate {self.s.board_path.name}\n\n{txt}\n\n"
                                       f"{self.setup.sim.f_start_hz / 1e9:.2f} - {self.setup.sim.f_stop_hz / 1e9:.2f} GHz\n{sz}\n\n"
                                       "The run continues in the background if you close this window.",
                                 "Z2Labs EMS - simulate", wx.OK | wx.CANCEL | wx.ICON_QUESTION | wx.STAY_ON_TOP)
            d.SetOKCancelLabels("Run", "Cancel")
            r = d.ShowModal()
            d.Destroy()
            if r != wx.ID_OK:
                return self.say("Not started.")
        try:
            job = api(base, "/api/run", dict(path=str(self.s.board_path), ports=specs, excite=excite,
                                             fmin=self.setup.sim.f_start_hz, fmax=self.setup.sim.f_stop_hz,
                                             threads=threads, materials=self.setup.materials()), timeout=120)
        except Exception as ex:  # noqa: BLE001
            return self.say(f"The run did not start: {ex}", True)
        self.job = job.get("id")
        self.setup.run.last_run = self.job
        self.save()
        self.sim_btn.Enable(False)
        self.poll_fails = 0
        self.t_run = time.time()
        self.est_min = self.model_min * factor
        self.timer.Start(int(POLL_S * 1000))
        self.say(f"Run {self.job} started.")

    def resume_job(self):
        """A run started earlier (window closed meanwhile): follow it if the server still has it."""
        from z2ems.simulate import api
        try:
            from z2ems.simulate import server_for
            # replace=False: never stop a server here, it may be running that very job
            self.base = server_for(load_config(), self.s.board_path, lambda m: None, replace=False)
            j = api(self.base, f"/api/job/{self.setup.run.last_run}", timeout=5)
        except Exception:  # noqa: BLE001
            return
        if j.get("state") in ("queued", "running"):
            self.job = self.setup.run.last_run
            self.sim_btn.Enable(False)
            self.timer.Start(int(POLL_S * 1000))
            self.say(f"Following run {self.job} ({j.get('state')}).")

    def on_poll(self, e):
        from z2ems.simulate import api
        try:
            j = api(self.base, f"/api/job/{self.job}", timeout=10)
            self.poll_fails = 0
        except Exception as ex:  # noqa: BLE001  - a busy server (mesh, post-processing) answers late: keep waiting
            self.poll_fails += 1
            if self.poll_fails >= POLL_FAILS_MAX:
                self.timer.Stop()
                self.job = None
                self.refresh_ports()
                self.say(f"Lost contact with the engine ({ex}); the run may still finish - see the 3D viewer.", True)
            return
        st = j.get("state", "?")
        pr = j.get("progress", {}) or {}
        frac = float(pr.get("frac") or 0)          # of the current excitation
        ts = int(pr.get("timestep") or 0)
        try:
            exc, n = int(pr.get("exc") or 0), int(pr.get("n") or 0)
        except (TypeError, ValueError):
            exc, n = 0, 0
        # an engine before 0.3.2 keeps the last excitation's numbers until the next one's first time step:
        # the bar jumped to full and back
        if exc != getattr(self, "_p_exc", None):
            self._p_exc, self._p_stale = exc, getattr(self, "_p_ts", 0)
        if getattr(self, "_p_stale", 0) and ts == self._p_stale:
            frac, ts = 0.0, 0
        else:
            self._p_stale = 0
        self._p_ts = int(pr.get("timestep") or 0)
        if n > 0 and exc >= 1:
            frac = (exc - 1 + frac) / n
        if st in ("queued", "running"):
            self.gauge.SetValue(int(1000 * min(max(frac, 0.0), 0.99)))
            el = time.time() - getattr(self, "t_run", time.time())
            clock = f"{int(el // 60)}:{int(el % 60):02d}"
            est = getattr(self, "est_min", 0.0)
            clock += f" of about {max(1, round(est))} min" if est and el < est * 60 else ""
            head = f"Running, {clock}:  excitation {max(exc, 1)} of {n or '?'}" if st == "running" else "Queued behind another run"
            if st == "running" and ts == 0:
                self.say(f"{head} - {pr.get('stage') or 'preparing the model'} (mesh and engine setup: up to a few minutes, "
                         "the time steps start after it)")
            elif st == "running":
                speed = pr.get("mcells_s")
                self.say(f"{head},  time step {ts},  energy {round(float(pr.get('energy_db') or 0)) + 0:d} dB (stops at -40 dB)"
                         + (f",  {float(speed):.1f} million cells/s" if speed else ""))
            else:
                self.say(head)
            return
        self.timer.Stop()
        self.job = None
        self.refresh_ports()
        if st == "done" and j.get("result"):
            from z2ems.simulate import summary, compare_reference
            res = j["result"]
            if getattr(self, "t_run", None) and getattr(self, "model_min", 0):      # followed from its start
                from z2ems.simulate import record_speed
                record_speed(self.model_min, (time.time() - self.t_run) / 60.0, getattr(self, "run_kind", "plain"))
            self.t_run = None
            text = summary(res)
            ref = compare_reference(self.s.board_path.stem, res)
            if ref:
                text += "\n\n" + "\n".join(ref)
            self.gauge.SetValue(1000)
            self.say("Done. " + text.splitlines()[-1])
            if os.environ.get("Z2EMS_NO_BROWSER") != "1":
                webbrowser.open(f"{self.base}/?pcb={urllib.parse.quote(str(self.s.board_path))}")
            self.last_result = text
            if os.environ.get("Z2EMS_AUTO") != "1":
                wx.MessageBox(text + f"\n\nResults: {self.s.board_path.stem}_twin/ (Earlier runs in the 3D viewer)",
                              "Z2Labs EMS - result", wx.ICON_INFORMATION | wx.STAY_ON_TOP, self)
        else:
            self.say(f"Run {st}: {j.get('error') or ''}", True)

    def on_close(self, e):
        self.timer.Stop()
        self.save()
        e.Skip()


def main() -> int:
    app = wx.App(False)
    try:
        s = connect()
    except Exception as e:  # noqa: BLE001
        wx.MessageBox(connect_error_text(e), "Z2Labs EMS", wx.ICON_ERROR | wx.STAY_ON_TOP)
        return 1
    try:
        w = MainWindow(s)
    except Exception:  # noqa: BLE001
        wx.MessageBox("Z2Labs EMS could not open:\n\n" + traceback.format_exc()[-2500:], "Z2Labs EMS - error",
                      wx.ICON_ERROR | wx.STAY_ON_TOP)
        return 1
    w.Show()
    w.Raise()
    app.MainLoop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
