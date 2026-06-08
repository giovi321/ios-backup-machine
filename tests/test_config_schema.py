"""Tests for config_schema: defaults, migration, and atomic save/load round-trip."""
import yaml

import config_schema


def test_apply_defaults_fills_missing():
    cfg = config_schema.apply_defaults({})
    assert cfg["config_version"] == config_schema.CONFIG_VERSION
    assert cfg["sync"]["min_battery_percent"] == 35
    assert cfg["backup"]["auto_start"] is True


def test_existing_values_win_with_sibling_fill():
    cfg = config_schema.apply_defaults(
        {"backup_dir": "/custom/", "sync": {"min_battery_percent": 50}})
    assert cfg["backup_dir"] == "/custom/"
    assert cfg["sync"]["min_battery_percent"] == 50      # user value preserved
    assert cfg["sync"]["allowed_network"] == "any"       # sibling default still filled


def test_input_not_mutated():
    src = {"sync": {"enabled": True}}
    out = config_schema.apply_defaults(src)
    assert "min_battery_percent" not in src["sync"]       # input untouched
    assert out["sync"]["min_battery_percent"] == 35


def test_migrate_stamps_version():
    cfg = config_schema.migrate({"setup_completed": True})
    assert cfg["config_version"] == config_schema.CONFIG_VERSION


def test_load_migrate_save_round_trip(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump({"owner_lines": ["X"], "sync": {"enabled": True}}))
    cfg = config_schema.load_config(str(p))
    assert cfg["owner_lines"] == ["X"]
    assert cfg["sync"]["enabled"] is True
    assert cfg["config_version"] == config_schema.CONFIG_VERSION

    cfg["webui"]["port"] = 9999
    config_schema.atomic_save(cfg, str(p))
    assert config_schema.load_config(str(p))["webui"]["port"] == 9999


def test_load_missing_file_returns_defaults(tmp_path):
    cfg = config_schema.load_config(str(tmp_path / "does-not-exist.yaml"))
    assert cfg["config_version"] == config_schema.CONFIG_VERSION
    assert cfg["setup_completed"] is False


def test_wifi_migration_seeds_networks_from_legacy_single():
    cfg = config_schema.apply_defaults(config_schema.migrate(
        {"config_version": 1, "wifi": {"enabled": True, "ssid": "Home", "password": "pw"}}))
    assert cfg["wifi"]["networks"] == [{"nickname": "", "ssid": "Home", "password": "pw"}]


def test_wifi_migration_keeps_existing_networks():
    nets = [{"nickname": "A", "ssid": "X", "password": "1"},
            {"nickname": "B", "ssid": "Y", "password": "2"}]
    cfg = config_schema.apply_defaults(config_schema.migrate(
        {"config_version": 2, "wifi": {"enabled": True, "ssid": "X", "password": "1",
                                       "networks": nets}}))
    assert cfg["wifi"]["networks"] == nets


def test_wifi_defaults_have_empty_networks_list():
    cfg = config_schema.apply_defaults({})
    assert cfg["wifi"]["networks"] == []
