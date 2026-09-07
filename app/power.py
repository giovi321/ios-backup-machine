#!/usr/bin/env python3
"""
power.py - PiSugar UPS battery reader.

Talks to the PiSugar server over its local TCP control socket (the same
``127.0.0.1:8423`` channel already used for the button listener and RTC sync).

Used by power-aware sync: refuse to start (and auto-abort) a sync when the
battery is low and not charging, so a long rsync isn't cut mid-transfer by
PiSugar's own 30% auto-shutdown.

Fail-open: if the battery can't be read (PiSugar not installed, server down,
parse error), ``get_battery_percent()`` returns ``None`` and callers proceed.

Import-safe: stdlib only, so it can be unit-tested on any machine.
"""
import socket

PISUGAR_HOST = "127.0.0.1"
PISUGAR_PORT = 8423


def _query(command, timeout=5):
    """Send a single PiSugar command and return the raw text reply, or None."""
    try:
        s = socket.create_connection((PISUGAR_HOST, PISUGAR_PORT), timeout=timeout)
        try:
            s.sendall(command.encode("ascii") + b"\n")
            s.settimeout(timeout)
            chunks = b""
            # Reply is small and newline-terminated; read until we have a line.
            while b"\n" not in chunks:
                data = s.recv(256)
                if not data:
                    break
                chunks += data
            return chunks.decode("utf-8", errors="replace").strip()
        finally:
            s.close()
    except Exception:
        return None


def _parse_value(reply, key):
    """Extract the value after ``key:`` from a PiSugar reply line, or None."""
    if not reply:
        return None
    prefix = key.lower() + ":"
    for line in reply.splitlines():
        line = line.strip()
        if line.lower().startswith(prefix):
            return line.split(":", 1)[1].strip()
    return None


def get_battery_percent(timeout=5):
    """Return battery charge as a float 0..100, or None if unreadable."""
    val = _parse_value(_query("get battery", timeout=timeout), "battery")
    if val is None:
        return None
    try:
        return float(val)
    except ValueError:
        return None


def is_charging(timeout=5):
    """Return True/False if the charging state is known, else None."""
    val = _parse_value(_query("get battery_charging", timeout=timeout), "battery_charging")
    if val is None:
        return None
    return val.strip().lower() in ("true", "1", "yes")


def get_battery(timeout=5):
    """Return ``{'percent': float|None, 'charging': bool|None}``.

    ``timeout`` is exposed because this is TWO socket round trips: against a
    PiSugar server that accepts the connection and never answers, the default
    costs up to 20s. The backup's in-run health probe runs on the thread
    pumping idevicebackup2's output, so it asks for a shorter one.
    """
    return {"percent": get_battery_percent(timeout=timeout),
            "charging": is_charging(timeout=timeout)}


def sync_allowed(threshold, battery=None):
    """Decide whether a sync may run given a battery ``threshold`` percent.

    Returns ``(allowed: bool, reason: str)``. Fail-open: an unreadable battery
    or an active charge always allows the sync.
    """
    if battery is None:
        battery = get_battery()
    pct = battery.get("percent")
    charging = battery.get("charging")
    if pct is None:
        return True, ""              # can't read — don't block
    if charging:
        return True, ""              # plugged in — fine to run
    if pct < threshold:
        return False, f"Battery low ({pct:.0f}% < {threshold:g}%)."
    return True, ""
