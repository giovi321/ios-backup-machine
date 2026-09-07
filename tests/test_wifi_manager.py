"""Tests for wifi_manager: the pure netplan YAML generator and the
apply/rollback failure reporting in apply_networks."""
import os

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
    # WiFi gets a high (low-priority) route metric so the iPhone hotspot is
    # preferred when present and WiFi isn't dropped on iPhone connect/disconnect.
    assert wlan["dhcp4-overrides"]["route-metric"] == wifi_manager.WIFI_ROUTE_METRIC
    assert wlan["dhcp4-overrides"]["route-metric"] > 1024   # above networkd's DHCP default
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


# ---------------------------------------------------------------------------
# apply_networks rollback reporting
# ---------------------------------------------------------------------------

def _patch_netplan_env(monkeypatch, tmp_path, apply_results):
    """Point the managed file at tmp_path and script `netplan` outcomes.

    apply_results: list of (rc, out) returned by successive `netplan apply`
    calls; `netplan generate` always succeeds. Returns the path of the managed
    file so a test can pre-seed the "previous" config."""
    managed = str(tmp_path / "90-iosbackup-wifi.yaml")
    monkeypatch.setattr(wifi_manager, "MANAGED_FILE", managed)
    monkeypatch.setattr(wifi_manager, "get_wifi_interface", lambda: "wlan0")
    calls = []

    def fake_run(args, timeout=90):
        calls.append(list(args))
        if args[:2] == ["netplan", "apply"]:
            return apply_results.pop(0)
        return 0, ""   # netplan generate

    monkeypatch.setattr(wifi_manager, "_run", fake_run)
    return managed, calls


def test_apply_failure_rolls_back_and_reports(monkeypatch, tmp_path):
    managed, _ = _patch_netplan_env(monkeypatch, tmp_path,
                                    apply_results=[(1, "boom"), (0, "")])
    with open(managed, "w") as f:
        f.write("previous-good-config")

    ok, msg = wifi_manager.apply_networks([{"ssid": "Home", "password": "pw"}])
    assert ok is False
    assert "netplan apply failed" in msg
    assert "rollback also failed" not in msg
    # The previous managed file is restored after the failed apply.
    with open(managed) as f:
        assert f.read() == "previous-good-config"


def test_apply_failure_with_failed_rollback_says_wifi_may_be_down(monkeypatch, tmp_path):
    managed, _ = _patch_netplan_env(monkeypatch, tmp_path,
                                    apply_results=[(1, "boom"), (1, "still broken")])
    with open(managed, "w") as f:
        f.write("previous-good-config")

    ok, msg = wifi_manager.apply_networks([{"ssid": "Home", "password": "pw"}])
    assert ok is False
    assert "rollback also failed" in msg
    assert "WiFi may be down" in msg


def test_zero_networks_apply_failure_is_reported(monkeypatch, tmp_path):
    # Previously this path swallowed everything and returned success.
    managed, _ = _patch_netplan_env(monkeypatch, tmp_path,
                                    apply_results=[(1, "boom"), (0, "")])
    with open(managed, "w") as f:
        f.write("previous-good-config")

    ok, msg = wifi_manager.apply_networks([])
    assert ok is False
    assert "netplan apply failed" in msg
    # Rolled back: the managed file is back.
    assert os.path.exists(managed)


def test_zero_networks_success(monkeypatch, tmp_path):
    managed, _ = _patch_netplan_env(monkeypatch, tmp_path,
                                    apply_results=[(0, "")])
    with open(managed, "w") as f:
        f.write("previous-good-config")

    ok, msg = wifi_manager.apply_networks([])
    assert ok is True
    assert msg == "No WiFi networks configured."
    assert not os.path.exists(managed)
