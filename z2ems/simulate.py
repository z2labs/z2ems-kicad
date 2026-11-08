"""Engine helpers for the Z2Labs EMS window and the overlay actions: the local engine server (start / find),
the HTTP API, port specs from the user's port definitions, run-size estimate, result summary and the
comparison with a documented reference. The toolbar "simulate" button opens the Z2Labs EMS window.

Results land in <board>_twin/<date-time>/ next to the board, listed under "Earlier runs" in the viewer.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from z2ems.config import EngineConfig  # noqa: E402
from z2ems.ports import read_ports  # noqa: E402


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


# the engine server API this plugin needs (an older server is replaced, not used)
NEED_API = {"ports_api": 2, "fields_api": 2}


def _norm(p) -> str:
    """Absolute, normalised ('..' removed), case-folded on Windows; symlinks are not followed (an
    engine folder may link its openEMS from elsewhere)."""
    try:
        return os.path.normcase(os.path.abspath(str(p))) if p else ""
    except (OSError, ValueError, TypeError):
        return ""


def server_engine_root(h: dict) -> str:
    """The engine folder a running server was started from: `root` from engines that report it,
    else derived from the openEMS executable it runs (<root>/bin/openEMS.exe in a release package)."""
    if h.get("root"):
        return _norm(h["root"])
    eng = h.get("engine") or ""
    if os.sep in eng or "/" in eng:
        d = Path(eng).parent
        return _norm(d.parent if d.name.lower() == "bin" else d)
    return ""


def server_is_current(h: dict, cfg: EngineConfig) -> tuple[bool, str]:
    """Is the server that answered `h` one of the configured engine, with the API this plugin needs?
    A server keeps running after KiCad closes, so after an engine update or a change of engine
    folder the old one would otherwise keep answering on the port."""
    for k, v in NEED_API.items():
        if int(h.get(k) or 1) < v:
            return False, f"older engine ({k} {h.get(k) or 1} < {v})"
    root = server_engine_root(h)
    want = _norm(cfg.fork_root)
    if not root:
        return False, "a server that does not say which engine it runs"
    exe = cfg.openems_exe or os.environ.get("Z2_OPENEMS")      # an engine run with its own openEMS build
    if root != want and not (exe and _norm(h.get("engine")) == _norm(exe)):
        return False, f"engine in {root}, not the selected {want}"
    return True, ""


def _pid_on_port(port: int) -> int | None:
    """The process listening on a local TCP port (Windows: Get-NetTCPConnection; Linux: /proc)."""
    try:
        if os.name == "nt":
            out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                                  f"(Get-NetTCPConnection -LocalPort {int(port)} -State Listen -ErrorAction SilentlyContinue"
                                  " | Select-Object -First 1).OwningProcess"],
                                 capture_output=True, text=True, timeout=30, creationflags=0x08000000)
            t = (out.stdout or "").strip()
            return int(t) if t.isdigit() else None
        inodes = set()
        for f in ("/proc/net/tcp", "/proc/net/tcp6"):
            try:
                for line in open(f).read().splitlines()[1:]:
                    c = line.split()
                    if c[3] == "0A" and int(c[1].rsplit(":", 1)[1], 16) == port:
                        inodes.add(c[9])
            except OSError:
                pass
        for d in Path("/proc").iterdir():
            if not d.name.isdigit():
                continue
            try:
                for fd in (d / "fd").iterdir():
                    t = os.readlink(fd)
                    if t.startswith("socket:[") and t[8:-1] in inodes:
                        return int(d.name)
            except OSError:
                continue
    except Exception:  # noqa: BLE001
        return None
    return None


def _cmdline(pid: int) -> str:
    try:
        if os.name == "nt":
            out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                                  f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine"],
                                 capture_output=True, text=True, timeout=30, creationflags=0x08000000)
            return out.stdout or ""
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except Exception:  # noqa: BLE001
        return ""


def stop_server(base: str, h: dict, log) -> bool:
    """Stop the engine server on `base` (only a z2twin server: checked by its command line). True when
    the port is free afterwards."""
    port = urllib.parse.urlparse(base).port or 8765
    try:                                             # engines from 0.3.1 stop themselves
        api(base, "/api/shutdown", {}, timeout=5)
    except Exception:  # noqa: BLE001
        pass
    for _ in range(10):
        time.sleep(0.3)
        try:
            api(base, "/api/health", timeout=2)
        except Exception:  # noqa: BLE001
            return True
    pid = h.get("pid") or _pid_on_port(port)
    if pid and "z2twin_server" in _cmdline(int(pid)):
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"], capture_output=True, timeout=30,
                               creationflags=0x08000000)
            else:
                os.kill(int(pid), 15)
        except Exception:  # noqa: BLE001
            pass
    for _ in range(20):
        time.sleep(0.5)
        try:
            api(base, "/api/health", timeout=2)
        except Exception:  # noqa: BLE001
            log(f"stopped the old engine server on {base}")
            return True
    return False


def server_for(cfg: EngineConfig, board: Path, log, replace: bool = True) -> str:
    """Return the base URL of a z2twin server of the configured engine that serves `board`'s folder;
    start one if needed.

    One server serves one folder, so with several boards open over a day there may be several
    servers on consecutive ports: walk the ports, reuse the one whose folder contains the board,
    and start a new server only on a port where nothing answers (a long GUI test had three
    servers on 8765-8767 and a fourth one failing to bind, 2026-10-17). A server of another engine
    folder or an older engine (left running from before an engine update - a server outlives
    KiCad) is stopped and replaced; if it cannot be stopped, the next port is used."""
    folder = board.parent.resolve()
    u = urllib.parse.urlparse(cfg.server)
    port0 = u.port or 8765
    for port in range(port0, port0 + 8):
        base = u._replace(netloc=f"{u.hostname}:{port}").geturl()
        try:
            h = api(base, "/api/health", timeout=3)
        except (urllib.error.URLError, ConnectionError, OSError, RuntimeError):
            h = None
        if h is not None:
            root = Path(h.get("folder", "")).resolve()
            serves = bool(h.get("folder")) and (root == folder or root in folder.parents)
            ok, why = server_is_current(h, cfg)
            if serves and ok:
                log(f"engine: {h.get('variant')} ({h.get('engine')}) serving {root}")
                return base
            if not serves:
                log(f"server on {base} serves {root}, not {folder}; next port")
                continue
            if not replace:
                raise RuntimeError(f"the engine server on {base} is not the selected engine ({why})")
            if h.get("busy"):
                log(f"server on {base}: {why}, but it is still running a simulation; next port")
                continue
            log(f"server on {base}: {why}; replacing it")
            if not stop_server(base, h, log):
                log(f"could not stop the server on {base}; next port")
                continue
        _start_server(cfg, folder, base, log)
        for _ in range(240):                         # first start after an install: virus scan
            time.sleep(0.5)
            try:
                h = api(base, "/api/health", timeout=2)
                break
            except Exception:  # noqa: BLE001
                continue
        else:
            raise RuntimeError(f"z2twin server did not come up on {base}")
        root = Path(h.get("folder", "")).resolve()
        # this server was started just now from the selected engine: only its API level can be wrong
        ok, why = True, ""
        for k, v in NEED_API.items():
            if int(h.get(k) or 1) < v:
                ok, why = False, f"{k} {h.get(k) or 1} < {v}"
        if (root == folder or root in folder.parents) and ok:
            log(f"engine: {h.get('variant')} ({h.get('engine')}) serving {root}")
            return base
        if not ok:
            raise RuntimeError(f"the selected engine is too old for this plugin ({why}): update it with Engine...")
        log(f"server on {base} came up for {root}, not {folder}; trying the next port")
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

def port_specs(board, setup) -> tuple[list[dict], str]:
    """Port specs for /api/mesh and /api/run, and where they came from: the ports defined in the Z2Labs EMS
    window (setup.json port_defs; complete ones only), else Z2EMS_PORT footprints. Never a guess: an empty
    list means the user has to define ports first."""
    from z2ems.portdefs import runnable
    ps = runnable(setup.port_defs)
    if ps:
        return [p.to_spec() for p in ps], "Z2Labs EMS ports"
    fps = read_ports(board)
    if fps:
        return [dict(ref=p.reference, net=p.sig_net, pad_no="1", z0=p.z0) for p in fps if p.sig_net], "Z2EMS_PORT footprints"
    return [], "none"


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
    sdb = {k: [x for x in v if x is not None] for k, v in res.get("S_dB", {}).items()}
    sdb = {k: v for k, v in sdb.items() if v}
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

if __name__ == "__main__":
    # The toolbar "simulate" button opens the Z2Labs EMS window: ports are confirmed there and the run is
    # followed there (a run started from a bare dialog with guessed ports was the 0.1 behaviour).
    from z2ems.main_window import main
    sys.exit(main())
