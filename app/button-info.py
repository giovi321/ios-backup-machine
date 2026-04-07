#!/usr/bin/env python3
# button-info.py — Button press screen: date, time, IP, last backup, disk free %, SoC temp
# Displays for 30 seconds, then chains to boot-message.py
import os, sys, time, subprocess, yaml
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont
from waveshare_epd import epd2in13_V4, epdconfig

CONFIG_PATH = os.getenv("IOSBACKUP_CONFIG", "/root/iosbackupmachine/config.yaml")

def load_config(path):
    try:
        with open(path, "r") as f:
            cfg = yaml.safe_load(f) or {}
    except Exception as e:
        print(f"[ERR] cannot read config {path}: {e}", file=sys.stderr)
        cfg = {}
    cfg.setdefault("orientation", "landscape_right")
    cfg.setdefault("env", {})
    cfg.setdefault("backup_dir", "/media/iosbackup")
    cfg.setdefault("disk_device", None)
    return cfg

CFG = load_config(CONFIG_PATH)
for k, v in CFG.get("env", {}).items():
    os.environ[k] = str(v)

CUSTOM_FONT = CFG.get("font_path") or "/root/iosbackupmachine/UbuntuMono-Regular.ttf"
BACKUP_DIR = CFG.get("backup_dir")
DISK_DEVICE = CFG.get("disk_device")
ORIENT = str(CFG.get("orientation", "landscape_right")).lower()

def font(sz):
    try:
        return ImageFont.truetype(CUSTOM_FONT, sz)
    except Exception:
        return ImageFont.load_default()

F = font(14)
F_SM = font(12)

def text_wh(d, t, f):
    try:
        l, t0, r, b = d.textbbox((0, 0), t, font=f)
        return r - l, b - t0
    except AttributeError:
        return d.textsize(t, font=f)

# --- Data retrieval ---

def get_ip():
    try:
        out = subprocess.check_output(["hostname", "-I"], text=True, timeout=5).strip()
        return out.split()[0] if out else "No IP"
    except Exception:
        return "No IP"

def get_soc_temp():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
            return round(int(f.read().strip()) / 1000, 1)
    except Exception:
        return None

def normpath(p):
    if not p:
        return p
    return os.path.abspath(os.path.expanduser(os.path.expandvars(p)))

def device_to_mountpoint(dev):
    try:
        with open("/proc/mounts", "r") as f:
            rows = [ln.split() for ln in f.read().splitlines()]
        mp = None
        for src, mnt, *_ in rows:
            if src == dev:
                if not mp or len(mnt) > len(mp):
                    mp = mnt
        return mp
    except Exception:
        return None

def get_free_disk_pct():
    mp = None
    if DISK_DEVICE and DISK_DEVICE.startswith("/dev/"):
        mp = device_to_mountpoint(DISK_DEVICE)
    if not mp:
        mp = normpath(BACKUP_DIR) or "/"
    try:
        st = os.statvfs(mp)
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        if total <= 0:
            return None
        return round(free / total * 100, 1)
    except Exception:
        return None

def latest_dir_mtime(root):
    try:
        with os.scandir(root) as it:
            cand = []
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=True):
                        st = e.stat(follow_symlinks=True)
                        cand.append(st.st_mtime)
                except Exception:
                    continue
        if not cand:
            return None
        return max(cand)
    except Exception:
        return None

def get_last_backup():
    bd = normpath(BACKUP_DIR)
    if not bd or not os.path.isdir(bd):
        return "No backups"
    ts = latest_dir_mtime(bd)
    if ts is None:
        return "No backups"
    return time.strftime("%H:%M / %d %b %Y", time.localtime(ts))

# --- Guard: skip if backup is running ---
def backup_running():
    """Check if iosbackupmachine.py is running by looking for its process."""
    try:
        # Use pgrep with exact script name match, exclude grep itself
        out = subprocess.run(
            ["pgrep", "-f", "python.*iosbackupmachine\\.py"],
            capture_output=True, text=True)
        return out.returncode == 0
    except Exception:
        return False

if backup_running():
    print("[INFO] backup in progress, skipping button-info display", file=sys.stderr)
    sys.exit(0)

# --- Init EPD ---
try:
    epdconfig.module_exit()
except Exception:
    pass
epd = epd2in13_V4.EPD()
epd.init()
epd.Clear(0xFF)

PW, PH = epd.width, epd.height
LW, LH = (PH, PW) if ORIENT in ("landscape_right", "landscape_left") else (PW, PH)

img = Image.new("1", (LW, LH), 255)
drw = ImageDraw.Draw(img)

# --- Gather data ---
now = datetime.now()
date_str = now.strftime("%d %b %Y")
time_str = now.strftime("%H:%M")
ip_str = get_ip()
temp = get_soc_temp()
temp_str = f"Temp: {temp}C" if temp is not None else "Temp: n/a"
free_pct = get_free_disk_pct()
disk_str = f"SD free: {free_pct}%" if free_pct is not None else "SD free: n/a"
backup_str = f"Last: {get_last_backup()}"

# Backup result from status file
def get_backup_result():
    try:
        import json
        sf = "/var/log/iosbackupmachine/backup_status.json"
        with open(sf, "r") as f:
            data = json.load(f)
        state = data.get("state", "")
        if state == "complete":
            return "OK"
        elif state == "interrupted":
            return "INTERRUPTED"
        elif state == "error":
            return "ERROR"
        elif state == "backing_up":
            return f"IN PROGRESS {data.get('percent', '')}%"
        else:
            return ""
    except Exception:
        return ""

result = get_backup_result()
if result:
    backup_str += f" ({result})"

# VPN status
def get_vpn_status():
    try:
        iface = "wg0"
        r = subprocess.run(["ip", "link", "show", iface], capture_output=True, text=True, timeout=3)
        return "VPN: connected" if r.returncode == 0 else "VPN: off"
    except Exception:
        return "VPN: off"

vpn_str = get_vpn_status()

# --- Layout: all lines centered ---
info_lines = [
    (date_str, F),
    (time_str, F),
    ("", F_SM),        # spacer
    (f"IP: {ip_str}", F_SM),
    (vpn_str, F_SM),
    (backup_str, F_SM),
    (disk_str, F_SM),
    (temp_str, F_SM),
]

spacing = 4
line_heights = [text_wh(drw, ln, f)[1] if ln else 4 for ln, f in info_lines]
total_h = sum(line_heights) + spacing * (len(info_lines) - 1)
y = (LH - total_h) // 2

for ln, f in info_lines:
    if ln:
        tw, th = text_wh(drw, ln, f)
        drw.text(((LW - tw) // 2, y), ln, font=f, fill=0)
        y += th + spacing
    else:
        y += 4 + spacing  # spacer

# --- Rotate and display ---
out = img.rotate(90, expand=True) if ORIENT == "landscape_right" else (
      img.rotate(270, expand=True) if ORIENT == "landscape_left" else img)
if out.size != (PW, PH):
    out = out.resize((PW, PH))
epd.display(epd.getbuffer(out))

# Hold 30 seconds
time.sleep(30)
try:
    epd.sleep()
except Exception:
    pass
epdconfig.module_exit()

# Chain to boot screen
script_dir = os.path.dirname(os.path.abspath(__file__))
next_path = os.path.join(script_dir, "boot-message.py")
if os.path.isfile(next_path):
    rc = subprocess.run([sys.executable, next_path]).returncode
    sys.exit(rc)
else:
    print(f"[WARN] boot-message.py not found at {next_path}", file=sys.stderr)
    sys.exit(0)
