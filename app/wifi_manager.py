#!/usr/bin/env python3
"""
wifi_manager.py - WiFi configuration via netplan (systemd-networkd + wpa_supplicant).

This device has no NetworkManager, so the previous nmcli-based path could never
work. WiFi here is managed by netplan: we own a single drop-in,
/etc/netplan/90-iosbackup-wifi.yaml, that lists every configured network as an
access-point under the wireless interface. wpa_supplicant then associates to
whichever configured network is in range — so "connect to whatever's available"
and roaming between known networks are handled by the supplicant itself.

Design notes:
- We never edit the OS's own netplan files; ours sorts last (90-) so on an SSID
  clash our password wins, and netplan merges the rest. A network defined only in
  an OS file therefore stays available even if removed from our list (documented
  caveat) — the upside is the device can't lose its existing connection if our
  config is wrong.
- apply_networks validates with `netplan generate` before `netplan apply`, and
  restores the previous managed file on failure, so a bad config can't knock a
  portable device off the network.
- netplan files must be mode 0600 (they hold WiFi PSKs); netplan warns otherwise.
"""
import os
import glob
import time
import subprocess

import yaml

NETPLAN_DIR = "/etc/netplan"
MANAGED_FILE = os.path.join(NETPLAN_DIR, "90-iosbackup-wifi.yaml")


def _run(args, timeout=90):
    """Run a command; return (rc, combined_output). Never raises."""
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()
    except FileNotFoundError:
        return 127, f"{args[0]} not found"
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    except Exception as e:  # pragma: no cover - defensive
        return 1, str(e)


def get_wifi_interface():
    """Return the wireless interface name (e.g. wlan0), or None.

    Reads /sys/class/net/*/wireless, which exists only for wireless devices — no
    external tool required."""
    try:
        for path in sorted(glob.glob("/sys/class/net/*/wireless")):
            return path.split(os.sep)[-2]
    except Exception:
        pass
    return None


def build_netplan(networks, iface):
    """Build the YAML for the managed netplan file. Pure and unit-testable.

    networks: list of {nickname, ssid, password}. Open networks (no password)
    become an empty access-point mapping."""
    aps = {}
    for net in networks:
        ssid = (net.get("ssid") or "").strip()
        if not ssid:
            continue
        password = net.get("password") or ""
        aps[ssid] = {"password": password} if password else {}
    # No explicit `renderer`: our file merges with the OS's own netplan files, and
    # declaring a global renderer risks a conflict. systemd-networkd is the active
    # default on this device, so the merged result uses it regardless.
    doc = {
        "network": {
            "version": 2,
            "wifis": {
                iface: {
                    "dhcp4": True,
                    "optional": True,   # don't block boot when WiFi is absent
                    "access-points": aps,
                }
            },
        }
    }
    return yaml.safe_dump(doc, default_flow_style=False, sort_keys=False,
                          allow_unicode=True)


def _write_managed(content):
    os.makedirs(NETPLAN_DIR, exist_ok=True)
    tmp = MANAGED_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write(content)
    os.chmod(tmp, 0o600)   # netplan requires private perms for files with PSKs
    os.replace(tmp, MANAGED_FILE)


def _restore(prev):
    """Put the managed file back to its previous content (or remove it)."""
    try:
        if prev is None:
            if os.path.exists(MANAGED_FILE):
                os.remove(MANAGED_FILE)
        else:
            _write_managed(prev)
    except Exception:
        pass


def apply_networks(networks):
    """Write the managed netplan file and apply it. Returns (ok, message).

    Validates with `netplan generate` first and restores the previous managed
    file (re-applying it) on any failure, so a bad config can't drop networking."""
    iface = get_wifi_interface()
    if not iface:
        return False, "No wireless interface found."

    nets = [n for n in networks if (n.get("ssid") or "").strip()]

    prev = None
    if os.path.exists(MANAGED_FILE):
        try:
            with open(MANAGED_FILE) as f:
                prev = f.read()
        except Exception:
            prev = None

    # No networks left: stop managing WiFi (remove our file) and re-apply.
    if not nets:
        try:
            if os.path.exists(MANAGED_FILE):
                os.remove(MANAGED_FILE)
            _run(["netplan", "apply"], timeout=90)
        except Exception:
            pass
        return True, "No WiFi networks configured."

    try:
        _write_managed(build_netplan(nets, iface))
    except Exception as e:
        return False, f"Could not write netplan config: {e}"

    rc, out = _run(["netplan", "generate"], timeout=30)
    if rc != 0:
        _restore(prev)
        return False, f"netplan config invalid: {out[:200]}"

    rc, out = _run(["netplan", "apply"], timeout=90)
    if rc != 0:
        _restore(prev)
        _run(["netplan", "apply"], timeout=90)   # bring the previous config back up
        return False, f"netplan apply failed: {out[:200]}"

    return True, f"Applied {len(nets)} network(s) on {iface}."


def current_ssid(iface=None):
    """Currently-associated SSID, or None. Tries iwgetid, then iw, then wpa_cli —
    no dependency on NetworkManager. wpa_cli is always present (wpa_supplicant)."""
    iface = iface or get_wifi_interface()

    rc, out = _run(["iwgetid", "-r"], timeout=5)
    if rc == 0 and out.strip():
        return out.strip()

    if iface:
        rc, out = _run(["iw", "dev", iface, "link"], timeout=5)
        if rc == 0:
            for line in out.splitlines():
                line = line.strip()
                if line.startswith("SSID:"):
                    ssid = line[len("SSID:"):].strip()
                    if ssid:
                        return ssid

        rc, out = _run(["wpa_cli", "-i", iface, "status"], timeout=5)
        if rc == 0:
            for line in out.splitlines():
                if line.startswith("ssid="):
                    ssid = line[len("ssid="):].strip()
                    if ssid:
                        return ssid
    return None


def scan_and_connect(networks, wait=16):
    """Re-apply the config (forcing wpa_supplicant to scan and associate to any
    configured network in range) and wait for an association. Returns (ok, message).
    The connected-SSID message is filled by the caller so it can add the nickname."""
    ok, msg = apply_networks(networks)
    if not ok:
        return False, msg
    iface = get_wifi_interface()
    waited = 0
    while waited < wait:
        ssid = current_ssid(iface)
        if ssid:
            return True, ssid
        time.sleep(2)
        waited += 2
    return False, "No configured network came into range."
