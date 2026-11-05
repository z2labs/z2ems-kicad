"""Tests that run without KiCad (pure-Python parts)."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from z2ems.model import PortExtra, Setup, SETUP_VERSION  # noqa: E402


def test_setup_roundtrip(tmp_path):
    s = Setup()
    s.nets.ground = ["GND"]
    s.ports["abcd-1234"] = PortExtra(deembed_mm=1.5, note="SMA J1")
    s.sim.excite = ["P1", "P2"]
    s.layers.ports = "BL_User_1"
    p = tmp_path / "setup.json"
    s.save(p)
    s2 = Setup.load(p)
    assert s2.version == SETUP_VERSION
    assert s2.nets.ground == ["GND"]
    assert s2.ports["abcd-1234"].deembed_mm == 1.5
    assert s2.sim.excite == ["P1", "P2"]
    assert s2.layers.ports == "BL_User_1"
    assert json.loads(p.read_text())["version"] == SETUP_VERSION


def test_missing_file_gives_defaults(tmp_path):
    s = Setup.load(tmp_path / "nope.json")
    assert s.sim.f_stop_hz == 6e9
    assert s.run.backend == "local"


def test_orphans():
    s = Setup()
    s.ports = {"a": PortExtra(), "b": PortExtra()}
    assert s.orphaned_ports(["a"]) == ["b"]


def test_newer_version_rejected():
    with pytest.raises(ValueError):
        Setup.from_json(json.dumps({"version": SETUP_VERSION + 1}))


def test_patch_layer_names():
    # imported lazily: layers.py imports kipy, which is installed in dev but KiCad is not needed
    from z2ems.layers import patch_layer_names
    pcb = '''(kicad_pcb (version 20241229)
  (layers
    (0 "F.Cu" signal)
    (2 "B.Cu" signal)
    (36 "B.SilkS" user "B.Silkscreen")
    (50 "User.1" user)
    (52 "User.2" user "OldName")
    (54 "User.3" user)
  )
)'''
    out = patch_layer_names(pcb, {"User.1": "Z2EMS.Ports", "User.2": "Z2EMS.Mesh"})
    assert '(50 "User.1" user "Z2EMS.Ports")' in out
    assert '(52 "User.2" user "Z2EMS.Mesh")' in out
    assert '(54 "User.3" user)' in out                 # untouched
    assert '(36 "B.SilkS" user "B.Silkscreen")' in out  # non-user layers untouched
    # idempotent
    assert patch_layer_names(out, {"User.1": "Z2EMS.Ports"}) == out


def test_ports_regexes():
    from z2ems.ports import GND_NET_RE, _RF_RE
    for n in ["GND", "AGND", "GND_RF", "RFGND", "gnd2", "VSS", "0V"]:
        assert GND_NET_RE.match(n), n
    for n in ["RF_IN", "GND_SENSE_EN", "VCC"]:
        assert not GND_NET_RE.match(n), n
    for t in ["Connector_Coaxial:SMA_Amphenol", "U.FL-R-SMT", "uFL", "MMCX_Edge", "RP-SMA"]:
        assert _RF_RE.search(t), t
    assert not _RF_RE.search("Resistor_SMD:R_0402")


def test_png_header_helpers():
    from z2ems.overlays import _png_height, _png_width
    import struct, zlib
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 123, 45, 8, 6, 0, 0, 0))
    assert _png_width(png) == 123 and _png_height(png) == 45


def test_non_connector_refs():
    from z2ems.ports import _NON_CONNECTOR_REF
    for r in ["D201", "C12", "U7", "IC502", "FB3", "TP1"]:
        assert _NON_CONNECTOR_REF.match(r), r
    for r in ["J1", "SMA_IN_501", "CON3", "LO_AMP_OUT1101", "P4", "XS1"]:  # X1 = crystal, excluded
        assert not _NON_CONNECTOR_REF.match(r), r


def test_patch_stackup_roundtrip():
    from z2ems.stackup import load_preset
    from z2ems.stackup_file import patch_stackup, render_stackup_block, find_block, board_copper_count
    preset = load_preset("JLC081611-2116")
    blk = render_stackup_block(preset)
    assert blk.count('(type "copper")') == 8
    assert blk.count("addsublayer") == 2
    assert '"dielectric 7"' in blk and '"dielectric 8"' not in blk
    pcb = '(kicad_pcb (version 20241229)\n\t(layers (0 "F.Cu" signal) (1 "In1.Cu" signal) (2 "In2.Cu" signal) (3 "In3.Cu" signal) (4 "In4.Cu" signal) (5 "In5.Cu" signal) (6 "In6.Cu" signal) (2 "B.Cu" signal))\n\t(setup\n\t\t(stackup\n\t\t\t(layer "F.Cu" (type "copper") (thickness 0.035))\n\t\t\t(copper_finish "ENIG")\n\t\t\t(dielectric_constraints yes)\n\t\t)\n\t\t(pad_to_mask_clearance 0)\n\t)\n)\n'
    assert board_copper_count(pcb) == 8
    out = patch_stackup(pcb, preset)
    assert '(copper_finish "ENIG")' in out and "(dielectric_constraints yes)" in out
    assert "(pad_to_mask_clearance 0)" in out
    assert out.count("(stackup") == 1
    # balanced
    i = out.index("(kicad_pcb"); assert find_block(out, i) == len(out.rstrip())


def test_jlc_table_import():
    from z2ems.jlc_import import parse_jlc_table
    txt = """layer	Material Type	Thickness
Layer	Copper	0.035mm
Prepreg	2116*1	0.109mm
inner Layer	Copper	0.03mm
Core	Core	0.25mm
inner Layer	Copper	0.03mm
Prepreg	2116*1	0.109mm
Prepreg	2116*1	0.109mm
inner Layer	Copper	0.03mm
Core	Core	0.25mm
inner Layer	Copper	0.03mm
Prepreg	2116*1	0.109mm
Prepreg	2116*1	0.109mm
inner Layer	Copper	0.03mm
Core	Core	0.25mm
inner Layer	Copper	0.03mm
Prepreg	2116*1	0.109mm
Layer	Copper	0.035mm"""
    p = parse_jlc_table(txt, "JLC081611-2116-test")
    assert p["copper_layers"] == 8
    pre = [l for l in p["layers"] if l["type"] == "prepreg"]
    assert [l.get("sublayers", 1) for l in pre] == [1, 2, 2, 1]
    assert [l["name"] for l in p["layers"] if l["type"] == "copper"][1] == "In1.Cu"
    assert abs(p["total_thickness_mm"] - 1.654) < 1e-6


def test_components_default_lumped_and_v1_migration():
    import json
    from z2ems.model import Setup
    assert Setup().materials() == {"components": True}
    v1 = json.dumps({"version": 1, "components": {"default": "remove", "overrides": {}}})
    s = Setup.from_json(v1)
    assert s.components.default == "lumped" and s.version == 3
    v2 = json.dumps({"version": 2, "components": {"default": "remove", "overrides": {}}})
    assert Setup.from_json(v2).materials() == {"components": False}   # an explicit v2 choice stays


def test_ground_nets_with_rule_area_zone():
    """A rule area (keepout) is a zone without a net: kipy returns net=None (cw_trainer board, 2026-11-05)."""
    from types import SimpleNamespace as NS
    from z2ems.ports import detect_ground_nets, net_name
    board = NS(get_nets=lambda: [NS(name="GND"), NS(name="AGND"), NS(name="RF_N1")],
               get_zones=lambda: [NS(net=None), NS(net=NS(name="AGND")), NS(net=NS(name=""))])
    assert detect_ground_nets(board) == ["AGND", "GND"]
    assert net_name(NS(net=None)) == "" and net_name(NS()) == ""
