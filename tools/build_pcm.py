#!/usr/bin/env python3
"""Build the KiCad Plugin and Content Manager (PCM) package of Z2Labs EMS.

    python tools/build_pcm.py [--out dist] [--url https://github.com/z2labs/z2ems-kicad/releases/download/v0.1.0/<zip>]

Writes
  dist/z2ems-kicad-<version>-pcm.zip    the package: plugins/ (plugin.json at its top, as KiCad expects
                                         for an IPC plugin), resources/icon.png, metadata.json
  dist/metadata.json                     the metadata for gitlab.com/kicad/addons/metadata
                                         (packages/<identifier>/metadata.json), with download_url,
                                         download_sha256, download_size and install_size filled in
  dist/icon.png                          the 64x64 icon for the same folder

The version comes from z2ems/__init__.py and must match pcm/metadata.json.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO_URL = "https://github.com/z2labs/z2ems-kicad"

# what goes into plugins/ (relative to the repository root)
PLUGIN_FILES = ["plugin.json", "requirements.txt", "LICENSE", "README.md"]
PLUGIN_DIRS = ["z2ems", "icons", "presets", "library", "spike0", "docs"]
SKIP = re.compile(r"(__pycache__|\.pyc$|\.pytest_cache|/user/.+)")


def version() -> str:
    m = re.search(r'__version__\s*=\s*"([^"]+)"', (ROOT / "z2ems" / "__init__.py").read_text(encoding="utf-8"))
    return m.group(1)


def files() -> list[tuple[Path, str]]:
    out = [(ROOT / f, f"plugins/{f}") for f in PLUGIN_FILES]
    for d in PLUGIN_DIRS:
        for p in sorted((ROOT / d).rglob("*")):
            rel = p.relative_to(ROOT).as_posix()
            if p.is_file() and not SKIP.search("/" + rel):
                out.append((p, f"plugins/{rel}"))
    return out


def build(out_dir: Path, url: str | None) -> dict:
    ver = version()
    meta = json.loads((ROOT / "pcm" / "metadata.json").read_text(encoding="utf-8"))
    plugin = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
    if meta["identifier"] != plugin["identifier"]:
        sys.exit(f"identifier mismatch: pcm/metadata.json {meta['identifier']} vs plugin.json {plugin['identifier']}")
    if [v["version"] for v in meta["versions"]] [-1] != ver:
        sys.exit(f"version mismatch: z2ems/__init__.py {ver} vs pcm/metadata.json {meta['versions'][-1]['version']}")
    out_dir.mkdir(parents=True, exist_ok=True)
    zpath = out_dir / f"z2ems-kicad-{ver}-pcm.zip"
    install_size = 0
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for src, arc in files():
            z.write(src, arc)
            install_size += src.stat().st_size
        z.write(ROOT / "pcm" / "icon.png", "resources/icon.png")
        install_size += (ROOT / "pcm" / "icon.png").stat().st_size
        inner = json.loads(json.dumps(meta))
        inner["versions"] = [v for v in inner["versions"] if v["version"] == ver]   # no download_* inside
        blob = json.dumps(inner, indent=2).encode("utf-8")
        z.writestr("metadata.json", blob)
        install_size += len(blob)
    data = zpath.read_bytes()
    v = next(v for v in meta["versions"] if v["version"] == ver)
    v["download_url"] = url or f"{REPO_URL}/releases/download/v{ver}/{zpath.name}"
    v["download_sha256"] = hashlib.sha256(data).hexdigest()
    v["download_size"] = len(data)
    v["install_size"] = install_size
    (out_dir / "metadata.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    shutil.copy(ROOT / "pcm" / "icon.png", out_dir / "icon.png")
    return dict(zip=str(zpath), version=ver, **{k: v[k] for k in ("download_url", "download_sha256", "download_size", "install_size")})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=str(ROOT / "dist"))
    ap.add_argument("--url", help="download URL of the zip (default: the GitHub release of this version)")
    a = ap.parse_args()
    print(json.dumps(build(Path(a.out), a.url), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
