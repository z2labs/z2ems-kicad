"""Plugin notices that the user actually sees.

The plugin is a separate process started by KiCad; its windows have no parent in
the PCB editor and, on Windows, open *behind* the maximized editor unless forced
to the top (Windows 11: wx.MessageBox + STAY_ON_TOP was enough for
the result box but the error box still hid behind pcbnew). The native
MessageBoxW with MB_TOPMOST | MB_SETFOREGROUND has been reliable in every live
run, so that is what Windows gets; wx elsewhere.

Every notice is also appended to <sidecar>/last_notice.txt when a sidecar
directory is given, so a soak harness can read what a dialog said.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path

NOTICE_FILE = "last_notice.txt"


def notify(title: str, text: str, kind: str = "info", sidecar: Path | None = None) -> None:
    if sidecar is not None:
        try:
            sidecar.mkdir(parents=True, exist_ok=True)
            with (sidecar / NOTICE_FILE).open("a", encoding="utf-8") as f:
                f.write(f"--- {dt.datetime.now().isoformat(timespec='seconds')} [{kind}] {title}\n{text}\n")
        except OSError:
            pass
    if os.name == "nt":
        import ctypes
        icon = {"error": 0x10, "warn": 0x30}.get(kind, 0x40)
        ctypes.windll.user32.MessageBoxW(None, text, title, icon | 0x00040000 | 0x00010000)   # TOPMOST | SETFOREGROUND
        return
    try:
        import wx
        style = {"error": wx.ICON_ERROR, "warn": wx.ICON_WARNING}.get(kind, wx.ICON_INFORMATION)
        wx.MessageBox(text, title, style | wx.STAY_ON_TOP)
    except Exception:  # noqa: BLE001  - no wx: plain stderr
        print(f"{title}: {text}", file=sys.stderr)
