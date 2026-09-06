import time

import pytest

import tuxthrottle_control as control


@pytest.fixture()
def server(tmp_path):
    sock = tmp_path / "control.sock"
    srv = control.ControlServer(sock)
    srv.register("echo", lambda p: {"got": p})
    srv.register("boom", lambda p: (_ for _ in ()).throw(RuntimeError("kaboom")))
    srv.start()
    time.sleep(0.15)
    yield sock
    srv.stop()


def test_ping_roundtrip(server):
    resp = control.call("ping", path=server)
    assert resp["ok"] is True
    assert "pong" in resp["result"]


def test_echo_passes_params(server):
    resp = control.call("echo", {"x": 1, "y": "z"}, path=server)
    assert resp == {"ok": True, "result": {"got": {"x": 1, "y": "z"}}}


def test_unknown_method(server):
    resp = control.call("nope", path=server)
    assert resp["ok"] is False and "unknown method" in resp["error"]


def test_handler_exception_becomes_error(server):
    resp = control.call("boom", path=server)
    assert resp["ok"] is False and "kaboom" in resp["error"]


def test_available_true_then_false(server):
    assert control.available(server) is True
    # a bogus path is never available
    assert control.available(server.parent / "missing.sock") is False


def test_call_returns_none_when_socket_absent(tmp_path):
    assert control.call("ping", path=tmp_path / "nope.sock") is None
