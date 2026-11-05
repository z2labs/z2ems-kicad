"""`z2ems-simulate` action: run the Z2 EMS engine on the open board and show the
result in the viewer dashboard (DESIGN M2, section 7).

Flow
  1. ports: Z2EMS_PORT footprints on the board; if none, the connector-based
     suggestions (SMA / u.FL pads) -- never more than 4, user confirms the list.
  2. engine: a running z2twin server whose folder contains the board, else one is
     started (twin python, gui/server/z2twin_server.py <board folder>).
  3. POST /api/run, open the viewer on this board, poll /api/job/<id> with a
     progress dialog, then show Z0 / eps_eff / |S11| max / |S21| min and, when a
     reference file exists for this board (presets/refs/<stem>.json), the
     deviation from the documented engine / closed-form values.
Results land in <board>_twin/<date-time>/ next to the board, listed under
"Earlier runs" in the viewer.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from z2ems import SETUP_FILENAME  # noqa: E402
from z2ems.config import EngineConfig, load as load_config  # noqa: E402
from z2ems.ipc import connect  # noqa: E402
from z2ems.model import Setup  # noqa: E402
from z2ems.ports import read_ports, suggest_ports, detect_ground_nets  # noqa: E402
from z2ems.ui import notify  # noqa: E402

REFS_DIR = Path(__file__).resolve().parent.parent / "presets" / "refs"


# --- HTTP ---------------------------------------------------------------------

def api(base: str, path: str, body: dict | None = None, timeout: float = 30) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:800]
        try:
            body = json.loads(body).get("error", body)
        except ValueError:
            pass
        raise RuntimeError(f"engine {path}: {body}") from None


def server_for(cfg: EngineConfig, board: Path, log) -> str:
    """Return the base URL of a z2twin server that serves `board`'s folder; start one if needed.

    One server serves one folder, so with several boards open over a day there may be several
    servers on consecutive ports: walk the ports, reuse the one whose folder contains the board,
    and start a new server only on a port where nothing answers (a long GUI test had three
    servers on 8765-8767 and a fourth one failing to bind, 2026-10-17)."""
    folder = board.parent.resolve()
    u = urllib.parse.urlparse(cfg.server)
    port0 = u.port or 8765
    for port in range(port0, port0 + 8):
        base = u._replace(netloc=f"{u.hostname}:{port}").geturl()
        try:
            h = api(base, "/api/health", timeout=3)
        except (urllib.error.URLError, ConnectionError, OSError, RuntimeError):
            _start_server(cfg, folder, base, log)
            for _ in range(80):
                time.sleep(0.5)
                try:
                    h = api(base, "/api/health", timeout=2)
                    break
                except Exception:  # noqa: BLE001
                    continue
            else:
                raise RuntimeError(f"z2twin server did not come up on {base}")
            root = Path(h.get("folder", "")).resolve()
            if root == folder or root in folder.parents:
                log(f"engine: {h.get('variant')} ({h.get('engine')}) serving {root}")
                return base
            log(f"server on {base} came up for {root}, not {folder}; trying the next port")
            continue
        root = Path(h.get("folder", "")).resolve()
        if root and (root == folder or root in folder.parents):
            log(f"engine: {h.get('variant')} ({h.get('engine')}) serving {root}")
            return base
        log(f"server on {base} serves {root}, not {folder}; next port")
    raise RuntimeError(f"no free port for a z2twin server in {port0}..{port0 + 7}")


def _start_server(cfg: EngineConfig, folder: Path, base: str, log) -> None:
    port = urllib.parse.urlparse(base).port or 8765
    env = cfg.server_env()
    logf = open(folder / "z2twin_server.log", "a", encoding="utf-8")
    kw = {}
    if os.name == "nt":
        # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP. Not DETACHED_PROCESS: a detached parent has
        # no console, so openEMS.exe (a console child of the server) pops a black terminal over
        # the PCB editor (seen live on Windows 11). With a hidden console the
        # children inherit it and stay invisible.
        kw["creationflags"] = 0x08000000 | 0x00000200
    else:
        kw["start_new_session"] = True
    subprocess.Popen([str(cfg.twin_py), str(cfg.server_script), str(folder), "--port", str(port)],
                     stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env,
                     cwd=str(cfg.fork_root / "twin"), **kw)
    log(f"started z2twin server on {base} for {folder}")


# --- ports --------------------------------------------------------------------

def port_specs(board, ground: list[str]) -> tuple[list[dict], str]:
    """[{ref, net, pad_no?, z0}] for /api/run, and how they were chosen."""
    ps = read_ports(board)
    if ps:
        return [dict(ref=p.reference, net=p.sig_net, pad_no="1", z0=p.z0) for p in ps if p.sig_net], "Z2EMS_PORT footprints"
    sug = [s for s in suggest_ports(board, ground) if s.footprint_ref]
    # one port per connector (NOT per net: both ends of a through line sit on the same net --
    # a live run of coupon_msl50 got only J1 and a 1-port sweep)
    seen, out = set(), []
    for s in sug:
        if s.footprint_ref in seen:
            continue
        seen.add(s.footprint_ref)
        out.append(dict(ref=s.footprint_ref, net=s.net, pad_no=s.pad_number, z0=50.0))
        if len(out) == 4:
            break
    return out, "connector suggestions"


# --- reference comparison ------------------------------------------------------

def compare_reference(stem: str, res: dict) -> list[str]:
    f = REFS_DIR / f"{stem}.json"
    if not f.exists():
        return []
    ref = json.loads(f.read_text(encoding="utf-8"))
    lines = [f"Reference ({ref.get('source', 'documented')}):"]
    for key, label, unit in (("Z0_ohm", "Z0", " ohm"), ("eeff", "eps_eff", "")):
        if key in res and key in ref:
            dv = (res[key] - ref[key]) / ref[key] * 100
            lines.append(f"  {label}: {res[key]:.2f}{unit} vs {ref[key]:.2f}{unit} ({dv:+.1f} %)")
    s11 = res.get("S_dB", {}).get("S11")
    if s11 and "s11_max_db" in ref:
        mx = max(s11)
        lines.append(f"  |S11| max: {mx:.1f} dB (reference limit {ref['s11_max_db']:.0f} dB) -> {'ok' if mx <= ref['s11_max_db'] else 'ABOVE'}")
    if ref.get("note"):
        lines.append("  " + ref["note"])
    return lines


def line_model_doubt(res: dict) -> str:
    """Why the uniform-line Z0/eps_eff extraction is meaningless for this 2-port, or "".
    Seen on series_c1p (a 1 pF series cap): it was reported as a line with
    Z0 = 1.97 ohm, eps_eff = 1.53 - numbers a user would take at face value."""
    z0, ee = res.get("Z0_ohm"), res.get("eeff")
    s21 = res.get("S_dB", {}).get("S21") or []
    if s21 and min(s21) < -10:
        return f"|S21| drops to {min(s21):.1f} dB"
    if z0 is None or not 5.0 <= z0 <= 300.0:
        return f"Z0 {z0:.2f} ohm out of range" if z0 is not None else "no Z0"
    if ee is None or not 1.0 <= ee <= 15.0:
        return f"eps_eff {ee} out of range"
    return ""


def summary(res: dict) -> str:
    out = [f"Board: {res.get('board')}", f"Stackup: {res.get('stackup')}",
           f"Mesh: {res.get('mesh')} ({res.get('mcells')} MCells)"]
    if "Z0_ohm" in res:
        why = line_model_doubt(res)
        if why:
            out.append(f"Not a uniform line ({why}): the Z0/eps_eff extraction "
                       f"(Z0={res['Z0_ohm']:.2f} ohm, eps_eff={res['eeff']:.3f}) does not apply - read the S-parameters.")
        else:
            out.append(f"Line: L={res.get('line_length_mm', 0):.1f} mm  Z0={res['Z0_ohm']:.2f} ohm  eps_eff={res['eeff']:.3f}")
    sdb = res.get("S_dB", {})
    fg = res.get("f_GHz", [])
    if "S11" in sdb:
        out.append(f"|S11| max {max(sdb['S11']):.1f} dB over {fg[0]:.2f}-{fg[-1]:.2f} GHz" if fg else f"|S11| max {max(sdb['S11']):.1f} dB")
    if "S21" in sdb:
        out.append(f"|S21| min {min(sdb['S21']):.2f} dB")
    return "\n".join(out)


# --- run-size estimate ----------------------------------------------------------

# Measured on a dual-core laptop (i7-7500U, threads=3 requested, CPU engine): coupon_msl50,
# 0.39 MCells, 2 excitations, ~10.4k timesteps each -> 390 s. Time per excitation scales
# with cells x timesteps; timesteps scale roughly with the board diagonal over the
# smallest cell, so cells^1.33 is a fair first-order model for a size warning.
REF_MCELLS, REF_SECONDS_PER_EXC, REF_THREADS = 0.39, 195.0, 3
BIG_RUN_MIN = 30.0


def estimate(base: str, board: Path, specs: list[dict], fmax: float, threads: int, log, materials: dict | None = None) -> dict | None:
    try:
        m = api(base, "/api/mesh", dict(path=str(board), ports=specs, fmax=fmax, materials=materials or {}), timeout=300)
    except Exception as e:  # noqa: BLE001
        log(f"mesh estimate failed: {e}")
        return None
    nx, ny, nz = len(m["x"]), len(m["y"]), len(m["z"])
    mc = nx * ny * nz / 1e6
    return dict(cells=f"{nx}x{ny}x{nz}", mcells=mc, minutes=run_minutes(mc, len(specs), threads))


def run_minutes(mcells: float, excitations: int, threads: int) -> float:
    """First-order wall-clock estimate (minutes) for this machine class, see REF_* above."""
    sec = REF_SECONDS_PER_EXC * (mcells / REF_MCELLS) ** 1.33 * excitations * REF_THREADS / max(1, min(threads, 8))
    return sec / 60.0


def default_threads() -> int:
    return max(1, (os.cpu_count() or 2) - 1)


def fmt_minutes(m: float) -> str:
    if m < 1:
        return "under a minute"
    if m < 90:
        return f"{m:.0f} min"
    if m < 48 * 60:
        return f"{m / 60:.1f} h"
    return f"{m / 1440:.0f} days"


# --- GUI -----------------------------------------------------------------------

def run_gui() -> int:
    import wx
    app = wx.App(False)
    from z2ems.engine_install import ensure_engine
    if not ensure_engine("Simulating needs the Z2 EMS engine."):
        return 1
    cfg = load_config()
    try:
        s = connect()
    except Exception as e:  # noqa: BLE001
        notify("Z2Labs EMS", f"Cannot connect to KiCad: {e}", "error")
        return 1
    board_path = s.board_path
    setup = Setup.load(s.sidecar_dir / SETUP_FILENAME)
    ground = setup.nets.ground or detect_ground_nets(s.board)[:1]
    specs, how = port_specs(s.board, ground)
    if not specs:
        notify("Z2Labs EMS", "No ports: place Z2EMS_PORT footprints or add an RF connector.", "error", s.sidecar_dir)
        return 1
    fmin, fmax = setup.sim.f_start_hz, setup.sim.f_stop_hz
    txt = "\n".join(f"  P{i+1}: {p['ref']} pad {p.get('pad_no') or '?'}  net {p['net']}  Z0 {p['z0']:g}" for i, p in enumerate(specs))
    log_lines: list[str] = []
    def log(m):
        log_lines.append(m)

    try:
        base = server_for(cfg, board_path, log)
    except Exception:  # noqa: BLE001
        notify("Z2Labs EMS", "Could not start the engine:\n" + traceback.format_exc()[-1500:], "error", s.sidecar_dir)
        return 1
    threads = default_threads()
    size = estimate(base, board_path, specs, fmax, threads, log, setup.materials())
    auto = os.environ.get("Z2EMS_AUTO") == "1"      # soak harness: no confirmation click
    if not auto:
        big = size is not None and size["minutes"] > BIG_RUN_MIN
        style = wx.YES_NO | wx.STAY_ON_TOP | (wx.NO_DEFAULT | wx.ICON_WARNING if big else wx.ICON_QUESTION)
        sz = (f"Mesh {size['cells']} = {size['mcells']:.1f} MCells, {len(specs)} excitations: "
              f"roughly {fmt_minutes(size['minutes'])} on {threads} threads here."
              + ("\nThis is a long run for this machine - consider fewer ports, a lower fmax, or a GPU box."
                 if big else "")) if size else "Mesh size unknown (engine could not build it)."
        if wx.MessageBox(f"Simulate {board_path.name}\n\nPorts ({how}):\n{txt}\n\n{fmin/1e9:.2f}-{fmax/1e9:.2f} GHz, "
                         f"engine on {base}\n{sz}\n\nRun now?", "Z2Labs EMS - simulate", style) != wx.YES:
            return 0

    try:
        job = api(base, "/api/run", dict(path=str(board_path), ports=specs, fmax=fmax, fmin=fmin,
                                         threads=threads, materials=setup.materials()), timeout=120)
    except Exception:  # noqa: BLE001
        notify("Z2Labs EMS", "Could not start the run:\n" + traceback.format_exc()[-1500:], "error", s.sidecar_dir)
        return 1
    jid = job.get("id")
    if os.environ.get("Z2EMS_NO_BROWSER") != "1":      # soak harness: no browser tab per run
        webbrowser.open(f"{base}/?pcb={urllib.parse.quote(str(board_path))}")

    dlg = wx.ProgressDialog("Z2Labs EMS - simulating", f"job {jid} started\n", maximum=1000,
                            style=wx.PD_APP_MODAL | wx.PD_CAN_ABORT | wx.PD_ELAPSED_TIME | wx.PD_AUTO_HIDE)
    # the plugin is a separate process: without STAY_ON_TOP its windows open *behind* the maximized
    # PCB editor (seen live on Windows 11: progress and result were only visible as taskbar thumbnails)
    dlg.SetWindowStyleFlag(dlg.GetWindowStyleFlag() | wx.STAY_ON_TOP)
    dlg.Raise()
    dlg.SetSize((620, 260))
    state, res, err = "running", None, None
    try:
        while True:
            time.sleep(1.0)
            try:
                j = api(base, f"/api/job/{jid}", timeout=10)
            except Exception as e:  # noqa: BLE001
                err = str(e); break
            state = j.get("state", "?")
            pr = j.get("progress", {}) or {}
            tail = (j.get("log") or j.get("log_tail") or [])
            tail = tail[-3:] if isinstance(tail, list) else str(tail)[-200:]
            msg = (f"{state}  excitation {pr.get('exc', 0)}/{pr.get('n', '?')}  timestep {pr.get('timestep', 0)}"
                   f"  energy {pr.get('energy_db', 0):.0f} dB  {pr.get('mcells_s') or ''}\n"
                   + ("\n".join(tail) if isinstance(tail, list) else tail))
            cont, _ = dlg.Update(int(1000 * float(pr.get("frac") or 0)), msg)
            if not cont:
                try:
                    api(base, f"/api/job/{jid}/cancel", {}, timeout=10)
                except Exception:  # noqa: BLE001
                    pass
                state = "cancelled"; break
            if state in ("done", "error", "cancelled", "failed"):
                res = j.get("result"); err = j.get("error"); break
    finally:
        dlg.Destroy()

    if state == "done" and res:
        setup.run.backend = "local"; setup.run.last_run = jid
        setup.save(s.sidecar_dir / SETUP_FILENAME)
        text = summary(res)
        ref = compare_reference(board_path.stem, res)
        if ref:
            text += "\n\n" + "\n".join(ref)
        text += f"\n\nResults: {board_path.stem}_twin/ (Earlier runs in the viewer)"
        notify("Z2Labs EMS - result", text, "info", s.sidecar_dir)
        return 0
    notify("Z2Labs EMS - error", f"Run ended: {state}\n{err or ''}\n\n" + "\n".join(log_lines[-5:]), "warn", s.sidecar_dir)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(run_gui())
    except Exception:  # noqa: BLE001
        if os.name == "nt":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, traceback.format_exc()[-3000:], "Z2Labs EMS - error", 0x10)
        raise
