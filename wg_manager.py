#!/usr/bin/env python3
"""wg_manager.py - WireGuard client management."""
import os, sys, subprocess
import wg_crypto

def is_interface_up(iface="wg0"):
    try:
        return subprocess.run(["ip", "link", "show", iface], capture_output=True, text=True, timeout=5).returncode == 0
    except Exception:
        return False

def start_wireguard(iface="wg0", passphrase=None):
    cfg = wg_crypto.decrypt_wg_config(passphrase=passphrase)
    if not cfg:
        return False
    wg_conf = cfg.get("wg_conf", "")
    if not wg_conf:
        return False
    conf_path = f"/etc/wireguard/{iface}.conf"
    try:
        os.makedirs("/etc/wireguard", exist_ok=True)
        with open(conf_path, "w") as f:
            f.write(wg_conf)
        os.chmod(conf_path, 0o600)
    except Exception:
        return False
    try:
        r = subprocess.run(["wg-quick", "up", iface], capture_output=True, text=True, timeout=15)
        return r.returncode == 0
    except Exception:
        return False

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
