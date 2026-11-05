"""Port definitions: a + terminal on the RF copper and a - reference to ground. Pure Python.

A port is
  plus   one pad (footprint reference + pad number) on an outer copper layer: the "hot" point;
  minus  either ground pads on the same layer (an edge connector's GND pads, an IC's neighbouring GND pins):
         the engine puts one lumped sub-port across each gap, in parallel ("gap" port);
         or a reference plane under the pad ("vertical" port, e.g. an antenna feed with no ground copper
         beside it); an empty minus asks the engine for the nearest reference copper under the pad.

Ports live in setup.json ("port_defs") and are drawn on the board as markers on the Z2EMS.Ports user layer.
They never touch copper, the netlist or the schematic, so DRC, ERC and the BOM are unaffected.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional


@dataclass
class Terminal:
    ref: str
    pad: str
    net: str = ""
    layer: str = ""

    def label(self) -> str:
        return f"{self.ref} pad {self.pad}"


@dataclass
class PortDef:
    n: int
    plus: Optional[Terminal] = None
    minus_pads: List[Terminal] = field(default_factory=list)
    minus_plane: Optional[str] = None
    z0: float = 50.0
    excite: bool = True
    source: str = "user"            # "user" | "suggested"

    # --- state -----------------------------------------------------------------
    @property
    def name(self) -> str:
        return f"P{self.n}"

    @property
    def kind(self) -> str:
        if self.minus_pads:
            return "gap"
        return "vertical"

    def problems(self) -> List[str]:
        out = []
        if self.plus is None:
            out.append("no + terminal")
        if not self.minus_pads and not self.minus_plane:
            out.append("no reference (-)")
        if self.plus is not None:
            for m in self.minus_pads:
                if m.layer and self.plus.layer and m.layer != self.plus.layer:
                    out.append(f"- pad {m.label()} is on {m.layer}, + on {self.plus.layer}")
                if m.ref == self.plus.ref and m.pad == self.plus.pad:
                    out.append("+ and - are the same pad")
                if m.net and self.plus.net and m.net == self.plus.net:
                    out.append(f"- pad {m.label()} is on the signal net {m.net}")
        if not (1.0 <= self.z0 <= 1000.0):
            out.append(f"Z0 {self.z0:g} ohm out of range")
        return out

    @property
    def complete(self) -> bool:
        return not self.problems()

    # --- text ------------------------------------------------------------------
    def plus_text(self) -> str:
        if self.plus is None:
            return "pick a + point..."
        net = f"  ·  net {self.plus.net}" if self.plus.net else ""
        lay = f"  ·  {self.plus.layer}" if self.plus.layer else ""
        return f"{self.plus.label()}{net}{lay}"

    def minus_text(self) -> str:
        if self.minus_pads:
            refs: Dict[str, List[str]] = {}
            for m in self.minus_pads:
                refs.setdefault(m.ref, []).append(m.pad)
            nets = sorted({m.net for m in self.minus_pads if m.net})
            pads = ";  ".join(f"{r} pad{'s' if len(p) > 1 else ''} {', '.join(_compact(p))}" for r, p in refs.items())
            return pads + (f"  ·  {', '.join(nets)}" if nets else "")
        if self.minus_plane:
            return f"plane below: {self.minus_plane}"
        return "pick a - point..."

    def status_text(self) -> str:
        p = self.problems()
        return "ok" if not p else p[0]

    # --- engine ----------------------------------------------------------------
    def to_spec(self) -> dict:
        """Port spec for the engine's /api/mesh and /api/run (kicad_sim.port_from_spec, v2)."""
        assert self.plus is not None
        sp = {"plus": {"ref": self.plus.ref, "pad": self.plus.pad, "layer": self.plus.layer or None},
              "z0": self.z0, "name": self.name}
        if self.minus_pads:
            sp["minus"] = {"pads": [{"ref": m.ref, "pad": m.pad} for m in self.minus_pads]}
        elif self.minus_plane:
            sp["minus"] = {"plane": self.minus_plane}
        return sp

    # --- json ------------------------------------------------------------------
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PortDef":
        return cls(n=int(d["n"]),
                   plus=Terminal(**d["plus"]) if d.get("plus") else None,
                   minus_pads=[Terminal(**m) for m in d.get("minus_pads", [])],
                   minus_plane=d.get("minus_plane"),
                   z0=float(d.get("z0", 50.0)), excite=bool(d.get("excite", True)),
                   source=d.get("source", "user"))


def _compact(pads: List[str]) -> List[str]:
    """['2','3','4','5'] -> ['2-5'] for consecutive numbers; other names as they are."""
    nums = sorted(int(p) for p in pads if p.isdigit())
    other = [p for p in pads if not p.isdigit()]
    out, i = [], 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        out.append(f"{nums[i]}-{nums[j]}" if j - i >= 2 else ", ".join(str(v) for v in nums[i:j + 1]))
        i = j + 1
    return out + other


def renumber(ports: List[PortDef]) -> List[PortDef]:
    for i, p in enumerate(ports):
        p.n = i + 1
    return ports


def runnable(ports: List[PortDef]) -> List[PortDef]:
    """The complete ports, renumbered P1..Pn in order (what the engine gets)."""
    return [p for p in ports if p.complete]
