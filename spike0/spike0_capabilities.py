"""Spike 0 - KiCad IPC API capability check for Z2Labs EMS (docs/DESIGN.md section 2.1).

Run with KiCad (9.0.5+ / 10) open on any board, IPC server enabled
(Preferences > Plugins > Enable IPC API server):

    python spike0/spike0_capabilities.py [--keep] [--png path.png]

Mutating tests create items and remove them again right away (items created
inside an open commit are not addressable by id until the commit is pushed on
KiCad 10.0.6, so commit/drop cannot be used); the board content is unchanged
afterwards, only the undo history grows. Do not save the board afterwards if
you want to be sure (one exception: the stackup
update test is skipped unless --allow-stackup is given, because it bypasses the
commit mechanism). The result table is printed and saved as
spike0_report.json next to this script.
"""

from __future__ import annotations

import argparse
import io
import json
import platform
import struct
import sys
import time
import traceback
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kipy import KiCad  # noqa: E402
from kipy.board_types import (BoardSegment, Field, Footprint, FootprintInstance, Group, Pad,  # noqa: E402
                              PadStack, ReferenceImage)
from kipy.geometry import Vector2  # noqa: E402
from kipy.proto.board import board_commands_pb2  # noqa: E402
from kipy.proto.board.board_types_pb2 import BoardLayer  # noqa: E402

from z2ems.ipc import USER_LAYERS as USER_LAYERS_ALL, enabled_user_layers  # noqa: E402

RESULTS: dict[str, dict] = {}


def check(name: str):
    """Decorator that RUNS the check immediately (the decorated body is the test)."""
    def deco(fn):
        def run(*a, **k):
            t0 = time.time()
            try:
                detail = fn(*a, **k)
                RESULTS[name] = {"status": "PASS", "detail": detail or "", "ms": int((time.time() - t0) * 1000)}
                print(f"  PASS  {name}: {detail or ''}")
            except NotImplementedError as e:
                RESULTS[name] = {"status": "SKIP", "detail": str(e)}
                print(f"  SKIP  {name}: {e}")
            except Exception as e:  # noqa: BLE001
                RESULTS[name] = {"status": "FAIL", "detail": f"{type(e).__name__}: {e}",
                                 "trace": traceback.format_exc(limit=3)}
                print(f"  FAIL  {name}: {type(e).__name__}: {e}")
        run()
        return run
    return deco


def tiny_png(w: int = 64, h: int = 32, alpha: int = 150) -> bytes:
    """Horizontal red->blue gradient with constant alpha (no PIL dependency)."""
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        for x in range(w):
            r = int(255 * x / (w - 1)); b = 255 - r
            raw += bytes((r, 0, b, alpha))

    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="push commits instead of dropping them (leaves test items on the board)")
    ap.add_argument("--png", type=Path, help="PNG to use for the reference-image test (default: generated gradient)")
    ap.add_argument("--allow-stackup", action="store_true", help="also test UpdateBoardStackup round-trip (writes the stackup back unchanged)")
    args = ap.parse_args()

    kicad = KiCad(timeout_ms=5000)
    print(f"KiCad {kicad.get_version().full_version}  kicad-python {_kipy_version()}  {platform.platform()}")
    RESULTS["_env"] = {"kicad": kicad.get_version().full_version, "kipy": _kipy_version()}
    board = kicad.get_board()
    print(f"Board: {board.document.board_filename}\n")

    def cleanup(items):
        """Immediate-commit mode: remove test items unless --keep."""
        if not args.keep and items:
            board.remove_items(items)

    user_layers = enabled_user_layers(board)
    enabled = board.get_enabled_layers()
    test_layer = user_layers[-1] if user_layers else (
        BoardLayer.BL_Dwgs_User if BoardLayer.BL_Dwgs_User in enabled else BoardLayer.BL_F_SilkS)
    # NOTE (KiCad 10.0.6): create_items on a layer that is NOT enabled "succeeds" but returns
    # items with empty ids that never appear on the board. Always check enabled layers first.

    # ---- 6. read access -------------------------------------------------------------
    @check("6a nets + netclasses")
    def _():
        nets = board.get_nets()
        classes = board.get_netclass_for_nets(nets[:20]) if nets else {}
        return f"{len(nets)} nets, {len(classes)} netclass lookups"

    @check("6b pads/tracks/zones/footprints with layer+geometry")
    def _():
        fps = board.get_footprints(); pads = board.get_pads(); tr = board.get_tracks(); zs = board.get_zones()
        sample = ""
        if pads:
            p = pads[0]
            sample = f" pad0 net={p.net.name!r} pos=({p.position.x},{p.position.y}) layers={list(p.padstack.layers)[:2]}"
        return f"{len(fps)} fp, {len(pads)} pads, {len(tr)} tracks, {len(zs)} zones;{sample}"

    @check("6c zone filled polygons")
    def _():
        zs = board.get_zones()
        if not zs:
            raise NotImplementedError("no zones on this board")
        z = zs[0]
        polys = sum(len(v) for v in z.filled_polygons.values())
        return f"zone '{z.net.name}' filled polygons: {polys}"

    @check("6d selection read")
    def _():
        sel = board.get_selection()
        return f"{len(sel)} items selected"

    # ---- 5. stackup -----------------------------------------------------------------
    @check("5a get_stackup")
    def _():
        su = board.get_stackup()
        kinds = [L.type for L in su.layers if L.enabled]
        return f"{len(kinds)} enabled stackup layers"

    @check("5b UpdateBoardStackup round-trip")
    def _():
        if not args.allow_stackup:
            raise NotImplementedError("pass --allow-stackup to test (writes stackup back unchanged)")
        su = board.get_stackup()
        cmd = board_commands_pb2.UpdateBoardStackup()
        cmd.board.CopyFrom(board.document)
        cmd.stackup.CopyFrom(su._proto)  # noqa: SLF001
        board.client.send(cmd, board_commands_pb2.BoardStackupResponse)
        return "accepted by server"

    # ---- 2. layers -------------------------------------------------------------------
    @check("2a enabled user layers + names")
    def _():
        names = [board.get_layer_name(l) for l in user_layers]
        return f"{len(user_layers)} enabled: {names[:6]}{'...' if len(names) > 6 else ''}"

    @check("2c set_enabled_layers: enable one more User.N layer (additive, then restore)")
    def _():
        copper = board.get_copper_layer_count()
        before = list(board.get_enabled_layers())
        free = [l for l in USER_LAYERS_ALL if l not in before]
        if not free:
            raise NotImplementedError("all 45 user layers already enabled")
        non_copper = [l for l in before if BoardLayer.Name(l) not in ("BL_F_Cu", "BL_B_Cu") and not BoardLayer.Name(l).startswith("BL_In")]
        after = board.set_enabled_layers(copper, non_copper + [free[0]])
        ok = free[0] in after
        if not args.keep:
            board.set_enabled_layers(copper, non_copper)
        return f"enabled {BoardLayer.Name(free[0])}: {ok}; copper count kept={board.get_copper_layer_count() == copper}"

    @check("2b get_layer_by_name('Z2EMS.Ports') (expect miss on fresh board)")
    def _():
        try:
            l = board.get_layer_by_name("Z2EMS.Ports")
            return f"found -> {BoardLayer.Name(l)}" + ("  (BL_UNDEFINED = not found, no exception raised)" if l == 0 else "")
        except Exception as e:  # noqa: BLE001
            return f"not found ({type(e).__name__}) - rename API absent in kipy 0.8.0, file-patch workaround applies"

    # ---- 3. segments + group + lock --------------------------------------------------
    @check("3 segments -> group -> lock -> delete")
    def _():
        if True:
            segs = []
            for i in range(5):
                s = BoardSegment(); s.layer = test_layer
                s.start = Vector2.from_xy_mm(10 + i, 10); s.end = Vector2.from_xy_mm(10 + i, 30)
                s.attributes.stroke.width = 20000
                segs.append(s)
            created = board.create_items(segs)
            g = Group(); g._proto.name = "Z2EMS spike0"; g.items = created  # noqa: SLF001
            (g2,) = board.create_items([g])
            for it in created:
                it.locked = True
            board.update_items(created)
            back = [grp for grp in board.get_groups() if grp.name == "Z2EMS spike0"]
            n_members = len(back[0].items) if back else -1
            locked = all(it.locked for it in board.get_items_by_id([c.id for c in created]))
            cleanup(created + [g2])
            return f"created 5, group members={n_members}, locked={locked}, removed ok"

    # ---- 4. reference image ----------------------------------------------------------
    @check("4 reference image on user layer")
    def _():
        data = args.png.read_bytes() if args.png else tiny_png()
        if True:
            img = ReferenceImage(); img.layer = test_layer; img.image_data = data
            img.image_scale = 1.0; img.position = Vector2.from_xy_mm(50, 50); img.locked = True
            (c,) = board.create_items([img])
            imgs = board.get_reference_images()
            bb = board.get_item_bounding_box(c)
            cleanup([c])
            return f"created on {BoardLayer.Name(test_layer)}, {len(imgs)} ref images on board, bbox={bb}"

    # ---- 1. footprint with custom fields ---------------------------------------------
    @check("1a read custom fields of existing footprints")
    def _():
        fps = board.get_footprints()
        if not fps:
            raise NotImplementedError("no footprints on board")
        names = set()
        for fp in fps[:30]:
            for it in fp.texts_and_fields:
                if isinstance(it, Field):
                    names.add(it.name)
        return f"field names seen: {sorted(names)[:12]}"

    @check("1b create footprint instance with 2 pads + custom field, update field, remove")
    def _():
        if True:
            fp = FootprintInstance()
            fp.position = Vector2.from_xy_mm(40, 40)
            fp.layer = BoardLayer.BL_F_Cu
            fp.definition.id.library = "z2ems"; fp.definition.id.name = "Z2EMS_PORT"
            fp.reference_field.text.value = "P99"; fp.value_field.text.value = "Z2EMS_PORT"
            for num, dx in (("1", 0.0), ("2", 0.8)):
                pad = Pad(); pad.number = num
                pad.position = Vector2.from_xy_mm(40 + dx, 40)
                pad.padstack.layers = [BoardLayer.BL_F_Cu]
                fp.definition.add_item(pad)
            f = Field(); f.name = "Z2EMS.Z0"; f.text.value = "50"; f.layer = BoardLayer.BL_F_Fab
            f.text.position = Vector2.from_xy_mm(40, 41)
            fp.definition.add_item(f)
            (c,) = board.create_items([fp])
            # update custom field
            got = {it.name: it for it in c.texts_and_fields if isinstance(it, Field)}
            ok_read = "Z2EMS.Z0" in got
            if ok_read:
                got["Z2EMS.Z0"].text.value = "75"
                board.update_items([c])
                c2 = board.get_items_by_id([c.id])[0]
                vals = {it.name: it.text.value for it in c2.texts_and_fields if isinstance(it, Field)}
                ok_write = vals.get("Z2EMS.Z0") == "75"
            else:
                ok_write = False
            cleanup([c])
            return f"created with {len(c.definition.pads)} pads; custom field read={ok_read} write={ok_write}"

    # ---- summary ---------------------------------------------------------------------
    out = Path(__file__).with_name("spike0_report.json")
    out.write_text(json.dumps(RESULTS, indent=2), encoding="utf-8")
    fails = [k for k, v in RESULTS.items() if isinstance(v, dict) and v.get("status") == "FAIL"]
    print(f"\n{len(RESULTS) - 1} checks, {len(fails)} failed -> {out}")
    return 1 if fails else 0


def _kipy_version() -> str:
    try:
        import importlib.metadata as m
        return m.version("kicad-python")
    except Exception:  # noqa: BLE001
        return "?"


if __name__ == "__main__":
    sys.exit(main())
