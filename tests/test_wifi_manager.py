"""Tests for wifi_manager.build_netplan — the pure netplan YAML generator."""
import yaml

import wifi_manager


def test_build_netplan_basic_structure():
    nets = [
        {"nickname": "Home", "ssid": "HomeNet", "password": "secret"},
        {"nickname": "Office", "ssid": "Corp", "password": ""},
    ]
    doc = yaml.safe_load(wifi_manager.build_netplan(nets, "wlan0"))
    net = doc["network"]
    assert net["version"] == 2
    # No explicit renderer — it merges with the OS netplan files (networkd default).
    assert "renderer" not in net
    wlan = net["wifis"]["wlan0"]
    assert wlan["dhcp4"] is True
    assert wlan["optional"] is True
    aps = wlan["access-points"]
    assert aps["HomeNet"] == {"password": "secret"}
    assert aps["Corp"] == {}          # open network -> empty mapping (no password key)


def test_build_netplan_skips_blank_ssid():
    nets = [
        {"nickname": "x", "ssid": "   ", "password": "p"},
        {"nickname": "y", "ssid": "Real", "password": "q"},
    ]
    doc = yaml.safe_load(wifi_manager.build_netplan(nets, "wlan0"))
    aps = doc["network"]["wifis"]["wlan0"]["access-points"]
    assert list(aps.keys()) == ["Real"]


def test_build_netplan_handles_special_chars():
    # Colons/spaces/symbols must survive a YAML round-trip (safe_dump quotes them).
    nets = [{"nickname": "", "ssid": "My:Net Work", "password": "p@ss: word#1"}]
    doc = yaml.safe_load(wifi_manager.build_netplan(nets, "wlan0"))
    aps = doc["network"]["wifis"]["wlan0"]["access-points"]
    assert aps["My:Net Work"] == {"password": "p@ss: word#1"}


def test_build_netplan_empty_list():
    doc = yaml.safe_load(wifi_manager.build_netplan([], "wlan0"))
    assert doc["network"]["wifis"]["wlan0"]["access-points"] == {}
