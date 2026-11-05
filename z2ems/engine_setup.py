"""Hub action "Engine...": install, update or locate the Z2 EMS engine.

Shows which engine the plugin uses and offers the latest release from github.com/z2labs/z2ems
(Windows) or a folder of an engine built from source. The other actions call the same dialog
when they find no engine.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from z2ems.engine_install import setup_dialog  # noqa: E402


def main() -> int:
    import wx
    app = wx.App(False)  # noqa: F841
    return 0 if setup_dialog(missing=False) else 1


if __name__ == "__main__":
    sys.exit(main())
