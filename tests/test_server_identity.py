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


def test_speed_factor_learns_from_a_run(tmp_path):
    from z2ems import simulate as sim
    f = tmp_path / "speed.json"
    assert sim.speed_factor("plain", f) == (1.0, False)
    sim.record_speed(5.0, 12.5, "plain", f)                 # the first coupon on the test machine
    assert sim.speed_factor("plain", f) == (2.5, True)
    fac, measured = sim.speed_factor("debye", f)            # no dispersive run yet: a guess from the plain one
    assert not measured and abs(fac - 4.0) < 1e-9
    sim.record_speed(5.0, 7.5, "plain", f)                  # the next run moves it half way
    assert sim.speed_factor("plain", f) == (2.0, True)
    sim.record_speed(0.0, 3.0, "plain", f)                  # nothing to learn from
    assert sim.speed_factor("plain", f) == (2.0, True)
    f.write_text("not json")
    assert sim.speed_factor("plain", f) == (1.0, False)


def test_one_starter_per_port(tmp_path, monkeypatch):
    """Two actions clicked together must not both start a server on the same port."""
    import tempfile
    from z2ems import simulate as sim
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    a = sim._start_lock(8765)
    assert a is not None and sim._start_lock(8765) is None
    assert sim._start_lock(8766) is not None                # another port is another server
    a.unlink()
    assert sim._start_lock(8765) is not None
    import os, time
    old = time.time() - 600                                 # a starter that died: its lock goes stale
    os.utime(tmp_path / "z2ems_server_8765.starting", (old, old))
    assert sim._start_lock(8765) is not None


def test_server_log_not_next_to_the_board(tmp_path, monkeypatch):
    from z2ems import simulate as sim
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "la"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "st"))
    d = sim.log_dir()
    assert d.is_dir() and tmp_path in d.parents
