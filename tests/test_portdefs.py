"""Port definitions (+ pad, - pads / plane), setup.json round trip, engine spec, marker geometry."""

import json
from types import SimpleNamespace as NS

from z2ems.model import Setup
from z2ems.portdefs import PortDef, Terminal, runnable
from z2ems import port_board as pb


def sma():
    return PortDef(n=1, plus=Terminal("J1", "1", "ANT", "F.Cu"),
                   minus_pads=[Terminal("J1", p, "GND", "F.Cu") for p in ("2", "3", "4", "5")])


def test_kinds_and_texts():
    p = sma()
    assert p.kind == "gap" and p.complete and p.status_text() == "ok"
    assert p.minus_text() == "J1 pads 2-5  ·  GND"
    v = PortDef(n=2, plus=Terminal("ANT1", "1", "RF", "F.Cu"), minus_plane="In1.Cu")
    assert v.kind == "vertical" and v.complete and v.minus_text() == "plane below: In1.Cu"
    q = PortDef(n=3, plus=Terminal("U3", "7", "RF_N2", "F.Cu"))
    assert not q.complete and q.status_text() == "no reference (-)"
    assert [x.name for x in runnable([p, v, q])] == ["P1", "P2"]


def test_bad_references():
    p = PortDef(n=1, plus=Terminal("J1", "1", "ANT", "F.Cu"), minus_pads=[Terminal("J1", "1", "ANT", "F.Cu")])
    assert "+ and - are the same pad" in p.problems()
    p = PortDef(n=1, plus=Terminal("J1", "1", "ANT", "F.Cu"), minus_pads=[Terminal("J1", "2", "GND", "B.Cu")])
    assert any("is on B.Cu" in x for x in p.problems())


def test_spec_v2():
    assert sma().to_spec() == {"plus": {"ref": "J1", "pad": "1", "layer": "F.Cu"}, "z0": 50.0, "name": "P1",
                               "minus": {"pads": [{"ref": "J1", "pad": p} for p in ("2", "3", "4", "5")]}}
    v = PortDef(n=2, plus=Terminal("ANT1", "1", "RF", "F.Cu"), minus_plane="In1.Cu", z0=75)
    assert v.to_spec()["minus"] == {"plane": "In1.Cu"} and v.to_spec()["z0"] == 75


def test_setup_roundtrip(tmp_path):
    s = Setup()
    s.port_defs = [sma(), PortDef(n=2, plus=Terminal("U3", "7", "RF", "F.Cu"))]
    f = tmp_path / "setup.json"
    s.save(f)
    t = Setup.load(f)
    assert [p.to_dict() for p in t.port_defs] == [p.to_dict() for p in s.port_defs]
    assert json.loads(f.read_text())["version"] == 3


def test_marker_geometry():
    g = pb.marker_geometry(sma(), (2.0, 6.0), [(2.0, 3.0), (2.0, 9.0)])
    assert len(g["circles"]) == 3 and len(g["segments"]) == 6          # two arrows of three strokes
    assert any(t[2] == "P1 50 Ω" for t in g["texts"])
    v = PortDef(n=2, plus=Terminal("ANT1", "1", "RF", "F.Cu"), minus_plane="In1.Cu")
    g = pb.marker_geometry(v, (10.0, 6.0), [])
    assert any(t[2] == "In1.Cu" for t in g["texts"]) and len(g["circles"]) == 1


# --- a fake board for the selection logic ---------------------------------------------------------

def pad(num, net, x, y, fid):
    return NS(number=num, net=NS(name=net), position=NS(x=int(x * 1e6), y=int(y * 1e6)),
              padstack=NS(layers=[3]), id=NS(value=f"{fid}-{num}"), parent=NS(value=fid))


def fp(ref, pads, fid):
    return NS(reference_field=NS(text=NS(value=ref)), definition=NS(pads=pads), id=NS(value=fid))


class Board:
    def __init__(self, fps, sel):
        self.fps, self.sel = fps, sel

    def get_footprints(self):
        return self.fps

    def get_selection(self):
        return self.sel

    def get_layer_name(self, l):
        return {3: "F.Cu", 4: "In1.Cu", 5: "In2.Cu", 6: "B.Cu"}[l]

    def get_enabled_layers(self):
        return [3, 4, 5, 6]

    def get_zones(self):
        return [NS(net=NS(name="GND"), layers=[4])]


def test_port_from_selected_connector():
    j = fp("J1", [pad("1", "ANT", 1, 6, "j"), pad("2", "GND", 1, 3, "j"), pad("2", "GND", 1, 9, "j")], "j")
    p, msg = pb.port_from_selection(Board([j], [j]), 1)
    assert p.plus.pad == "1" and [m.pad for m in p.minus_pads] == ["2"] and p.complete, msg


def test_port_from_selected_ic_pin_takes_neighbour_gnd():
    pins = [pad(str(i), "GND" if i in (6, 8, 1, 14) else f"N{i}", 10, i * 0.65, "u") for i in range(1, 15)]
    pins[6].net = NS(name="RF")                      # pin 7
    u = fp("U3", pins, "u")
    p, msg = pb.port_from_selection(Board([u], [pins[6]]), 3)
    assert p.plus.pad == "7" and sorted(m.pad for m in p.minus_pads) == ["6", "8"], msg


def test_port_without_ground_pads_gets_plane_below():
    a = fp("ANT1", [pad("1", "RF", 10, 6, "a")], "a")
    p, msg = pb.port_from_selection(Board([a], [a]), 2)
    assert p.kind == "vertical" and p.minus_plane == "In1.Cu" and p.complete, msg


def test_shield_pads_sharing_one_number_are_counted():
    p = PortDef(1, plus=Terminal("J1A", "1", "RF1", "F.Cu"), minus_pads=[Terminal("J1A", "2", "GND", "F.Cu", count=4)])
    assert "4 pads" in p.minus_text()
    assert p.to_spec()["minus"] == {"pads": [{"ref": "J1A", "pad": "2"}]}      # the engine takes every pad "2"
    assert PortDef.from_dict(p.to_dict()).minus_pads[0].count == 4
