"""Z2 EMS engine: download and install the engine package, or point the plugin at an existing one.

The plugin (installed from KiCad's Plugin and Content Manager) is small; the engine (openEMS with the CPU
and CUDA engines, a private Python with the CSXCAD/openEMS bindings, the twin and the local server) is a
separate download of ~160 MB from the z2labs/z2ems GitHub releases. Nothing is downloaded without the
user's consent: the first action that needs the engine asks, shows where it comes from and where it goes.

Windows: release asset Z2EMS_engine_<version>_win64.zip, checked against the SHA-256 that GitHub publishes
for the asset (or the release's SHA256SUMS.txt), extracted to %LOCALAPPDATA%\\Z2EMS\\engine.
Linux/macOS: no prebuilt package yet; the dialog links the source build and lets the user pick the folder
of an engine built there (stored in ~/.z2ems.json as "engine").
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sys
import zipfile
from pathlib import Path
from typing import Callable
from urllib.request import Request, urlopen

from z2ems import __version__
from z2ems import config

REPO = "z2labs/z2ems"
RELEASES_URL = f"https://github.com/{REPO}/releases"
SOURCE_BUILD_URL = f"https://github.com/{REPO}#install"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
ASSET_PATTERNS = {"win32": re.compile(r"^Z2EMS_engine_.+_win64\.zip$")}
SUMS_ASSET = "SHA256SUMS.txt"
RELEASE_FILE = "z2ems-release.json"     # written into the engine folder after an install
CHUNK = 1 << 20

Opener = Callable[..., object]


class InstallError(RuntimeError):
    pass


# ------------------------------------------------------------------ release lookup

def _get(url: str, opener: Opener = urlopen, timeout: float = 30):
    req = Request(url, headers={"User-Agent": f"z2ems-kicad/{__version__}",
                                "Accept": "application/vnd.github+json"})
    return opener(req, timeout=timeout)


def fetch_release(url: str = API_LATEST, opener: Opener = urlopen) -> dict:
    with _get(url, opener) as r:
        return json.loads(r.read().decode("utf-8"))


def pick_asset(release: dict, platform: str = sys.platform) -> dict | None:
    pat = ASSET_PATTERNS.get(platform)
    if pat is None:
        return None
    for a in release.get("assets", []):
        if pat.match(a.get("name", "")):
            return a
    return None


def expected_sha256(release: dict, asset: dict, opener: Opener = urlopen) -> str | None:
    """SHA-256 of the asset: GitHub's own asset digest, else the release's SHA256SUMS.txt."""
    d = asset.get("digest") or ""
    if d.startswith("sha256:"):
        return d.split(":", 1)[1].lower()
    for a in release.get("assets", []):
        if a.get("name") == SUMS_ASSET:
            with _get(a["browser_download_url"], opener) as r:
                for line in r.read().decode("utf-8", "replace").splitlines():
                    parts = line.split()
                    if len(parts) == 2 and parts[1].lstrip("*") == asset["name"]:
                        return parts[0].lower()
    return None


# ------------------------------------------------------------------ download + install

def download(url: str, dest: Path, sha256: str, size: int = 0,
             progress: Callable[[int, int], bool] | None = None, opener: Opener = urlopen) -> Path:
    """Stream url to dest and check its SHA-256. progress(done, total) returning False aborts."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    h = hashlib.sha256()
    done = 0
    with _get(url, opener, timeout=60) as r, part.open("wb") as f:
        total = int(getattr(r, "headers", {}).get("Content-Length") or size or 0)
        while True:
            buf = r.read(CHUNK)
            if not buf:
                break
            f.write(buf)
            h.update(buf)
            done += len(buf)
            if progress and progress(done, total) is False:
                f.close()
                part.unlink(missing_ok=True)
                raise InstallError("download cancelled")
    if h.hexdigest().lower() != sha256.lower():
        part.unlink(missing_ok=True)
        raise InstallError(f"checksum mismatch for {url}: got {h.hexdigest()}, expected {sha256}")
    part.replace(dest)
    return dest


def looks_like_engine(root: Path) -> bool:
    return (root / "gui" / "server" / "z2twin_server.py").exists() and (root / "twin" / "kicad_sim.py").exists()


def _safe_extract(zf: zipfile.ZipFile, target: Path, unpack_progress=None) -> None:
    """Extract after checking every member stays inside target. unpack_progress(i, n) is called every
    few files (the engine is ~5000 files; on Windows with a virus scanner that takes minutes)."""
    base = target.resolve()
    members = zf.infolist()
    for m in members:
        p = (target / m.filename).resolve()
        if p != base and base not in p.parents:
            raise InstallError(f"unsafe path in archive: {m.filename}")
    n = len(members)
    for i, m in enumerate(members):
        zf.extract(m, target)
        if unpack_progress and (i % 25 == 0 or i == n - 1):
            unpack_progress(i + 1, n)


def install_zip(zip_path: Path, root: Path, info: dict | None = None, unpack_progress=None) -> Path:
    """Extract an engine package zip into root (replacing a previous install). The zip holds one top
    folder (Z2EMS/) or the package files directly."""
    root = Path(root)
    stage = root.with_name(root.name + ".new")
    old = root.with_name(root.name + ".old")
    for p in (stage, old):
        if p.exists():
            shutil.rmtree(p)
    with zipfile.ZipFile(zip_path) as zf:
        _safe_extract(zf, stage, unpack_progress)
    pkg = stage
    if not looks_like_engine(pkg):
        subs = [d for d in stage.iterdir() if d.is_dir()]
        if len(subs) == 1 and looks_like_engine(subs[0]):
            pkg = subs[0]
        else:
            shutil.rmtree(stage)
            raise InstallError("the archive is not a Z2 EMS engine package (no twin/ and gui/server/)")
    root.parent.mkdir(parents=True, exist_ok=True)
    if root.exists():
        root.replace(old)
    try:
        pkg.replace(root)
    except OSError:
        if old.exists() and not root.exists():
            old.replace(root)
        raise
    if stage.exists():
        shutil.rmtree(stage, ignore_errors=True)
    if old.exists():
        shutil.rmtree(old, ignore_errors=True)
    meta = dict(info or {})
    meta.setdefault("installed", dt.datetime.now().isoformat(timespec="seconds"))
    (root / RELEASE_FILE).write_text(json.dumps(meta, indent=1), encoding="utf-8")
    return root


def installed_release(root: Path | None = None) -> dict | None:
    root = Path(root) if root else install_root()
    try:
        return json.loads((root / RELEASE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def install_root() -> Path:
    return config.default_roots()[0]


def set_engine_folder(folder: Path, cfg_file: Path | None = None) -> None:
    """Remember an engine folder in ~/.z2ems.json (keeps the file's other keys)."""
    cfg_file = cfg_file or config.CONFIG_FILE
    try:
        j = json.loads(cfg_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        j = {}
    for k in ("twin_py", "fork_root"):      # superseded by "engine"
        j.pop(k, None)
    j["engine"] = str(Path(folder))
    cfg_file.write_text(json.dumps(j, indent=1), encoding="utf-8")


def plan(opener: Opener = urlopen, platform: str = sys.platform) -> dict:
    """What a download would fetch: tag, asset name/url/size, sha256. Raises InstallError."""
    rel = fetch_release(opener=opener)
    asset = pick_asset(rel, platform)
    if asset is None:
        raise InstallError(f"release {rel.get('tag_name')} has no engine package for {platform}")
    sha = expected_sha256(rel, asset, opener)
    if not sha:
        raise InstallError(f"release {rel.get('tag_name')} publishes no SHA-256 for {asset['name']}")
    return dict(tag=rel.get("tag_name"), published=rel.get("published_at"), asset=asset["name"],
                url=asset["browser_download_url"], size=int(asset.get("size") or 0), sha256=sha)


def download_and_install(p: dict, root: Path | None = None, progress=None, opener: Opener = urlopen,
                         unpack_progress=None) -> Path:
    root = Path(root) if root else install_root()
    dl = root.parent / "download" / p["asset"]
    download(p["url"], dl, p["sha256"], p.get("size", 0), progress, opener)
    out = install_zip(dl, root, dict(tag=p["tag"], asset=p["asset"], sha256=p["sha256"], source=p["url"]),
                      unpack_progress)
    try:
        dl.unlink()
    except OSError:
        pass
    return out


# ------------------------------------------------------------------ GUI

def _mb(n: int) -> str:
    return f"{n / 1e6:.0f} MB" if n else "size unknown"


_OWNER = None


def _owner():
    """A hidden always-on-top frame as parent: the plugin runs in its own process, and on Windows a
    parentless dialog can open behind the maximized PCB editor."""
    global _OWNER
    import wx
    if _OWNER is None:
        _OWNER = wx.Frame(None, title="Z2Labs EMS", style=wx.STAY_ON_TOP | wx.FRAME_NO_TASKBAR)
    return _OWNER


def _modal(dlg) -> int:
    dlg.CentreOnScreen()
    dlg.Raise()
    r = dlg.ShowModal()
    dlg.Destroy()
    return r


def _box(text: str, icon) -> None:
    import wx
    _modal(wx.MessageDialog(_owner(), text, "Z2Labs EMS - engine", wx.OK | icon | wx.STAY_ON_TOP))


def ensure_engine(reason: str = "") -> bool:
    """Called by an action that needs the engine. True when an engine is usable (maybe after installing
    it now). Needs a wx.App."""
    if not config.load().problems():
        return True
    return setup_dialog(missing=True, reason=reason)


def setup_dialog(missing: bool = False, reason: str = "") -> bool:
    import wx
    root = install_root()
    cfg = config.load()
    have = installed_release(cfg.fork_root) or installed_release(root)
    head = ("The Z2 EMS engine is not installed on this computer." if missing else
            f"Z2 EMS engine: {cfg.fork_root}" + (f"  (release {have.get('tag')})" if have else ""))
    if reason:
        head = reason + "\n\n" + head
    if sys.platform != "win32":
        msg = (head + "\n\nThere is no prebuilt engine for this system yet. Build it from source:\n"
               f"{SOURCE_BUILD_URL}\n\nThen choose the folder of the built engine (the one with twin/ and gui/).")
        d = wx.MessageDialog(_owner(), msg, "Z2Labs EMS - engine", wx.YES_NO | wx.ICON_INFORMATION | wx.STAY_ON_TOP)
        d.SetYesNoLabels("&Choose folder...", "Cancel")
        return _choose_folder() if _modal(d) == wx.ID_YES else False
    try:
        p = plan()
    except Exception as e:  # noqa: BLE001
        msg = (head + f"\n\nCould not look up the engine release on GitHub:\n{e}\n\n"
               f"Download it by hand from {RELEASES_URL}, unzip it to\n{root}\nor choose the folder of an engine.")
        d = wx.MessageDialog(_owner(), msg, "Z2Labs EMS - engine", wx.YES_NO | wx.ICON_WARNING | wx.STAY_ON_TOP)
        d.SetYesNoLabels("&Choose folder...", "Cancel")
        return _choose_folder() if _modal(d) == wx.ID_YES else False
    if have and have.get("tag") == p["tag"] and not missing:
        action = f"The installed engine is the latest release ({p['tag']}). Install it again?"
    else:
        action = (f"Download and install the Z2 EMS engine {p['tag']} now?\n\n"
                  f"  file:   {p['asset']} ({_mb(p['size'])})\n  from:   {RELEASES_URL}\n  into:   {root}\n\n"
                  "It contains openEMS (CPU and NVIDIA CUDA engines), a private Python and the Z2 EMS\n"
                  "simulation scripts; free software under the GPL-3.0. Nothing else on this computer is changed.")
    d = wx.MessageDialog(_owner(), head + "\n\n" + action, "Z2Labs EMS - engine",
                         wx.YES_NO | wx.CANCEL | wx.ICON_QUESTION | wx.STAY_ON_TOP)
    d.SetYesNoCancelLabels("&Download", "&Choose folder...", "Cancel")
    r = _modal(d)
    if r == wx.ID_NO:
        return _choose_folder()
    if r != wx.ID_YES:
        return False
    pd = wx.ProgressDialog("Z2Labs EMS - engine", f"Downloading {p['asset']}...", maximum=1000, parent=_owner(),
                           style=wx.PD_CAN_ABORT | wx.PD_APP_MODAL | wx.PD_AUTO_HIDE | wx.PD_ELAPSED_TIME | wx.PD_REMAINING_TIME)
    pd.CentreOnScreen()

    def prog(done: int, total: int) -> bool:
        frac = done / total if total else 0.0
        cont, _ = pd.Update(min(int(frac * 950), 950), f"Downloading {p['asset']}: {done / 1e6:.0f} / {_mb(total)}")
        wx.YieldIfNeeded()
        return cont

    def unpack(i: int, n: int) -> None:
        pd.Update(950 + int(49 * i / max(n, 1)), f"Unpacking the engine: {i} / {n} files")
        wx.YieldIfNeeded()

    try:
        download_and_install(p, root, progress=prog, unpack_progress=unpack)
        pd.Update(1000, "Installed.")
    except Exception as e:  # noqa: BLE001
        pd.Destroy()
        _box(f"The engine was not installed:\n{e}\n\n(If a simulation or the 3D viewer of the old engine is running, close it and try again.)", wx.ICON_ERROR)
        return False
    pd.Destroy()
    # an explicit engine setting elsewhere would shadow the new install: point it here
    cfg = config.load()
    if cfg.fork_root.resolve() != root.resolve():
        set_engine_folder(root)
    _box(f"Z2 EMS engine {p['tag']} installed in\n{root}", wx.ICON_INFORMATION)
    return True


def _choose_folder() -> bool:
    import wx
    d = wx.DirDialog(_owner(), "Folder of the Z2 EMS engine (contains twin/ and gui/)",
                     style=wx.DD_DEFAULT_STYLE | wx.DD_DIR_MUST_EXIST)
    ok = d.ShowModal() == wx.ID_OK
    path = Path(d.GetPath()) if ok else None
    d.Destroy()
    if not path:
        return False
    if not looks_like_engine(path):
        _box(f"{path}\nis not a Z2 EMS engine folder (no twin/kicad_sim.py and gui/server/).", wx.ICON_ERROR)
        return False
    set_engine_folder(path)
    probs = config.load().problems()
    if probs:
        _box("Engine folder saved, but:\n" + "\n".join(probs), wx.ICON_WARNING)
        return False
    return True
