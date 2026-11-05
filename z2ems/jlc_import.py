"""Turn a pasted JLCPCB "Layer Stackup" table into a preset JSON.

Paste the text of the order-dialog table (copy the rows, one per line), e.g.

    Layer        Copper   0.035mm
    Prepreg      2116*1   0.109mm
    inner Layer  Copper   0.03mm
    Core         Core     0.25mm
    ...

Consecutive prepreg rows are merged into one dielectric with sub-layers, as
KiCad models them. Dk/Df come from a per-material table (JLC impedance
calculator notes) and are marked in `source` so they can be reviewed.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List

MATERIAL_DK: Dict[str, tuple[float, float]] = {
    # material: (epsilon_r, loss_tangent) - JLCPCB calculator notes, ~1 GHz; verify before relying on them
    "7628": (4.4, 0.02),
    "2116": (4.16, 0.02),
    "3313": (4.05, 0.02),
    "2313": (4.05, 0.02),
    "1080": (3.91, 0.02),
    "106": (3.75, 0.02),
    "core": (4.6, 0.02),
}
SOLDERMASK = {"type": "soldermask", "thickness_mm": 0.0152, "epsilon_r": 3.8, "loss_tangent": 0.02}

_ROW = re.compile(r"^\s*(inner\s+layer|layer|prepreg|core)\s+(\S+)\s+([\d.]+)\s*mm", re.IGNORECASE)


def parse_jlc_table(text: str, preset_id: str, description: str = "") -> dict:
    rows: List[dict] = []
    for line in text.splitlines():
        m = _ROW.match(line)
        if not m:
            continue
        kind, mat, th = m.group(1).lower(), m.group(2), float(m.group(3))
        if kind in ("layer", "inner layer"):
            rows.append({"type": "copper", "thickness_mm": th})
        elif kind == "prepreg":
            mm_ = re.match(r"([A-Za-z0-9]+)(?:\*(\d+))?", mat)
            material = mm_.group(1) if mm_ else mat
            count = int(mm_.group(2)) if mm_ and mm_.group(2) else 1
            er, df = MATERIAL_DK.get(material, (4.3, 0.02))
            for _ in range(count):
                rows.append({"type": "prepreg", "material": material, "thickness_mm": th,
                             "epsilon_r": er, "loss_tangent": df})
        elif kind == "core":
            er, df = MATERIAL_DK["core"]
            rows.append({"type": "core", "material": "NP-155F", "thickness_mm": th,
                         "epsilon_r": er, "loss_tangent": df})
    if not rows:
        raise ValueError("no stackup rows recognised (expected 'Layer Copper 0.035mm' style lines)")

    # merge consecutive identical prepregs into sublayers
    merged: List[dict] = []
    for r in rows:
        if (merged and r["type"] == "prepreg" and merged[-1]["type"] == "prepreg"
                and merged[-1].get("material") == r.get("material")
                and abs(merged[-1]["thickness_mm"] - r["thickness_mm"]) < 1e-9):
            merged[-1]["sublayers"] = merged[-1].get("sublayers", 1) + 1
        else:
            merged.append(dict(r))

    cu = [r for r in merged if r["type"] == "copper"]
    n = len(cu)
    if n < 2 or n % 2:
        raise ValueError(f"found {n} copper layers - expected an even number >= 2")
    names = ["F.Cu"] + [f"In{i}.Cu" for i in range(1, n - 1)] + ["B.Cu"]
    for r, name in zip(cu, names):
        r["name"] = name
    total = sum(r["thickness_mm"] * r.get("sublayers", 1) for r in merged)
    return {
        "id": preset_id,
        "fab": "JLCPCB",
        "description": description or f"JLCPCB {n}-layer, imported from order-dialog table",
        "copper_layers": n,
        "total_thickness_mm": round(total, 3),
        "layers": [dict(SOLDERMASK)] + merged + [dict(SOLDERMASK)],
        "source": "Thicknesses pasted from the JLCPCB 'Layer Stackup' table; Dk/Df from the built-in material table (verify).",
        "spec_frequency_hz": 1e9,
    }


def save_preset(preset: dict, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    fn = re.sub(r"[^A-Za-z0-9._-]+", "-", preset["id"]).lower() + ".json"
    p = directory / fn
    p.write_text(json.dumps(preset, indent=2) + "\n", encoding="utf-8")
    return p
