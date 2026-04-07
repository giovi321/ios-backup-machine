#!/usr/bin/env python3
"""
ntp-sync.py - Sync system clock from NTP when internet is available,
then update the PiSugar RTC.

Checks connectivity via WiFi or USB iPhone hotspot.
Reads NTP servers from config.yaml.
"""
import os, sys, subprocess, socket, time, yaml

CONFIG_PATH = os.getenv("IOSBACKUP_CONFIG", "/root/iosbackupmachine/config.yaml")
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
    """Try multiple NTP sync methods until one succeeds.

    Preference order:
    1. timedatectl / systemd-timesyncd (modern systemd, no extra packages)
    2. ntpdate (classic, may not be packaged on newer distros)
    3. sntp   (sometimes bundled with ntp or chrony)
    """
    # --- Method 1: systemd-timesyncd via timedatectl ---
    try:
        # Configure NTP servers
        ntp_line = " ".join(servers)
        subprocess.run(
            ["timedatectl", "set-ntp", "true"],
            capture_output=True, text=True, timeout=5
        )
        # Write servers to timesyncd config
        try:
            timesyncd_conf = "/etc/systemd/timesyncd.conf.d/iosbackup.conf"
            os.makedirs(os.path.dirname(timesyncd_conf), exist_ok=True)
            with open(timesyncd_conf, "w") as f:
                f.write(f"[Time]\nNTP={ntp_line}\n")
            subprocess.run(["systemctl", "restart", "systemd-timesyncd"],
                           capture_output=True, text=True, timeout=10)
        except Exception:
            pass
        # Wait briefly then check if time was synced
        time.sleep(3)
        r = subprocess.run(["timedatectl", "show", "--property=NTPSynchronized", "--value"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip() == "yes":
            log(f"NTP sync OK via systemd-timesyncd (servers: {ntp_line})")
            return True
        # Even if not yet marked synced, timesyncd will keep trying in background
        log("systemd-timesyncd enabled but not yet synchronized; trying other methods...")
    except FileNotFoundError:
        log("timedatectl not found, trying fallback methods...")
    except Exception as e:
        log(f"timedatectl error: {e}")

    # --- Method 2 & 3: ntpdate / sntp per-server ---
    for srv in servers:
        log(f"Trying NTP server: {srv}")
        for cmd, args in [("ntpdate", ["-u", srv]), ("sntp", ["-sS", srv])]:
            try:
                r = subprocess.run(
                    [cmd] + args,
                    capture_output=True, text=True, timeout=15
                )
                if r.returncode == 0:
                    log(f"NTP sync OK via {cmd} {srv}: {r.stdout.strip()}")
                    return True
                else:
                    log(f"{cmd} failed for {srv}: {r.stderr.strip()}")
            except FileNotFoundError:
                continue
            except subprocess.TimeoutExpired:
                log(f"Timeout syncing with {srv} via {cmd}")
    log("All NTP sync methods failed.")
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
