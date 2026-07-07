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


# A dedicated fwmark bit for the "answer LAN connections over the LAN" exception.
# Must not collide with wg's own fwmark (0xca6c), so a high bit is used.
LAN_FWMARK = 0x40000
_LAN_RULE_PRIO = "5000"   # evaluated before wg's catch-all rule (~32765)


def _run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, timeout=5).returncode
    except Exception:
        return 1


def _remove_lan_bypass():
    """Delete wg-quick's `ip rule ... suppress_prefixlength 0` (v4 + v6), which is
    what lets same-subnet/local destinations skip the tunnel. Returns count removed."""
    removed = 0
    for base in (["ip", "rule", "del"], ["ip", "-6", "rule", "del"]):
        for _ in range(8):   # there can be several instances; remove them all
            if _run(base + ["table", "main", "suppress_prefixlength", "0"]) != 0:
                break
            removed += 1
    return removed


def _lan_exception_specs(iface):
    """iptables rule specs (table, chain, *args) that keep inbound LAN connections
    answerable over the LAN instead of the tunnel."""
    m = hex(LAN_FWMARK)
    return [
        # Mark NEW connections arriving on any non-VPN interface (WiFi, iPhone USB…).
        ("mangle", "PREROUTING", "!", "-i", iface, "-m", "conntrack", "--ctstate", "NEW",
         "-j", "CONNMARK", "--set-xmark", f"{m}/{m}"),
        # Stamp the connection's mark onto reply packets so the ip rule below sees it.
        # --mask touches only our bit, leaving wg's fwmark on its own packets intact.
        ("mangle", "OUTPUT", "-j", "CONNMARK", "--restore-mark", "--mask", m),
    ]


def _apply_lan_access_exception(iface):
    """Let SSH / the web UI stay reachable from the local network while full_tunnel
    routes everything else through the VPN: replies to connections that arrived on a
    non-VPN interface go back out via the main table (the LAN), not the tunnel.
    Idempotent (append only if absent; re-add the ip rule cleanly)."""
    m = hex(LAN_FWMARK)
    for table, chain, *spec in _lan_exception_specs(iface):
        if _run(["iptables", "-t", table, "-C", chain, *spec]) != 0:
            _run(["iptables", "-t", table, "-A", chain, *spec])
    for _ in range(4):
        if _run(["ip", "rule", "del", "fwmark", f"{m}/{m}", "table", "main",
                 "priority", _LAN_RULE_PRIO]) != 0:
            break
    _run(["ip", "rule", "add", "fwmark", f"{m}/{m}", "table", "main",
          "priority", _LAN_RULE_PRIO])


def _clear_lan_access_exception(iface):
    m = hex(LAN_FWMARK)
    for _ in range(4):
        if _run(["ip", "rule", "del", "fwmark", f"{m}/{m}", "table", "main",
                 "priority", _LAN_RULE_PRIO]) != 0:
            break
    for table, chain, *spec in _lan_exception_specs(iface):
        for _ in range(4):
            if _run(["iptables", "-t", table, "-D", chain, *spec]) != 0:
                break


def enforce_full_tunnel(iface="wg0"):
    """Route all traffic through the VPN while keeping the device reachable locally.

    1. Remove wg-quick's LAN-access bypass so even same-subnet destinations (e.g. a
       sync server whose IP overlaps the WiFi subnet) use the tunnel. wg's own
       fwmark'd handshake still uses the main table, so the link stays up.
    2. Install a connection-mark exception so inbound LAN connections (SSH, web UI)
       are answered over the LAN — local access keeps working.

    Best-effort and idempotent; re-applied on every connect (from start_wireguard),
    so it survives reconnects after errors/disconnects. Returns bypass rules removed."""
    removed = _remove_lan_bypass()
    _apply_lan_access_exception(iface)
    print(f"[WG] Full tunnel enforced (removed {removed} bypass rule(s); LAN access kept)",
          flush=True)
    return removed


def clear_full_tunnel(iface="wg0"):
    """Undo the LAN-access exception (when full_tunnel is off or on disconnect). The
    wg-quick LAN bypass is restored by wg-quick itself on the next connect."""
    _clear_lan_access_exception(iface)


def is_interface_up(iface="wg0"):
    try:
        return subprocess.run(["ip", "link", "show", iface], capture_output=True, text=True, timeout=5).returncode == 0
    except Exception:
        return False

def latest_handshake(iface="wg0"):
    """Newest peer handshake as a unix epoch (int), or 0 if none has completed.

    A wg interface can exist ('up') without ever handshaking — e.g. brought up
    while the endpoint is unreachable, or before the clock is NTP-synced (a wrong
    clock makes the server reject the handshake). 0 means 'up but not actually
    connected'; any positive value means at least one handshake succeeded."""
    try:
        r = subprocess.run(["wg", "show", iface, "latest-handshakes"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return 0
        best = 0
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2:
                try:
                    best = max(best, int(parts[-1]))
                except ValueError:
                    pass
        return best
    except Exception:
        return 0

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
            # route everything through the tunnel when full_tunnel is enabled,
            # while keeping local SSH / web UI access. Otherwise clear any leftover.
            try:
                if _full_tunnel_enabled():
                    enforce_full_tunnel(iface)
                else:
                    clear_full_tunnel(iface)
            except Exception as e:
                print(f"[WG] Full-tunnel enforcement error: {e}", flush=True)
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
        clear_full_tunnel(iface)   # remove the LAN-access exception rules
    except Exception:
        pass
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
