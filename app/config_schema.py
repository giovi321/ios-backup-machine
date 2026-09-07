#!/usr/bin/env python3
"""
config_schema.py - Single source of truth for the config.yaml structure.

Replaces the ad-hoc ``setdefault`` chains that were duplicated across webui.py
and every per-script loader. Provides:

- ``DEFAULTS``      : the canonical default tree (every key the app reads).
- ``apply_defaults``: deep-merge defaults under a config (existing values win).
- ``migrate``       : versioned, ordered migration step run once on update.
- ``load_config``   : read + migrate + default-fill. Fail-safe: a missing,
                      corrupt, or non-dict file yields defaults (the bad file is
                      preserved as config.yaml.bad-*) and the problems are
                      recorded in ``LAST_LOAD_WARNINGS`` / ``LAST_LOAD_DEGRADED``.
- ``atomic_save``   : tmp + fsync + os.replace, so a power loss can't truncate
                      config.yaml (the bug this module fixes).

Import-safe: depends only on the stdlib + PyYAML (no hardware modules), so it
can be unit-tested on any machine.
"""
import os
import copy
import shutil
import threading
import time

import yaml

CONFIG_PATH = os.getenv("IOSBACKUP_CONFIG", "/root/iosbackupmachine/config.yaml")

# Bump whenever the schema changes in a way that needs a migration step below.
CONFIG_VERSION = 4

# Result of the most recent load_config(): problems found (and repaired) while
# reading the file, and whether the on-disk config had to be discarded entirely.
# load_config keeps returning a plain dict; readers that care about the degraded
# state (the web UI banner, the setup-wizard gate) use the accessors below.
_STATE_LOCK = threading.Lock()
LAST_LOAD_WARNINGS = []
LAST_LOAD_DEGRADED = False


def get_load_warnings():
    with _STATE_LOCK:
        return list(LAST_LOAD_WARNINGS)


def was_load_degraded():
    """True when the last load found the file missing/unreadable/non-dict and
    fell back to defaults — i.e. any saved settings (including setup state) are
    gone, not just one mistyped key."""
    with _STATE_LOCK:
        return LAST_LOAD_DEGRADED


def _record_load_result(warnings, degraded):
    global LAST_LOAD_WARNINGS, LAST_LOAD_DEGRADED
    with _STATE_LOCK:
        LAST_LOAD_WARNINGS = list(warnings)
        LAST_LOAD_DEGRADED = degraded

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
    "backup": {"auto_start": True, "notify_on_rejected": True,
               # hang_timeout_sec: idevicebackup2 silent this long -> considered hung, killed.
               # max_duration_sec: total cap for one backup run.
               "hang_timeout_sec": 600, "max_duration_sec": 4 * 3600,
               # min_battery_percent: a backup started below this is a backup cut
               # mid-write by PiSugar's own 30% auto-shutdown. Same value and same
               # reasoning as sync.min_battery_percent below; 0 disables.
               "min_battery_percent": 35,
               # min_free_mb: mid-run free-space reserve for the backup drive. Also
               # raises the pre-backup floor when set above it, so a run can never
               # start and then abort on its own first poll. 0 disables the drive
               # check, never the rootfs one.
               "min_free_mb": 512,
               # notify_stale / stale_after_sec: every other notification is edge-
               # triggered, so a device that quietly stops backing up (phone no longer
               # plugged in, auto_start switched off, the filter rejecting) tells nobody
               # until a restore is needed. One alert per quiet episode, re-armed by the
               # next successful backup. 0 seconds disables the check entirely.
               "notify_stale": True,
               "stale_after_sec": 7 * 24 * 3600},
    "backup_encryption": {"encryption_confirmed": False},
    "device_filter": {"enabled": False, "allowed_devices": []},
    # networks: list of {nickname, ssid, password}. The legacy single ssid/password
    # are kept for backward-compat reads; the v2 migration seeds networks from them.
    "wifi": {"enabled": False, "ssid": "", "password": "", "networks": []},
    "ntp": {"enabled": True, "servers": ["pool.ntp.org", "time.google.com"]},
    "webui": {"enabled": True, "port": 8080, "bind_interfaces": ["all"], "secret_key": "change-me"},
    "notifications": {
        "webhook": {"enabled": False, "url": "",
                    "events": ["backup_complete", "backup_error", "backup_stale"],
                    "auth_enabled": False, "auth_header": "Authorization"},
        "mqtt": {"enabled": False, "broker": "", "port": 1883, "username": "", "password": "",
                 "topic_prefix": "iosbackupmachine",
                 "events": ["backup_complete", "backup_error", "backup_stale"]},
    },
    # full_tunnel: route ALL traffic (incl. the local subnet) through the VPN, so a
    # sync server whose IP overlaps the WiFi subnet is reachable over the tunnel.
    # Requires AllowedIPs=0.0.0.0/0 in the WireGuard config. Enforced on every connect.
    "wireguard": {"enabled": False, "auto_connect": False, "auto_connect_on": ["iphone"],
                  "interface_name": "wg0", "full_tunnel": False},
    "credential_encryption": {"passphrase_mode": "udid"},
    # min_battery_percent: power-aware sync refuses to start / auto-aborts below
    # this when not charging. Comfortably above PiSugar's 30% auto-shutdown.
    "sync": {"enabled": False, "auto_sync": False, "allowed_network": "any", "min_battery_percent": 35,
             # max_seconds: overall cap for one sync run. 0 = no cap, the default:
             # the scan/stall watchdogs already kill a sync that has stopped moving,
             # and a first sync of a large backup set legitimately runs for many
             # hours. Settable in the web UI (Remote Sync).
             "max_seconds": 0},
}


def _types_match(default, value):
    # bool is a subclass of int in Python — keep them distinct so `true` in
    # config.yaml doesn't pass where a port number is expected (or vice versa).
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, int):
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, type(default))


def _deep_merge(defaults, current, warnings=None, path=""):
    """Recursively merge ``defaults`` under ``current`` — existing values win.
    Returns a new dict; inputs are not mutated. A user value whose type doesn't
    match the default's (or that is null) is replaced by the default, with a
    note appended to ``warnings`` when a list is given."""
    result = copy.deepcopy(defaults)
    for k, v in (current or {}).items():
        key_path = f"{path}.{k}" if path else k
        if k in result:
            d = result[k]
            if isinstance(d, dict) and isinstance(v, dict):
                result[k] = _deep_merge(d, v, warnings, key_path)
            elif v is None or not _types_match(d, v):
                if warnings is not None:
                    warnings.append(f"'{key_path}' has an invalid value; using the default")
            else:
                result[k] = copy.deepcopy(v)
        else:
            result[k] = copy.deepcopy(v)
    return result


def apply_defaults(cfg, warnings=None):
    """Return ``cfg`` with every missing default filled in (existing values win)."""
    return _deep_merge(DEFAULTS, cfg or {}, warnings)


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


def _migrate_2_to_3(cfg):
    # v3 introduces the backup_stale event (no successful backup for a week).
    # Adding it to DEFAULTS alone reaches FRESH INSTALLS ONLY: _deep_merge treats a
    # saved events list as type-matching and copies it wholesale, and install.sh
    # merges with the saved config winning — so every device that already has
    # notifications configured, i.e. every device that would benefit, would never
    # see the event. Only lists that already opted into backup_error are touched: a
    # list deliberately narrowed to successes is a choice, and an empty list already
    # means all events. Idempotent.
    notif = cfg.get("notifications")
    if not isinstance(notif, dict):
        return cfg
    for channel in ("webhook", "mqtt"):
        ch = notif.get(channel)
        if not isinstance(ch, dict):
            continue
        events = ch.get("events")
        if not isinstance(events, list):
            continue
        if "backup_error" in events and "backup_stale" not in events:
            events.append("backup_stale")
    return cfg


def _migrate_3_to_4(cfg):
    # v4 turns the overall sync cap off by default. v3 shipped 3600 as a value
    # nobody could change without editing the file by hand, and a first sync of a
    # large backup set hits it while rsync is still nowhere near done - the abort
    # then reads as a failure. Clear only that exact value; any other number was
    # chosen deliberately and is left alone. Idempotent.
    sync = cfg.get("sync")
    if isinstance(sync, dict) and sync.get("max_seconds") == 3600:
        sync["max_seconds"] = 0
    return cfg


_MIGRATIONS = {
    0: _migrate_0_to_1,
    1: _migrate_1_to_2,
    2: _migrate_2_to_3,
    3: _migrate_3_to_4,
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


def _backup_corrupt_config(path, warnings):
    """Best-effort copy of the unreadable config next to the original."""
    dst = f"{path}.bad-{time.strftime('%Y%m%d-%H%M%S')}"
    try:
        shutil.copy2(path, dst)
        warnings.append(f"the original file was saved as {os.path.basename(dst)}")
    except OSError:
        pass


def load_config(path=None):
    """Read, migrate, and default-fill the config at ``path``.

    Fail-safe: any read/parse/shape failure (corrupt YAML, a truncated file
    that parses to a scalar, a non-dict document) yields defaults + migration
    instead of an exception. The bad file is preserved as ``config.yaml.bad-*``
    and the problems are recorded for get_load_warnings()/was_load_degraded().
    """
    path = path or CONFIG_PATH
    warnings = []
    degraded = False
    try:
        with open(path, "r") as f:
            cfg = yaml.safe_load(f)
    except FileNotFoundError:
        warnings.append(f"{os.path.basename(path)} was not found; defaults are in effect")
        degraded = True
        cfg = {}
    except (yaml.YAMLError, AttributeError, TypeError, OSError) as e:
        warnings.append(f"{os.path.basename(path)} could not be parsed ({e}); defaults are in effect")
        _backup_corrupt_config(path, warnings)
        degraded = True
        cfg = {}
    if cfg is None:
        cfg = {}
    if not isinstance(cfg, dict):
        # Valid YAML that isn't a mapping (e.g. a truncated file parsing to a
        # scalar or list) is just as unusable as unparseable YAML.
        warnings.append(f"{os.path.basename(path)} did not contain a settings mapping; defaults are in effect")
        _backup_corrupt_config(path, warnings)
        degraded = True
        cfg = {}
    try:
        cfg = migrate(cfg)
    except (AttributeError, TypeError) as e:
        warnings.append(f"migrating {os.path.basename(path)} failed ({e}); defaults are in effect")
        _backup_corrupt_config(path, warnings)
        degraded = True
        cfg = migrate({})
    cfg = apply_defaults(cfg, warnings)
    _record_load_result(warnings, degraded)
    return cfg


_SAVE_LOCK = threading.Lock()


def atomic_save(cfg, path=None):
    """Write config atomically: tmp file + fsync + os.replace.
    A power loss mid-write leaves the previous config intact. Serialized across
    threads, with a per-call tmp name, so concurrent saves can't clobber each
    other's temp file."""
    path = path or CONFIG_PATH
    tmp = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}.{os.urandom(4).hex()}"
    try:
        with _SAVE_LOCK:
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
            # Windows briefly locks the destination (AV/indexer) right after a
            # replace; retry the rename a few times. Never triggers on Linux.
            for attempt in range(5):
                try:
                    os.replace(tmp, path)
                    break
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.05)
            # fsync the directory so the rename itself survives a power loss.
            try:
                dfd = os.open(d or ".", os.O_RDONLY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
            except OSError:
                pass
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
