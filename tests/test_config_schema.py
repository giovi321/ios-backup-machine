"""Tests for config_schema: defaults, migration, and atomic save/load round-trip."""
import threading

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


def test_wireguard_full_tunnel_defaults_off():
    cfg = config_schema.apply_defaults({})
    assert cfg["wireguard"]["full_tunnel"] is False
    # existing value preserved
    cfg2 = config_schema.apply_defaults({"wireguard": {"full_tunnel": True}})
    assert cfg2["wireguard"]["full_tunnel"] is True


def test_backup_safety_keys_have_defaults():
    cfg = config_schema.apply_defaults({})
    # 35 deliberately matches sync.min_battery_percent: one battery policy for the
    # whole appliance, and comfortably above PiSugar's 30% auto-shutdown.
    assert cfg["backup"]["min_battery_percent"] == 35
    assert cfg["backup"]["min_free_mb"] == 512


def test_backup_safety_keys_keep_configured_values():
    cfg = config_schema.apply_defaults(
        {"backup": {"min_battery_percent": 20, "min_free_mb": 2048}})
    assert cfg["backup"]["min_battery_percent"] == 20
    assert cfg["backup"]["min_free_mb"] == 2048


def test_a_mistyped_backup_safety_key_falls_back():
    cfg = config_schema.apply_defaults(
        {"backup": {"min_battery_percent": "35", "min_free_mb": None}})
    assert cfg["backup"]["min_battery_percent"] == 35
    assert cfg["backup"]["min_free_mb"] == 512


def test_load_missing_file_records_degraded(tmp_path):
    cfg = config_schema.load_config(str(tmp_path / "does-not-exist.yaml"))
    assert cfg["config_version"] == config_schema.CONFIG_VERSION
    assert config_schema.was_load_degraded() is True
    assert config_schema.get_load_warnings()


def test_load_corrupt_yaml_returns_defaults_and_backs_up(tmp_path):
    p = tmp_path / "config.yaml"
    original = "wifi: [unclosed\n"
    p.write_text(original)
    cfg = config_schema.load_config(str(p))
    assert cfg["config_version"] == config_schema.CONFIG_VERSION
    assert cfg["setup_completed"] is False
    warnings = config_schema.get_load_warnings()
    assert any("could not be parsed" in w for w in warnings)
    assert any("config.yaml.bad-" in w for w in warnings)
    assert config_schema.was_load_degraded() is True
    bad = list(tmp_path.glob("config.yaml.bad-*"))
    assert len(bad) == 1
    assert bad[0].read_text() == original


def test_load_non_dict_yaml_returns_defaults_and_backs_up(tmp_path):
    for content in ("- just\n- a\n- list\n", "just a scalar\n"):
        for old in tmp_path.glob("config.yaml.bad-*"):
            old.unlink()
        p = tmp_path / "config.yaml"
        p.write_text(content)
        cfg = config_schema.load_config(str(p))
        assert cfg["config_version"] == config_schema.CONFIG_VERSION
        assert any("did not contain a settings mapping" in w
                   for w in config_schema.get_load_warnings())
        assert config_schema.was_load_degraded() is True
        assert len(list(tmp_path.glob("config.yaml.bad-*"))) == 1


def test_wrong_type_values_replaced_with_default_and_warning():
    warnings = []
    cfg = config_schema.apply_defaults(
        {"wifi": "foo", "webui": {"port": "abc"}}, warnings)
    assert cfg["wifi"] == config_schema.DEFAULTS["wifi"]
    assert cfg["webui"]["port"] == 8080
    assert any("'wifi'" in w for w in warnings)
    assert any("'webui.port'" in w for w in warnings)


def test_load_with_wrong_types_warns_but_is_not_degraded(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump({"setup_completed": True, "webui": {"port": "abc"}}))
    cfg = config_schema.load_config(str(p))
    assert cfg["webui"]["port"] == 8080
    assert config_schema.get_load_warnings()
    assert config_schema.was_load_degraded() is False


def test_bool_is_not_accepted_where_int_expected_and_vice_versa():
    warnings = []
    cfg = config_schema.apply_defaults(
        {"webui": {"port": True}, "backup": {"auto_start": 1}}, warnings)
    assert cfg["webui"]["port"] == 8080
    assert cfg["backup"]["auto_start"] is True
    assert any("'webui.port'" in w for w in warnings)
    assert any("'backup.auto_start'" in w for w in warnings)


def test_null_values_fall_back_to_default():
    warnings = []
    cfg = config_schema.apply_defaults({"backup_dir": None, "ntp": None}, warnings)
    assert cfg["backup_dir"] == "/media/iosbackup/"
    assert cfg["ntp"]["enabled"] is True
    assert len(warnings) == 2


def test_extra_keys_and_correct_types_still_win():
    warnings = []
    cfg = config_schema.apply_defaults(
        {"custom_key": 1, "webui": {"port": 9090}}, warnings)
    assert cfg["custom_key"] == 1
    assert cfg["webui"]["port"] == 9090
    assert warnings == []


def test_atomic_save_is_safe_under_concurrency(tmp_path):
    p = tmp_path / "config.yaml"
    errors = []

    def worker(i):
        try:
            for n in range(20):
                config_schema.atomic_save(
                    {"config_version": 2, "value": i * 100 + n}, str(p))
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    data = yaml.safe_load(p.read_text())
    assert isinstance(data, dict) and "value" in data
    assert not list(tmp_path.glob("config.yaml.tmp.*"))


# --- backup_stale event and its config keys -------------------------------------

def test_defaults_carry_the_staleness_keys():
    bk = config_schema.DEFAULTS["backup"]
    assert bk["notify_stale"] is True
    assert bk["stale_after_sec"] == 7 * 24 * 3600
    assert isinstance(bk["stale_after_sec"], int)
    for ch in ("webhook", "mqtt"):
        assert "backup_stale" in config_schema.DEFAULTS["notifications"][ch]["events"]


def test_the_migration_reaches_devices_that_already_have_notifications():
    # DEFAULTS alone reaches fresh installs only: _deep_merge copies a saved list
    # wholesale, so without this every device that would benefit gets nothing.
    cfg = {"notifications": {
        "webhook": {"events": ["backup_complete", "backup_error"]},
        "mqtt": {"events": ["backup_error"]},
    }}
    config_schema._migrate_2_to_3(cfg)
    assert cfg["notifications"]["webhook"]["events"] == [
        "backup_complete", "backup_error", "backup_stale"]
    assert cfg["notifications"]["mqtt"]["events"] == ["backup_error", "backup_stale"]
    # Idempotent: a second pass must not append it twice.
    config_schema._migrate_2_to_3(cfg)
    assert cfg["notifications"]["webhook"]["events"].count("backup_stale") == 1


def test_the_migration_leaves_a_deliberately_narrowed_list_alone():
    # A list without backup_error is someone who wants successes only; forcing
    # the event on them is the kind of surprise this branch exists to remove.
    cfg = {"notifications": {
        "webhook": {"events": ["backup_complete"]},
        "mqtt": {"events": []},                 # empty already means all events
    }}
    config_schema._migrate_2_to_3(cfg)
    assert cfg["notifications"]["webhook"]["events"] == ["backup_complete"]
    assert cfg["notifications"]["mqtt"]["events"] == []


def test_the_migration_does_not_choke_on_a_junk_shape():
    cfg = {"notifications": {"webhook": {"events": "backup_error"}, "mqtt": "nope"}}
    config_schema._migrate_2_to_3(cfg)
    assert cfg["notifications"]["webhook"]["events"] == "backup_error"
    config_schema._migrate_2_to_3({"notifications": None})
    config_schema._migrate_2_to_3({})


def test_a_v2_config_comes_out_at_v3_with_the_new_keys(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump({
        "config_version": 2, "setup_completed": True,
        "notifications": {"webhook": {"events": ["backup_error"]}},
    }))
    cfg = config_schema.load_config(str(p))
    assert cfg["config_version"] == 3
    assert cfg["backup"]["notify_stale"] is True
    assert cfg["backup"]["stale_after_sec"] == 7 * 24 * 3600
    assert "backup_stale" in cfg["notifications"]["webhook"]["events"]


def test_a_junk_stale_threshold_falls_back_to_the_default(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump({"backup": {"stale_after_sec": "soon"}}))
    cfg = config_schema.load_config(str(p))
    assert cfg["backup"]["stale_after_sec"] == 7 * 24 * 3600
