"""Tests for wg_manager.latest_handshake's None-vs-0 contract and the
stop_wireguard clear_full_tunnel warning."""
import wg_manager


class _FakeProc:
    def __init__(self, returncode, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


def test_latest_handshake_none_on_command_error(monkeypatch):
    monkeypatch.setattr(wg_manager.subprocess, "run",
                        lambda *a, **k: _FakeProc(1, ""))
    assert wg_manager.latest_handshake("wg0") is None


def test_latest_handshake_none_when_wg_missing(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("wg")
    monkeypatch.setattr(wg_manager.subprocess, "run", boom)
    assert wg_manager.latest_handshake("wg0") is None


def test_latest_handshake_none_on_timeout(monkeypatch):
    def boom(*a, **k):
        raise wg_manager.subprocess.TimeoutExpired(cmd="wg", timeout=5)
    monkeypatch.setattr(wg_manager.subprocess, "run", boom)
    assert wg_manager.latest_handshake("wg0") is None


def test_latest_handshake_zero_when_never_handshaked(monkeypatch):
    # wg prints a 0 epoch for a peer that has never handshaked — a real answer.
    monkeypatch.setattr(wg_manager.subprocess, "run",
                        lambda *a, **k: _FakeProc(0, "pubkeyAAA\t0\n"))
    assert wg_manager.latest_handshake("wg0") == 0


def test_latest_handshake_parses_newest(monkeypatch):
    out = "pubkeyAAA\t1700000000\npubkeyBBB\t1700000500\n"
    monkeypatch.setattr(wg_manager.subprocess, "run",
                        lambda *a, **k: _FakeProc(0, out))
    assert wg_manager.latest_handshake("wg0") == 1700000500


def test_stop_wireguard_warns_when_clear_fails(monkeypatch, capsys):
    def boom(iface):
        raise RuntimeError("iptables gone")
    monkeypatch.setattr(wg_manager, "clear_full_tunnel", boom)
    monkeypatch.setattr(wg_manager.subprocess, "run",
                        lambda *a, **k: _FakeProc(0, ""))
    # wg-quick down still runs and its result is still returned.
    assert wg_manager.stop_wireguard("wg0") is True
    out = capsys.readouterr().out
    assert "could not clear full-tunnel rules" in out
