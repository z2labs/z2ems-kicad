"""Engine installer: release lookup, checksum, extraction, config - against a fake GitHub (no network)."""

import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest

from z2ems import config, engine_install as ei


class Resp(io.BytesIO):
    def __init__(self, data: bytes):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def make_engine_zip(top: str | None = "Z2EMS", extra: dict | None = None) -> bytes:
    b = io.BytesIO()
    pre = f"{top}/" if top else ""
    with zipfile.ZipFile(b, "w") as z:
        z.writestr(pre + "twin/kicad_sim.py", "# twin\n")
        z.writestr(pre + "gui/server/z2twin_server.py", "# server\n")
        z.writestr(pre + "python/python.exe", "")
        z.writestr(pre + "bin/openEMS.exe", "")
        for k, v in (extra or {}).items():
            z.writestr(k, v)
    return b.getvalue()


def fake_github(zip_bytes: bytes, digest: bool = True, sums: bool = False, name="Z2EMS_engine_v0.1.0_win64.zip"):
    sha = hashlib.sha256(zip_bytes).hexdigest()
    asset = {"name": name, "size": len(zip_bytes), "browser_download_url": "https://dl/" + name}
    if digest:
        asset["digest"] = "sha256:" + sha
    assets = [asset, {"name": "openEMS_x64_v0.37.0_msvc.zip", "browser_download_url": "https://dl/oe.zip"}]
    if sums:
        assets.append({"name": ei.SUMS_ASSET, "browser_download_url": "https://dl/sums"})
    rel = {"tag_name": "v0.1.0", "published_at": "2026-11-06T00:00:00Z", "assets": assets}
    files = {ei.API_LATEST: json.dumps(rel).encode(), "https://dl/" + name: zip_bytes,
             "https://dl/sums": f"{sha}  {name}\n0000  other.zip\n".encode()}

    def opener(req, timeout=0):
        url = req.full_url if hasattr(req, "full_url") else req
        return Resp(files[url])
    return opener, sha


def test_plan_uses_github_digest():
    opener, sha = fake_github(make_engine_zip())
    p = ei.plan(opener=opener, platform="win32")
    assert p["tag"] == "v0.1.0" and p["sha256"] == sha and p["asset"].endswith("_win64.zip")


def test_plan_falls_back_to_sha256sums():
    opener, sha = fake_github(make_engine_zip(), digest=False, sums=True)
    assert ei.plan(opener=opener, platform="win32")["sha256"] == sha


def test_plan_without_checksum_refuses():
    opener, _ = fake_github(make_engine_zip(), digest=False, sums=False)
    with pytest.raises(ei.InstallError):
        ei.plan(opener=opener, platform="win32")


def test_no_package_for_linux():
    opener, _ = fake_github(make_engine_zip())
    with pytest.raises(ei.InstallError):
        ei.plan(opener=opener, platform="linux")


@pytest.mark.parametrize("top", ["Z2EMS", None])
def test_download_and_install(tmp_path: Path, top):
    opener, _ = fake_github(make_engine_zip(top))
    root = tmp_path / "Z2EMS" / "engine"
    seen = []
    p = ei.plan(opener=opener, platform="win32")
    unpacked = []
    out = ei.download_and_install(p, root, progress=lambda d, t: seen.append((d, t)) or True, opener=opener,
                                  unpack_progress=lambda i, n: unpacked.append((i, n)))
    assert out == root and ei.looks_like_engine(root)
    assert unpacked and unpacked[-1][0] == unpacked[-1][1]
    assert (root / "python" / "python.exe").exists()
    assert ei.installed_release(root)["tag"] == "v0.1.0"
    assert seen and seen[-1][0] == seen[-1][1]
    assert not (root.parent / "download" / p["asset"]).exists()
    # a second install replaces the first and leaves no staging folders
    ei.download_and_install(p, root, opener=opener)
    assert sorted(x.name for x in root.parent.iterdir()) == ["download", "engine"]


def test_checksum_mismatch_installs_nothing(tmp_path: Path):
    opener, _ = fake_github(make_engine_zip())
    p = ei.plan(opener=opener, platform="win32")
    p["sha256"] = "0" * 64
    root = tmp_path / "engine"
    with pytest.raises(ei.InstallError):
        ei.download_and_install(p, root, opener=opener)
    assert not root.exists()


def test_cancel(tmp_path: Path):
    opener, _ = fake_github(make_engine_zip())
    p = ei.plan(opener=opener, platform="win32")
    with pytest.raises(ei.InstallError):
        ei.download_and_install(p, tmp_path / "engine", progress=lambda d, t: False, opener=opener)
    assert not (tmp_path / "engine").exists()


def test_zip_slip_rejected(tmp_path: Path):
    z = tmp_path / "bad.zip"
    z.write_bytes(make_engine_zip(extra={"../evil.txt": "x"}))
    with pytest.raises(ei.InstallError):
        ei.install_zip(z, tmp_path / "engine")
    assert not (tmp_path / "evil.txt").exists()


def test_not_an_engine(tmp_path: Path):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as zf:
        zf.writestr("readme.txt", "hi")
    z = tmp_path / "x.zip"
    z.write_bytes(b.getvalue())
    with pytest.raises(ei.InstallError):
        ei.install_zip(z, tmp_path / "engine")


def test_set_engine_folder_is_used_by_config(tmp_path: Path, monkeypatch):
    cfg = tmp_path / ".z2ems.json"
    cfg.write_text(json.dumps({"twin_py": "/old/python", "openems_exe": "/x/openEMS"}))
    eng = tmp_path / "eng"
    (eng / "gui" / "server").mkdir(parents=True)
    (eng / "gui" / "server" / "z2twin_server.py").write_text("")
    ei.set_engine_folder(eng, cfg)
    j = json.loads(cfg.read_text())
    assert j["engine"] == str(eng) and "twin_py" not in j and j["openems_exe"] == "/x/openEMS"
    monkeypatch.setattr(config, "CONFIG_FILE", cfg)
    for k in ("Z2EMS_ENGINE", "Z2EMS_TWIN_PY", "Z2EMS_FORK_ROOT"):
        monkeypatch.delenv(k, raising=False)
    assert config.load().fork_root == eng
