"""Tests for netutil's fail-soft contracts: every probe failure must still
return the safe default ({}, None, False) — now with logging instead of
silence, but with the return values unchanged."""
import netutil


def _raise(*a, **k):
    raise OSError("no such tool")


def test_get_all_interfaces_empty_on_failure(monkeypatch):
    monkeypatch.setattr(netutil.subprocess, "run", _raise)
    assert netutil.get_all_interfaces() == {}


def test_dependent_getters_fall_back_when_probe_fails(monkeypatch):
    monkeypatch.setattr(netutil.subprocess, "run", _raise)
    assert netutil.get_wifi_ip() is None
    assert netutil.get_usb_iphone_ip() is None
    assert netutil.get_wireguard_ip() is None
    assert netutil.get_interface_ip("eth0") is None
    assert netutil.get_active_ip() == (None, None)
    # With no interface IPs to bind, bind_interfaces falls back to all.
    assert netutil.get_bind_address(["wifi", "usb_iphone"]) == "0.0.0.0"


def test_get_wifi_ssid_none_when_all_tools_fail(monkeypatch):
    monkeypatch.setattr(netutil.subprocess, "run", _raise)
    assert netutil.get_wifi_ssid() is None


def test_have_connectivity_false_when_unreachable(monkeypatch):
    monkeypatch.setattr(netutil.socket, "create_connection", _raise)
    assert netutil.have_connectivity() is False


def test_failures_are_logged(monkeypatch, caplog):
    import logging
    monkeypatch.setattr(netutil.subprocess, "run", _raise)
    with caplog.at_level(logging.WARNING, logger="netutil"):
        netutil.get_all_interfaces()
    assert any("ip addr show" in r.message for r in caplog.records)
