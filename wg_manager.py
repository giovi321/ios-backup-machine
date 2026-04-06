#!/usr/bin/env python3
"""
wg_manager.py - WireGuard client management for iOS Backup Machine.

Handles starting/stopping the WireGuard interface using wg-quick,
and generating/applying configs from the encrypted store.
"""
import os, sys, subprocess, tempfile
import wg_crypto

WG_TEMP_CONF = "/tmp/wg_iosbackup.conf"

def is_interface_up(iface="wg0"):
    """Check if the WireGuard interface is up."""
    try:
        out = subprocess.run(
            ["ip", "link", "show", iface],
            capture_output=True, text=True, timeout=5
        )
        return out.returncode == 0
    except Exception:
        return False

def start_wireguard(iface="wg0", passphrase=None, udid=None):
    """Decrypt WireGuard config and bring up the interface."""
    cfg = wg_crypto.decrypt_wg_config(passphrase=passphrase, udid=udid)
    if not cfg:
        print("[WG] Cannot decrypt WireGuard config.", file=sys.stderr)
        return False

    wg_conf = cfg.get("wg_conf", "")
    if not wg_conf:
        print("[WG] No WireGuard config content found.", file=sys.stderr)
        return False

    # Write temporary config
    conf_path = f"/etc/wireguard/{iface}.conf"
    try:
        os.makedirs("/etc/wireguard", exist_ok=True)
        with open(conf_path, "w") as f:
            f.write(wg_conf)
        os.chmod(conf_path, 0o600)
    except Exception as e:
        print(f"[WG] Cannot write config: {e}", file=sys.stderr)
        return False

    # Bring up
    try:
        r = subprocess.run(["wg-quick", "up", iface],
                           capture_output=True, text=True, timeout=15)
        if r.returncode == 0:
            print(f"[WG] Interface {iface} is up.")
            return True
        else:
            print(f"[WG] wg-quick up failed: {r.stderr}", file=sys.stderr)
            return False
    except FileNotFoundError:
        print("[WG] wg-quick not found. Install wireguard-tools.", file=sys.stderr)
        return False

def stop_wireguard(iface="wg0"):
    """Bring down the WireGuard interface."""
    try:
        r = subprocess.run(["wg-quick", "down", iface],
                           capture_output=True, text=True, timeout=15)
        if r.returncode == 0:
            print(f"[WG] Interface {iface} is down.")
            return True
        else:
            print(f"[WG] wg-quick down failed: {r.stderr}", file=sys.stderr)
            return False
    except FileNotFoundError:
        return False

def get_wireguard_status(iface="wg0"):
    """Get WireGuard interface status."""
    if not is_interface_up(iface):
        return {"up": False}

    try:
        r = subprocess.run(["wg", "show", iface],
                           capture_output=True, text=True, timeout=5)
        return {"up": True, "details": r.stdout.strip()}
    except Exception:
        return {"up": True, "details": ""}
