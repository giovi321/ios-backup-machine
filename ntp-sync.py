#!/usr/bin/env python3
"""
ntp-sync.py — Sync system clock from NTP when internet is available,
then update the PiSugar RTC.

Checks connectivity via WiFi or USB iPhone hotspot.
Reads NTP servers from config.yaml.
"""
import os, sys, subprocess, socket, time, yaml

CONFIG_PATH = os.getenv("IOSBACKUP_CONFIG", "/root/config.yaml")
LOG = "/var/log/iosbackupmachine/ntp-sync.log"

def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass

def load_config():
    try:
        with open(CONFIG_PATH, "r") as f:
            cfg = yaml.safe_load(f) or {}
    except Exception as e:
        log(f"Cannot read config: {e}")
        cfg = {}
    ntp = cfg.get("ntp", {})
    ntp.setdefault("enabled", True)
    ntp.setdefault("servers", ["pool.ntp.org", "time.google.com"])
    return ntp

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

def sync_ntp(servers):
    """Try ntpdate with each server until one succeeds."""
    for srv in servers:
        log(f"Trying NTP server: {srv}")
        try:
            r = subprocess.run(
                ["ntpdate", "-u", srv],
                capture_output=True, text=True, timeout=15
            )
            if r.returncode == 0:
                log(f"NTP sync OK via {srv}: {r.stdout.strip()}")
                return True
            else:
                log(f"ntpdate failed for {srv}: {r.stderr.strip()}")
        except FileNotFoundError:
            # ntpdate not installed, try ntpd -gq or date -s with sntp
            try:
                r = subprocess.run(
                    ["sntp", "-sS", srv],
                    capture_output=True, text=True, timeout=15
                )
                if r.returncode == 0:
                    log(f"sntp sync OK via {srv}: {r.stdout.strip()}")
                    return True
            except FileNotFoundError:
                log("Neither ntpdate nor sntp found. Install ntpdate.")
                return False
        except subprocess.TimeoutExpired:
            log(f"Timeout syncing with {srv}")
    return False

def update_rtc():
    """Push system time to PiSugar RTC via its TCP command interface."""
    try:
        s = socket.create_connection(("127.0.0.1", 8423), timeout=5)
        s.sendall(b"rtc_pi2rtc\n")
        time.sleep(0.5)
        resp = s.recv(256).decode(errors="replace").strip()
        s.close()
        log(f"RTC updated: {resp}")
        return True
    except Exception as e:
        log(f"Failed to update RTC: {e}")
        return False

def main():
    cfg = load_config()
    if not cfg.get("enabled", True):
        log("NTP sync disabled in config.")
        sys.exit(0)

    log("Checking internet connectivity...")
    if not have_connectivity():
        log("No internet connectivity. Skipping NTP sync.")
        sys.exit(0)

    log("Internet available. Starting NTP sync...")
    if sync_ntp(cfg["servers"]):
        update_rtc()
        log("Time sync complete.")
    else:
        log("All NTP servers failed.")
        sys.exit(1)

if __name__ == "__main__":
    main()
