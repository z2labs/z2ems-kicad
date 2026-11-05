"""Thin helpers around kicad-python (kipy) for the Z2Labs EMS plugin.

Everything that touches a live KiCad instance goes through here so the rest of
the package can be unit-tested without KiCad running.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional

from kipy import KiCad
from kipy.board import Board
from kipy.errors import ApiError, ConnectionError as KiConnectionError
from kipy.board_types import Field, FootprintInstance
from kipy.proto.board.board_types_pb2 import BoardLayer

from . import FIELD_PREFIX, FIELD_ROLE, PORT_FOOTPRINT_NAME

NM_PER_MM = 1_000_000


def mm(nm: int) -> float:
    return nm / NM_PER_MM


def nm(mm_value: float) -> int:
    return int(round(mm_value * NM_PER_MM))


@dataclass
class Session:
    kicad: KiCad
    board: Board

    @property
    def board_path(self) -> Path:
        """Absolute path of the open .kicad_pcb (from the document specifier)."""
        doc = self.board.document
        bf = Path(doc.board_filename) if doc.board_filename else None
        if bf is not None and bf.is_absolute():
            return bf
        base = Path(doc.project.path) if doc.project.path else Path(os.getcwd())
        if base.suffix == ".kicad_pro":
            base = base.parent
        return base / bf if bf is not None else base / (doc.project.name + ".kicad_pcb")

    @property
    def sidecar_dir(self) -> Path:
        from . import SIDECAR_SUFFIX
        p = self.board_path
        return p.with_name(p.stem + SIDECAR_SUFFIX)


class NotReadyError(RuntimeError):
    """KiCad is reachable but no board is loaded yet / it keeps answering 'busy'."""


# Transient API replies seen live on KiCad 10.0.7 (macOS soak, 2026-10-17):
#   "KiCad is not ready to reply"   - first seconds after the socket appears
#   "KiCad is busy and cannot ..."  - zone fill after load, modal dialog open
#   empty board_filename            - socket up, document not registered yet
TRANSIENT_MARKS = ("busy", "not ready")


def _is_busy(e: BaseException) -> bool:
    """True for any transient 'try again shortly' API error."""
    return isinstance(e, ApiError) and any(m in str(e).lower() for m in TRANSIENT_MARKS)


class BusyRetryBoard:
    """Proxy around kipy.Board: every method call is retried while KiCad answers
    'KiCad is busy ...' (zone fill after load, modal dialog, file still loading).

    Live soak on macOS 2026-10-17: a single busy reply from get_zones() on an
    8-layer board during zone fill turned into a traceback dialog for the user.
    """

    def __init__(self, board: Board, total_s: float = 20.0, first_delay: float = 0.3):
        object.__setattr__(self, "_b", board)
        object.__setattr__(self, "_total", total_s)
        object.__setattr__(self, "_delay", first_delay)

    def __getattr__(self, name):
        attr = getattr(self._b, name)
        if not callable(attr):
            return attr

        def wrapped(*a, **k):
            t0 = time.time(); delay = self._delay; last = None
            while True:
                try:
                    return attr(*a, **k)
                except ApiError as e:
                    if not _is_busy(e) or time.time() - t0 > self._total:
                        raise
                    last = e
                    time.sleep(delay); delay = min(delay * 1.6, 2.0)
        return wrapped

    def __setattr__(self, name, value):
        setattr(self._b, name, value)

    def __repr__(self):
        return f"BusyRetryBoard({self._b!r})"


def connect(timeout_ms: int = 5000, ready_s: float = 25.0) -> Session:
    """Connect to the running KiCad instance and wait until a board is really
    loaded (the API socket comes up before the document is open) and KiCad is
    not busy. Raises NotReadyError with a human message after `ready_s`."""
    t0 = time.time(); delay = 0.4; last: BaseException | None = None
    while True:
        try:
            kicad = KiCad(timeout_ms=timeout_ms)
            board = kicad.get_board()
            if not board.document.board_filename:
                raise NotReadyError("no board filename yet")
            board.get_nets()          # cheap probe; raises ApiError(busy) while loading
            return Session(kicad=kicad, board=BusyRetryBoard(board))  # type: ignore[arg-type]
        except (KiConnectionError, NotReadyError) as e:
            last = e
        except ApiError as e:
            if not _is_busy(e):
                raise
            last = e
        except Exception as e:  # noqa: BLE001  (kipy raises plain Exception for 'no document')
            if "document" not in str(e).lower() and "board" not in str(e).lower():
                raise
            last = e
        if time.time() - t0 > ready_s:
            raise NotReadyError(
                "KiCad is reachable but no board is ready for the API "
                f"(still loading, busy, or no board open). Last: {last}")
        time.sleep(delay); delay = min(delay * 1.5, 2.0)


# --- fields -------------------------------------------------------------------

def footprint_fields(fp: FootprintInstance) -> dict[str, str]:
    """All fields of a footprint instance as {name: text}."""
    out: dict[str, str] = {}
    for item in fp.texts_and_fields:
        if isinstance(item, Field):
            out[item.name] = item.text.value
    return out


def z2ems_fields(fp: FootprintInstance) -> dict[str, str]:
    return {k: v for k, v in footprint_fields(fp).items() if k.startswith(FIELD_PREFIX)}


def is_port_footprint(fp: FootprintInstance) -> bool:
    fields = footprint_fields(fp)
    if fields.get(FIELD_ROLE, "").lower() == "port":
        return True
    try:
        return fp.definition.id.name == PORT_FOOTPRINT_NAME
    except Exception:
        return False


def find_port_footprints(board: Board) -> List[FootprintInstance]:
    return [fp for fp in board.get_footprints() if is_port_footprint(fp)]


# --- layers -------------------------------------------------------------------

USER_LAYERS: List[int] = [getattr(BoardLayer, f"BL_User_{i}") for i in range(1, 46)]


def layer_display_name(board: Board, layer: int) -> str:
    try:
        return board.get_layer_name(layer)
    except Exception:
        return BoardLayer.Name(layer)


def find_layer_by_name(board: Board, name: str) -> Optional[int]:
    try:
        l = board.get_layer_by_name(name)
    except Exception:
        return None
    return None if l == BoardLayer.BL_UNDEFINED else l   # KiCad 10.0.6 returns BL_UNDEFINED, no error


def enabled_user_layers(board: Board) -> List[int]:
    enabled = set(board.get_enabled_layers())
    return [l for l in USER_LAYERS if l in enabled]
