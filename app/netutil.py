#!/usr/bin/env python3
"""
netutil.py - Shared network utilities for iOS Backup Machine.

Provides IP detection for WiFi and USB iPhone hotspot interfaces.
"""
import subprocess, re, socket
import logging

log = logging.getLogger(__name__)

# Common interface name patterns
WIFI_IFACES = ["wlan0", "wlan1"]
USB_IPHONE_IFACES = ["usb0", "eth1", "enx"]  # iPhone USB tethering often appears as usb0 or ethX

def get_all_interfaces():
    """Return dict of {iface_name: [list_of_ipv4]}."""
    result = {}
    try:
        out = subprocess.run(
            ["ip", "-4", "-o", "addr", "show"],
            capture_output=True, text=True, timeout=5
        ).stdout
        for line in out.strip().splitlines():
            parts = line.split()
            if len(parts) >= 4:
                iface = parts[1]
                m = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", line)
                if m:
                    result.setdefault(iface, []).append(m.group(1))
    except Exception as e:
        # Foundational probe — most getters below read through this, so one
        # failure blinds them all; worth a warning rather than silence.
        log.warning("could not list interfaces (`ip addr show` failed): %s", e)
    return result

def get_wifi_ip():
    """Return the first WiFi IP found, or None."""
    ifaces = get_all_interfaces()
    for wif in WIFI_IFACES:
        if wif in ifaces and ifaces[wif]:
            return ifaces[wif][0]
    return None

def get_usb_iphone_ip():
    """Return the first USB iPhone hotspot IP found, or None."""
    ifaces = get_all_interfaces()
    for uif in USB_IPHONE_IFACES:
        for name, ips in ifaces.items():
            if name == uif or name.startswith(uif):
                if ips:
                    return ips[0]
    return None

def _wireless_iface():
    """First wireless interface name (e.g. wlan0), or None. Reads sysfs — no tool."""
    import glob
    try:
        for path in sorted(glob.glob("/sys/class/net/*/wireless")):
            return path.split("/")[-2]
    except Exception as e:
        log.debug("could not scan sysfs for a wireless interface: %s", e)
    return None

def get_wifi_ssid():
    """Return the SSID of the currently associated WiFi network, or None.

    Works without NetworkManager (this device uses netplan + wpa_supplicant):
    tries iwgetid, then `iw dev <iface> link`, then `wpa_cli status` — whichever
    is available wins. Returns None when not associated with any WiFi."""
    try:
        ssid = subprocess.run(
            ["iwgetid", "-r"], capture_output=True, text=True, timeout=5
        ).stdout.strip()
        if ssid:
            return ssid
    except Exception as e:
        log.debug("iwgetid failed: %s", e)

    iface = _wireless_iface()
    if not iface:
        return None

    try:
        out = subprocess.run(
            ["iw", "dev", iface, "link"], capture_output=True, text=True, timeout=5
        ).stdout
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("SSID:"):
                ssid = line[len("SSID:"):].strip()
                if ssid:
                    return ssid
    except Exception as e:
        log.debug("`iw dev %s link` failed: %s", iface, e)

    try:
        out = subprocess.run(
            ["wpa_cli", "-i", iface, "status"], capture_output=True, text=True, timeout=5
        ).stdout
        for line in out.splitlines():
            if line.startswith("ssid="):
                ssid = line[len("ssid="):].strip()
                if ssid:
                    return ssid
    except Exception as e:
        log.debug("`wpa_cli -i %s status` failed: %s", iface, e)
    return None

def get_wireguard_ip(iface="wg0"):
    """Return the WireGuard interface IP, or None."""
    ifaces = get_all_interfaces()
    ips = ifaces.get(iface, [])
    return ips[0] if ips else None

def get_active_ip():
    """Return (ip, interface_type) for the first active network connection."""
    wifi = get_wifi_ip()
    if wifi:
        return wifi, "wifi"
    usb = get_usb_iphone_ip()
    if usb:
        return usb, "usb_iphone"
    return None, None

def get_interface_ip(iface_name):
    """Return the IP of a specific interface, or None."""
    ifaces = get_all_interfaces()
    ips = ifaces.get(iface_name, [])
    return ips[0] if ips else None

def resolve_bind_addresses(bind_interfaces):
    """Every address the ``webui.bind_interfaces`` selection resolves to, right now.

    The setting is a multi-select in the settings UI, so it means "listen on all
    of these", not "listen on whichever of these answers first". Returns a
    de-duplicated list in config order; the caller binds one listener per entry.

    ``all`` (or an empty selection) is the wildcard and absorbs the rest: a
    wildcard and a specific address on the same port collide, so the two can
    never be served together.

    A selected interface that currently has no IP is skipped. It is deliberately
    NOT a reason to fall back to the wildcard: the operator restricted where the
    UI answers, and widening that silently is the one outcome they did not ask
    for. The address set is re-resolved periodically by the web UI's bind
    supervisor, so an interface that comes up later is picked up without a
    restart.
    """
    if not bind_interfaces or "all" in bind_interfaces:
        return ["0.0.0.0"]

    addresses = []
    for bi in bind_interfaces:
        if bi == "wifi":
            ip = get_wifi_ip()
        elif bi == "usb_iphone":
            ip = get_usb_iphone_ip()
        elif bi == "wireguard":
            ip = get_wireguard_ip()
        else:
            log.warning("unknown bind_interfaces entry %r, ignored", bi)
            continue
        if ip and ip not in addresses:
            addresses.append(ip)
    return addresses


def get_bind_address(bind_interfaces):
    """First address the selection resolves to, or the wildcard if none does.

    Kept for callers that can only hold one address. Anything that can serve
    several should use :func:`resolve_bind_addresses` instead - collapsing the
    selection to one address is what made a selected-but-down interface strand
    the web UI on whichever other interface happened to be up.
    """
    addresses = resolve_bind_addresses(bind_interfaces)
    return addresses[0] if addresses else "0.0.0.0"

def have_connectivity(timeout=4):
    """Check if we can reach the internet via any interface."""
    for host in ["8.8.8.8", "1.1.1.1"]:
        try:
            s = socket.create_connection((host, 53), timeout=timeout)
            s.close()
            return True
        except OSError as e:
            log.debug("connectivity probe to %s failed: %s", host, e)
            continue
    return False
