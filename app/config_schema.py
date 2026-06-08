#!/usr/bin/env python3
"""
config_schema.py - Single source of truth for the config.yaml structure.

Replaces the ad-hoc ``setdefault`` chains that were duplicated across webui.py
and every per-script loader. Provides:

- ``DEFAULTS``      : the canonical default tree (every key the app reads).
- ``apply_defaults``: deep-merge defaults under a config (existing values win).
- ``migrate``       : versioned, ordered migration step run once on update.
- ``load_config``   : read + migrate + default-fill.
- ``atomic_save``   : tmp + fsync + os.replace, so a power loss can't truncate
                      config.yaml (the bug this module fixes).

Import-safe: depends only on the stdlib + PyYAML (no hardware modules), so it
can be unit-tested on any machine.
"""
import os
import copy

import yaml

CONFIG_PATH = os.getenv("IOSBACKUP_CONFIG", "/root/iosbackupmachine/config.yaml")

# Bump whenever the schema changes in a way that needs a migration step below.
CONFIG_VERSION = 2

# Canonical defaults. Every key the app reads should appear here so a fresh or
# partial config becomes a complete, valid tree after apply_defaults().
DEFAULTS = {
    "config_version": CONFIG_VERSION,
    "setup_completed": False,
    "backup_dir": "/media/iosbackup/",
    "marker_file": ".foldermarker",
    "disk_device": "/dev/mmcblk1",
    "orientation": "landscape_right",
    "font_path": "/root/iosbackupmachine/UbuntuMono-Regular.ttf",
    "owner_lines": ["Name", "telephone", "email", "message"],
    "error_codes": {},
    "env": {},
    "auth": {"password_hash": ""},
    "backup": {"auto_start": True, "notify_on_rejected": True},
    "backup_encryption": {"encryption_confirmed": False},
    "device_filter": {"enabled": False, "allowed_devices": []},
    # networks: list of {nickname, ssid, password}. The legacy single ssid/password
    # are kept for backward-compat reads; the v2 migration seeds networks from them.
    "wifi": {"enabled": False, "ssid": "", "password": "", "networks": []},
    "ntp": {"enabled": True, "servers": ["pool.ntp.org", "time.google.com"]},
    "webui": {"enabled": True, "port": 8080, "bind_interfaces": ["all"], "secret_key": "change-me"},
    "notifications": {
        "webhook": {"enabled": False, "url": "", "events": ["backup_complete", "backup_error"],
                    "auth_enabled": False, "auth_header": "Authorization"},
        "mqtt": {"enabled": False, "broker": "", "port": 1883, "username": "", "password": "",
                 "topic_prefix": "iosbackupmachine", "events": ["backup_complete", "backup_error"]},
    },
    "wireguard": {"enabled": False, "auto_connect": False, "auto_connect_on": ["iphone"],
                  "interface_name": "wg0"},
    "credential_encryption": {"passphrase_mode": "udid"},
    # min_battery_percent: power-aware sync refuses to start / auto-aborts below
    # this when not charging. Comfortably above PiSugar's 30% auto-shutdown.
    "sync": {"enabled": False, "auto_sync": False, "allowed_network": "any", "min_battery_percent": 35},
}


def _deep_merge(defaults, current):
    """Recursively merge ``defaults`` under ``current`` — existing values win.
    Returns a new dict; inputs are not mutated."""
    result = copy.deepcopy(defaults)
    for k, v in (current or {}).items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = copy.deepcopy(v)
    return result


def apply_defaults(cfg):
    """Return ``cfg`` with every missing default filled in (existing values win)."""
    return _deep_merge(DEFAULTS, cfg or {})


# --- Migrations -------------------------------------------------------------
# Each entry transforms a config from version N to N+1, in place. Pure-dict ops
# only (no I/O). apply_defaults() runs afterwards to fill any new keys, so a
# migration only needs to handle *renames / restructures*, not additions.

def _migrate_0_to_1(cfg):
    # v1 introduces config_version tracking and sync.min_battery_percent.
    # Both are additive (apply_defaults fills them), so nothing to restructure.
    return cfg


def _migrate_1_to_2(cfg):
    # v2 introduces wifi.networks — a list of {nickname, ssid, password} so the
    # device can roam between several configured WiFi networks. Seed it from the
    # single legacy ssid/password so an existing setup keeps its network.
    wifi = cfg.get("wifi")
    if isinstance(wifi, dict) and not wifi.get("networks"):
        ssid = (wifi.get("ssid") or "").strip()
        if ssid:
            wifi["networks"] = [{
                "nickname": "",
                "ssid": ssid,
                "password": wifi.get("password", ""),
            }]
    return cfg


_MIGRATIONS = {
    0: _migrate_0_to_1,
    1: _migrate_1_to_2,
}


def migrate(cfg):
    """Bring ``cfg`` up to CONFIG_VERSION. Returns the migrated dict (mutated)."""
    cfg = cfg or {}
    ver = cfg.get("config_version", 0)
    try:
        ver = int(ver)
    except (TypeError, ValueError):
        ver = 0
    while ver < CONFIG_VERSION:
        step = _MIGRATIONS.get(ver)
        if step:
            cfg = step(cfg) or cfg
        ver += 1
    cfg["config_version"] = CONFIG_VERSION
    return cfg


def load_config(path=None):
    """Read, migrate, and default-fill the config at ``path``."""
    path = path or CONFIG_PATH
    try:
        with open(path, "r") as f:
            cfg = yaml.safe_load(f) or {}
    except FileNotFoundError:
        cfg = {}
    cfg = migrate(cfg)
    cfg = apply_defaults(cfg)
    return cfg


def atomic_save(cfg, path=None):
    """Write config atomically: tmp file + fsync + os.replace.
    A power loss mid-write leaves the previous config intact."""
    path = path or CONFIG_PATH
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(tmp, "w") as f:
            yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, path)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise


def migrate_file(path=None):
    """Load, migrate / default-fill, and atomically re-save the config file.
    This is the single migration step install.sh / update.sh call on update."""
    path = path or CONFIG_PATH
    cfg = load_config(path)
    atomic_save(cfg, path)
    return cfg


if __name__ == "__main__":
    # `python3 config_schema.py [path]` — migrate a config file in place.
    import sys
    migrate_file(sys.argv[1] if len(sys.argv) > 1 else None)
    print(f"Migrated config to version {CONFIG_VERSION}")
