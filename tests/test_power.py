"""Tests for power.py: PiSugar reply parsing and the power-aware sync decision."""
import power


def test_parse_value():
    assert power._parse_value("battery: 87.5", "battery") == "87.5"
    assert power._parse_value("battery_charging: true", "battery_charging") == "true"
    assert power._parse_value("other: 1", "battery") is None
    assert power._parse_value("", "battery") is None
    assert power._parse_value(None, "battery") is None


def test_sync_allowed_unreadable_fails_open():
    ok, reason = power.sync_allowed(35, battery={"percent": None, "charging": None})
    assert ok is True
    assert reason == ""


def test_sync_allowed_charging_bypasses_threshold():
    ok, _ = power.sync_allowed(35, battery={"percent": 10, "charging": True})
    assert ok is True


def test_sync_allowed_low_battery_refuses():
    ok, reason = power.sync_allowed(35, battery={"percent": 20, "charging": False})
    assert ok is False
    assert "20" in reason


def test_sync_allowed_above_threshold():
    ok, _ = power.sync_allowed(35, battery={"percent": 80, "charging": False})
    assert ok is True
