"""setup.json data model (sidecar, v1). Pure Python - no KiCad dependency.

Footprint fields are the truth for per-port electrical values; this file only
holds user choices that have no natural place on the board, caches, and run
bookkeeping. See docs/DESIGN.md section 3.2.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

SETUP_VERSION = 2


@dataclass
class StackupSetup:
    source: str = "kicad"            # "kicad" | "preset"
    preset: Optional[str] = None     # e.g. "JLC04161H-7628"
    validated: bool = False
    issues: List[str] = field(default_factory=list)


@dataclass
class NetsSetup:
    ground: List[str] = field(default_factory=list)
    excluded: List[str] = field(default_factory=list)
    signal_override: Dict[str, str] = field(default_factory=dict)


@dataclass
class ComponentsSetup:
    # "lumped" | "remove" | "keep3d". Default lumped: "remove" turns a series part into an open gap
    # (series_c1p: |S21| -66 dB for a 1 pF coupling cap).
    default: str = "lumped"
    overrides: Dict[str, str] = field(default_factory=dict)   # fp-uuid -> mode


@dataclass
class PortExtra:
    deembed_mm: float = 0.0
    note: str = ""
    # cached snapshot of the footprint fields at last run (for staleness / orphan display)
    last_seen: Dict[str, str] = field(default_factory=dict)


@dataclass
class SimSetup:
    f_start_hz: float = 1e8
    f_stop_hz: float = 6e9
    excite: List[str] = field(default_factory=list)      # port references, e.g. ["P1", "P2"]
    s_matrix_mask: str = "full"                          # "full" | "selected"
    boundary: str = "PML_8"
    mesh_policy: str = "auto"                            # "auto" | "ml" | "manual"
    accuracy: int = 2                                    # 1 (fast) .. 4 (fine)
    end_criteria_db: float = -40.0
    memory_guard: bool = True
    auto_rerun_on_bitflip: bool = True


@dataclass
class RunSetup:
    backend: str = "local"           # "local" | "remote"
    host: str = "localhost"
    port: int = 8765
    last_run: Optional[str] = None


@dataclass
class LayerMap:
    """Which KiCad user layer (enum name, e.g. BL_User_1) plays which role."""
    ports: Optional[str] = None
    mesh: Optional[str] = None
    notes: Optional[str] = None
    fields: Dict[str, str] = field(default_factory=dict)   # tag -> layer enum name


@dataclass
class Setup:
    version: int = SETUP_VERSION
    board_hash: Optional[str] = None
    stackup: StackupSetup = field(default_factory=StackupSetup)
    nets: NetsSetup = field(default_factory=NetsSetup)
    components: ComponentsSetup = field(default_factory=ComponentsSetup)
    ports: Dict[str, PortExtra] = field(default_factory=dict)    # fp-uuid -> extras
    sim: SimSetup = field(default_factory=SimSetup)
    run: RunSetup = field(default_factory=RunSetup)
    layers: LayerMap = field(default_factory=LayerMap)

    # --- persistence ------------------------------------------------------

    def materials(self) -> dict:
        """`materials` for the engine's /api/run and /api/mesh."""
        return {"components": self.components.default == "lumped"}

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=False) + "\n"

    @classmethod
    def from_json(cls, text: str) -> "Setup":
        raw = json.loads(text)
        if raw.get("version", 1) > SETUP_VERSION:
            raise ValueError(f"setup.json version {raw['version']} is newer than supported {SETUP_VERSION}")
        s = cls()
        s.version = raw.get("version", SETUP_VERSION)
        s.board_hash = raw.get("board_hash")
        s.stackup = StackupSetup(**raw.get("stackup", {}))
        s.nets = NetsSetup(**raw.get("nets", {}))
        s.components = ComponentsSetup(**raw.get("components", {}))
        if raw.get("version", 1) < 2 and s.components.default == "remove":
            s.components.default = "lumped"      # v1 wrote the old default, never a user choice
        s.version = SETUP_VERSION
        s.ports = {k: PortExtra(**v) for k, v in raw.get("ports", {}).items()}
        s.sim = SimSetup(**raw.get("sim", {}))
        s.run = RunSetup(**raw.get("run", {}))
        s.layers = LayerMap(**raw.get("layers", {}))
        return s

    @classmethod
    def load(cls, path: Path) -> "Setup":
        if not path.exists():
            return cls()
        return cls.from_json(path.read_text(encoding="utf-8"))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json(), encoding="utf-8")

    # --- helpers ----------------------------------------------------------

    def orphaned_ports(self, live_uuids: List[str]) -> List[str]:
        """Port UUIDs present in setup.json but no longer on the board."""
        live = set(live_uuids)
        return [u for u in self.ports if u not in live]

    def is_stale(self, board_path: Path) -> bool:
        if self.board_hash is None:
            return True
        return self.board_hash != board_file_hash(board_path)


def board_file_hash(board_path: Path) -> str:
    h = hashlib.sha256()
    with board_path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
