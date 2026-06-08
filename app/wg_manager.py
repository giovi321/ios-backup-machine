#!/usr/bin/env python3
"""wg_manager.py - WireGuard client management."""
import os, sys, subprocess
import yaml
import wg_crypto

CONFIG_PATH = os.getenv("IOSBACKUP_CONFIG", "/root/iosbackupmachine/config.yaml")


def _full_tunnel_enabled():
    try:
        with open(CONFIG_PATH) as f:
            cfg = yaml.safe_load(f) or {}
        return bool(cfg.get("wireguard", {}).get("full_tunnel"))
    except Exception:
        return False


def enforce_full_tunnel():
    """Route ALL traffic — including the local WiFi subnet — through the VPN.

    For AllowedIPs=0.0.0.0/0, wg-quick installs an `ip rule ... suppress_prefixlength 0`
    so destinations that match a specific main-table route (e.g. the local WiFi
    subnet, or a sync server whose IP overlaps it) skip the tunnel. Deleting that
    rule sends them through the tunnel too. wg's own (fwmark'd) handshake traffic
    still uses the main table, so the link stays up.

    Best-effort and idempotent: re-applied on every connect (called from
    start_wireguard), so it survives reconnects after errors/disconnects. No-op if
    the rule isn't present (e.g. a split-tunnel config). Returns rules removed."""
    removed = 0
    for base in (["ip", "rule", "del"], ["ip", "-6", "rule", "del"]):
        for _ in range(8):   # there can be several instances; remove them all
            try:
                rc = subprocess.run(base + ["table", "main", "suppress_prefixlength", "0"],
                                    capture_output=True, timeout=5).returncode
            except Exception:
                break
            if rc != 0:
                break
            removed += 1
    if removed:
        print(f"[WG] Full tunnel: removed {removed} LAN-bypass rule(s)", flush=True)
    return removed


def is_interface_up(iface="wg0"):
    try:
        return subprocess.run(["ip", "link", "show", iface], capture_output=True, text=True, timeout=5).returncode == 0
    except Exception:
        return False

def start_wireguard(iface="wg0", passphrase=None):
    """Decrypt WireGuard config and bring up the interface.
    Returns (success: bool, error: str or None)."""
    cfg = wg_crypto.decrypt_wg_config(passphrase=passphrase)
    if not cfg:
        serial = wg_crypto.get_iphone_serial()
        udid = wg_crypto.get_iphone_udid()
        if not udid:
            return False, "Cannot decrypt: no iPhone connected."
        elif not serial:
            return False, "iPhone connected but cannot read serial number. Unlock the iPhone and tap Trust."
        else:
            return False, "Decryption failed. Was the config encrypted with a different iPhone or passphrase?"

    wg_conf = cfg.get("wg_conf", "")
    if not wg_conf:
        return False, "Encrypted config is empty (no wg_conf key)."

    conf_path = f"/etc/wireguard/{iface}.conf"
    try:
        os.makedirs("/etc/wireguard", exist_ok=True)
        with open(conf_path, "w") as f:
            f.write(wg_conf)
        os.chmod(conf_path, 0o600)
    except Exception as e:
        return False, f"Cannot write config to {conf_path}: {e}"

    try:
        r = subprocess.run(["wg-quick", "up", iface], capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            # Force every connect (incl. reconnects after errors/disconnects) to
            # route everything through the tunnel when full_tunnel is enabled.
            if _full_tunnel_enabled():
                enforce_full_tunnel()
            return True, None
        else:
            # wg-quick prints commands to stderr; the actual error is usually the last few lines
            output = (r.stderr or "") + (r.stdout or "")
            # Extract only the last meaningful lines (skip the [#] command echo lines)
            lines = [ln for ln in output.strip().splitlines() if not ln.startswith("[#]")]
            err = "\n".join(lines[-5:]) if lines else output.strip()[-500:]
            return False, f"wg-quick failed: {err}"
    except FileNotFoundError:
        return False, "wg-quick not found. Install wireguard-tools."
    except subprocess.TimeoutExpired:
        return False, "wg-quick timed out after 15s."
    except Exception as e:
        return False, f"Unexpected error: {e}"

def stop_wireguard(iface="wg0"):
    try:
        return subprocess.run(["wg-quick", "down", iface], capture_output=True, text=True, timeout=15).returncode == 0
    except Exception:
        return False

def get_wireguard_status(iface="wg0"):
    if not is_interface_up(iface):
        return {"up": False}
    try:
        r = subprocess.run(["wg", "show", iface], capture_output=True, text=True, timeout=5)
        return {"up": True, "details": r.stdout.strip()}
    except Exception:
        return {"up": True, "details": ""}
