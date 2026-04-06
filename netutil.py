#!/usr/bin/env python3
"""
netutil.py - Shared network utilities for iOS Backup Machine.

Provides IP detection for WiFi and USB iPhone hotspot interfaces.
"""
import subprocess, re, socket

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
    except Exception:
        pass
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

def get_bind_address(bind_interfaces):
    """
    Given a list of bind_interfaces from config (e.g. ['wifi', 'usb_iphone', 'all']),
    return the address to bind to.
    'all' -> '0.0.0.0'
    Otherwise try to find the first matching interface IP.
    """
    if not bind_interfaces or "all" in bind_interfaces:
        return "0.0.0.0"

    for bi in bind_interfaces:
        if bi == "wifi":
            ip = get_wifi_ip()
            if ip:
                return ip
        elif bi == "usb_iphone":
            ip = get_usb_iphone_ip()
            if ip:
                return ip

    # Fallback: bind to all
    return "0.0.0.0"

def have_connectivity(timeout=4):
    """Check if we can reach the internet via any interface."""
    for host in ["8.8.8.8", "1.1.1.1"]:
        try:
            s = socket.create_connection((host, 53), timeout=timeout)
            s.close()
            return True
        except OSError:
            continue
    return False
