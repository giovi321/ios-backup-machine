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


# --- resolve_bind_addresses -------------------------------------------------
# bind_interfaces is a multi-select in the settings UI, so it must resolve to
# every selected address, not just the first one that happens to have an IP.


def _stub_ips(monkeypatch, wifi=None, usb=None, wg=None):
    monkeypatch.setattr(netutil, "get_wifi_ip", lambda: wifi)
    monkeypatch.setattr(netutil, "get_usb_iphone_ip", lambda: usb)
    monkeypatch.setattr(netutil, "get_wireguard_ip", lambda *a, **k: wg)


def test_resolve_bind_addresses_all_is_the_wildcard(monkeypatch):
    _stub_ips(monkeypatch, wifi="192.168.1.50")
    assert netutil.resolve_bind_addresses(["all"]) == ["0.0.0.0"]


def test_resolve_bind_addresses_empty_selection_is_the_wildcard(monkeypatch):
    _stub_ips(monkeypatch, wifi="192.168.1.50")
    assert netutil.resolve_bind_addresses([]) == ["0.0.0.0"]
    assert netutil.resolve_bind_addresses(None) == ["0.0.0.0"]


def test_resolve_bind_addresses_all_absorbs_the_others(monkeypatch):
    # A wildcard bind and a specific bind on one port collide, so 'all' wins
    # outright rather than being listed alongside a concrete address.
    _stub_ips(monkeypatch, wifi="192.168.1.50", wg="10.7.0.3")
    assert netutil.resolve_bind_addresses(["wifi", "all", "wireguard"]) == ["0.0.0.0"]


def test_resolve_bind_addresses_returns_every_selected_interface(monkeypatch):
    _stub_ips(monkeypatch, wifi="192.168.1.50", usb="172.20.10.2", wg="10.7.0.3")
    assert netutil.resolve_bind_addresses(["usb_iphone", "wireguard"]) == [
        "172.20.10.2", "10.7.0.3",
    ]


def test_resolve_bind_addresses_keeps_config_order(monkeypatch):
    _stub_ips(monkeypatch, wifi="192.168.1.50", usb="172.20.10.2")
    assert netutil.resolve_bind_addresses(["usb_iphone", "wifi"]) == [
        "172.20.10.2", "192.168.1.50",
    ]


def test_resolve_bind_addresses_deduplicates(monkeypatch):
    # Two selectors can name one interface (a WiFi link that is also the
    # WireGuard endpoint address); binding it twice would fail with EADDRINUSE.
    _stub_ips(monkeypatch, wifi="10.7.0.3", wg="10.7.0.3")
    assert netutil.resolve_bind_addresses(["wifi", "wireguard"]) == ["10.7.0.3"]


def test_resolve_bind_addresses_skips_an_interface_with_no_ip(monkeypatch):
    # The regression this whole change exists for: a selected interface that is
    # down must be skipped, never a reason to serve the others' traffic instead.
    _stub_ips(monkeypatch, usb="172.20.10.2", wg=None)
    assert netutil.resolve_bind_addresses(["usb_iphone", "wireguard"]) == ["172.20.10.2"]


def test_resolve_bind_addresses_does_not_widen_when_nothing_resolves(monkeypatch):
    # Falling back to 0.0.0.0 here would silently discard the restriction the
    # operator configured. Bind nothing; the supervisor retries.
    _stub_ips(monkeypatch)
    assert netutil.resolve_bind_addresses(["wifi", "wireguard"]) == []


def test_resolve_bind_addresses_ignores_an_unknown_selector(monkeypatch, caplog):
    import logging
    _stub_ips(monkeypatch, wifi="192.168.1.50")
    with caplog.at_level(logging.WARNING, logger="netutil"):
        assert netutil.resolve_bind_addresses(["wifi", "ethernet"]) == ["192.168.1.50"]
    assert "ethernet" in caplog.text


def test_get_bind_address_returns_the_first_resolved_address(monkeypatch):
    _stub_ips(monkeypatch, wifi="192.168.1.50", usb="172.20.10.2")
    assert netutil.get_bind_address(["usb_iphone", "wifi"]) == "172.20.10.2"
