from pathlib import Path

from z2ems.config import EngineConfig
from z2ems.simulate import server_engine_root, server_is_current


def cfg(root):
    return EngineConfig(twin_py=Path("python"), fork_root=Path(root))


def test_new_server_same_root_is_current(tmp_path):
    h = dict(ports_api=2, fields_api=2, root=str(tmp_path), engine="openEMS")
    assert server_is_current(h, cfg(tmp_path))[0]


def test_server_of_another_engine_folder_is_replaced(tmp_path):
    h = dict(ports_api=2, fields_api=2, root=str(tmp_path / "old"), engine="openEMS")
    ok, why = server_is_current(h, cfg(tmp_path / "new"))
    assert not ok and "not the selected" in why


def test_old_api_is_replaced(tmp_path):
    h = dict(ports_api=1, root=str(tmp_path))
    assert not server_is_current(h, cfg(tmp_path))[0]


def test_package_root_from_engine_path(tmp_path):
    # a 0.3.0 package server reports only <root>/twin/../bin/openEMS.exe
    exe = tmp_path / "twin" / ".." / "bin" / "openEMS.exe"
    h = dict(ports_api=2, fields_api=2, engine=str(exe))
    assert server_engine_root(h) == server_engine_root(dict(root=str(tmp_path)))
    assert server_is_current(h, cfg(tmp_path))[0]


def test_unknown_engine_is_replaced(tmp_path):
    h = dict(ports_api=2, fields_api=2, engine="openEMS")
    assert not server_is_current(h, cfg(tmp_path))[0]
