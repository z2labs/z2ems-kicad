"""Board overlays: mesh lines (grouped + locked segments) and field heat-maps
(PNG reference images). DESIGN section 4.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

from kipy.board import Board
from kipy.board_types import BoardSegment, Group, ReferenceImage
from kipy.geometry import Vector2
from kipy.proto.board.board_types_pb2 import BoardLayer

from .ipc import nm

MESH_GROUP_NAME = "Z2EMS mesh"
FIELD_LOCK = True


# --- mesh ----------------------------------------------------------------------

def draw_mesh(board: Board, layer: int, x_lines_mm: Sequence[float], y_lines_mm: Sequence[float],
              bbox_mm: Tuple[float, float, float, float], line_width_mm: float = 0.02,
              group_name: str = MESH_GROUP_NAME, message: str = "Z2EMS: draw mesh preview") -> Group:
    """Draw x/y mesh lines spanning `bbox_mm` (x0, y0, x1, y1) on `layer`, grouped and locked.
    Any previous mesh group is removed first so the mesh is always regenerated as a unit."""
    clear_mesh(board, group_name)
    x0, y0, x1, y1 = bbox_mm
    segs: List[BoardSegment] = []
    for x in x_lines_mm:
        segs.append(_segment(layer, (x, y0), (x, y1), line_width_mm))
    for y in y_lines_mm:
        segs.append(_segment(layer, (x0, y), (x1, y), line_width_mm))

    # Immediate mode on purpose: items created inside an open begin_commit() are not addressable by
    # id until the commit is pushed (Spike 0 pitfall b), so the group could not reference them.
    # Large meshes are created in chunks to keep single IPC messages small.
    created: List[BoardSegment] = []
    for i in range(0, len(segs), 500):
        created += list(board.create_items(segs[i:i + 500]))
    grp = Group()
    grp._proto.name = group_name   # noqa: SLF001 - no public setter in 0.8.0
    grp.items = created
    (grp,) = board.create_items([grp])
    for it in created:
        it.locked = True
    for i in range(0, len(created), 500):
        board.update_items(created[i:i + 500])
    return grp


def clear_mesh(board: Board, group_name: str = MESH_GROUP_NAME) -> int:
    """Remove the mesh group and its members. Returns number of items removed."""
    removed = 0
    for grp in board.get_groups():
        if grp.name != group_name:
            continue
        members = list(grp.items)
        board.remove_items(members + [grp])
        removed += len(members)
    return removed


def _segment(layer: int, a: Tuple[float, float], b: Tuple[float, float], width_mm: float) -> BoardSegment:
    s = BoardSegment()
    s.layer = layer
    s.start = Vector2.from_xy_mm(*a)
    s.end = Vector2.from_xy_mm(*b)
    s.attributes.stroke.width = nm(width_mm)
    return s


# --- field overlay -------------------------------------------------------------

def place_field_overlay(board: Board, layer: int, png_path: Path, origin_mm: Tuple[float, float],
                        width_mm: float, px_width: int, lock: bool = FIELD_LOCK,
                        message: str = "Z2EMS: field overlay") -> ReferenceImage:
    """Place a PNG heat-map as a reference image.

    The PNG must be rendered in board coordinates: `origin_mm` is the board
    position of the image's top-left pixel and `width_mm / px_width` its pixel
    pitch. Opacity is baked into the PNG alpha channel (the IPC API exposes no
    opacity for reference images). The image is centred on its own bounding box
    in KiCad, so position = origin + half size.
    """
    data = Path(png_path).read_bytes()
    img = ReferenceImage()
    img.layer = layer
    img.image_data = data
    # KiCad renders bitmaps at a nominal 300 DPI before scaling: scale = mm_per_px / (25.4/300)
    mm_per_px = width_mm / float(px_width)
    img.image_scale = mm_per_px / (25.4 / 300.0)
    px_height = _png_height(data)
    height_mm = px_height * mm_per_px
    img.position = Vector2.from_xy_mm(origin_mm[0] + width_mm / 2.0, origin_mm[1] + height_mm / 2.0)
    img.locked = lock
    commit = board.begin_commit()
    try:
        (created,) = board.create_items([img])
        board.push_commit(commit, message)
    except Exception:
        board.drop_commit(commit)
        raise
    return created


def clear_field_overlays(board: Board, layers: Iterable[int]) -> int:
    layers = set(layers)
    victims = [im for im in board.get_reference_images() if im.layer in layers]
    if not victims:
        return 0
    commit = board.begin_commit()
    try:
        board.remove_items(victims)
        board.push_commit(commit, "Z2EMS: clear field overlays")
    except Exception:
        board.drop_commit(commit)
        raise
    return len(victims)


def _png_height(data: bytes) -> int:
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG file")
    return int.from_bytes(data[20:24], "big")


def _png_width(data: bytes) -> int:
    return int.from_bytes(data[16:20], "big")
