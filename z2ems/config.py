"""Where the Z2 EMS engine lives on this machine.

The engine is a Z2 EMS engine package (a release zip, extracted: bin/ with openEMS, python/ with CSXCAD and
openEMS bindings, twin/, gui/) or a source checkout of z2labs/z2ems with a python environment.

Resolution order: environment -> ~/.z2ems.json -> default locations.
  Z2EMS_ENGINE    engine package root (sets twin python, root and engine executable at once)
  Z2EMS_TWIN_PY   python with CSXCAD/openEMS bindings
  Z2EMS_FORK_ROOT root containing twin/ and gui/ (a z2ems checkout or the engine package)
  Z2EMS_OPENEMS   openEMS executable (optional; the package's bin/openEMS is found by the engine itself)
  Z2EMS_SERVER    base URL of a running z2twin server (default http://127.0.0.1:8765)
~/.z2ems.json accepts the keys engine, twin_py, fork_root, openems_exe.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class EngineConfig:
    twin_py: Path
    fork_root: Path
    server: str = "http://127.0.0.1:8765"
    openems_exe: Path | None = None     # engine the server's 'local' variant runs (-> Z2_OPENEMS)

    @property
    def server_script(self) -> Path:
        return self.fork_root / "gui" / "server" / "z2twin_server.py"

    @property
    def kicad_sim(self) -> Path:
        return self.fork_root / "twin" / "kicad_sim.py"

    def problems(self) -> list[str]:
        out = []
        if not self.twin_py.exists():
            out.append(f"twin python not found: {self.twin_py}")
        if not self.server_script.exists():
            out.append(f"z2twin_server.py not found under: {self.fork_root}")
        if self.openems_exe and not self.openems_exe.exists():
            out.append(f"openEMS engine not found: {self.openems_exe}")
        return out

    def server_env(self) -> dict:
        """Environment for a z2twin server started by the plugin."""
        env = dict(os.environ)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        if self.openems_exe:
            env["Z2_OPENEMS"] = str(self.openems_exe)
        return env


def _package_python(root: Path) -> Path:
    """The python of an engine package or checkout: python/ (Windows package), venv/ or .venv/."""
    for c in (root / "python" / "python.exe", root / "venv" / "Scripts" / "python.exe", root / ".venv" / "Scripts" / "python.exe",
              root / "python" / "bin" / "python3", root / "venv" / "bin" / "python", root / ".venv" / "bin" / "python"):
        if c.exists():
            return c
    return Path(sys.executable)


def _default_roots() -> list[Path]:
    home = Path.home()
    if os.name == "nt":
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        return [local / "Z2EMS" / "engine", Path("C:/Z2EMS"), home / "z2ems"]
    return [home / ".local" / "share" / "z2ems" / "engine", home / "z2ems"]


def _defaults() -> tuple[Path, Path]:
    for root in _default_roots():
        if (root / "gui" / "server" / "z2twin_server.py").exists():
            return _package_python(root), root
    root = _default_roots()[0]
    return _package_python(root), root


def load() -> EngineConfig:
    twin_py, fork_root = _defaults()
    exe = None
    cfg_file = Path.home() / ".z2ems.json"
    if cfg_file.exists():
        try:
            j = json.loads(cfg_file.read_text(encoding="utf-8"))
            if j.get("engine"):
                fork_root = Path(j["engine"])
                twin_py = _package_python(fork_root)
            twin_py = Path(j.get("twin_py", twin_py))
            fork_root = Path(j.get("fork_root", fork_root))
            exe = j.get("openems_exe") or None
        except json.JSONDecodeError:
            pass
    if os.environ.get("Z2EMS_ENGINE"):
        fork_root = Path(os.environ["Z2EMS_ENGINE"])
        twin_py = _package_python(fork_root)
    twin_py = Path(os.environ.get("Z2EMS_TWIN_PY", twin_py))
    fork_root = Path(os.environ.get("Z2EMS_FORK_ROOT", fork_root))
    server = os.environ.get("Z2EMS_SERVER", "http://127.0.0.1:8765")
    exe = os.environ.get("Z2EMS_OPENEMS", exe)
    return EngineConfig(twin_py=twin_py, fork_root=fork_root, server=server,
                        openems_exe=Path(exe) if exe else None)
