#!/usr/bin/env python3
import os, sys, time, yaml, subprocess
from PIL import Image, ImageDraw, ImageFont
from waveshare_epd import epd2in13_V4, epdconfig

CONFIG_PATH = os.getenv("IOSBACKUP_CONFIG", "/root/config.yaml")
NEXT_SCRIPT = os.getenv("NEXT_SCRIPT", "owner-message.py")

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

CUSTOM_FONT = CFG.get("font_path")

# Normalize paths
def normpath(p):
    if not p: return p
    return os.path.abspath(os.path.expanduser(os.path.expandvars(p)))

BACKUP_DIR = normpath(CFG.get("backup_dir"))
DISK_DEVICE = CFG.get("disk_device")
ORIENT = str(CFG.get("orientation", "landscape_right")).lower()

print(f"[DBG] CONFIG_PATH={CONFIG_PATH}", file=sys.stderr)
print(f"[DBG] backup_dir={BACKUP_DIR}", file=sys.stderr)
print(f"[DBG] disk_device={DISK_DEVICE}", file=sys.stderr)
print(f"[DBG] orientation={ORIENT}", file=sys.stderr)

def font(sz):
    try:
        return ImageFont.truetype(CUSTOM_FONT, sz)
    except Exception:
        return ImageFont.load_default()

F_TITLE = font(14)
F_TIME  = font(14)
F_USAGE = font(14)

def text_wh(d, t, f):
    try:
        l,t0,r,b = d.textbbox((0,0), t, font=f)
        return r-l, b-t0
    except AttributeError:
        return d.textsize(t, font=f)

def latest_dir_mtime(root):
    try:
        with os.scandir(root) as it:
            # accept dirs and symlinks-to-dirs
            cand = []
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=True):
                        st = e.stat(follow_symlinks=True)
                        cand.append((st.st_mtime, e))
                except Exception:
                    continue
        if not cand:
            return None
        cand.sort(key=lambda x: x[0], reverse=True)
        return cand[0][0]
    except Exception as e:
        print(f"[ERR] latest_dir_mtime: {e}", file=sys.stderr)
        return None

def device_to_mountpoint(dev):
    # Map /dev/* to its mountpoint via /proc/mounts
    try:
        with open("/proc/mounts","r") as f:
            rows = [ln.split() for ln in f.read().splitlines()]
        # Prefer the longest mountpoint for the exact device
        mp = None
        for src, mnt, *_ in rows:
            if src == dev:
                if not mp or len(mnt) > len(mp):
                    mp = mnt
        return mp
    except Exception:
        return None

def get_disk_usage_pct(path_or_dev):
    mp = None
    if path_or_dev:
        if path_or_dev.startswith("/dev/"):
            mp = device_to_mountpoint(path_or_dev)
        else:
            mp = normpath(path_or_dev)
    if not mp:
        # fallback to backup_dir
        mp = BACKUP_DIR or "/"
    try:
        st = os.statvfs(mp)
        total = st.f_blocks * st.f_frsize
        free  = st.f_bavail * st.f_frsize
        used  = total - free
        if total <= 0:
            return None, mp
        return round(used / total * 100, 1), mp
    except Exception as e:
        print(f"[ERR] statvfs({mp}): {e}", file=sys.stderr)
        return None, mp

# Init EPD
try: epdconfig.module_exit()
except Exception: pass
epd = epd2in13_V4.EPD()
epd.init()
epd.Clear(0xFF)

PW, PH = epd.width, epd.height  # 250 x 122
LW, LH = (PH, PW) if ORIENT in ("landscape_right","landscape_left") else (PW, PH)

img = Image.new("1", (LW, LH), 255)
drw = ImageDraw.Draw(img)

# Data
ts = latest_dir_mtime(BACKUP_DIR) if BACKUP_DIR and os.path.isdir(BACKUP_DIR) else None
last_backup = time.strftime("%H:%M / %d %b %Y", time.localtime(ts)) if ts else "No backups found"

usage_pct, used_mp = get_disk_usage_pct(DISK_DEVICE or BACKUP_DIR)
usage_str = f"{usage_pct}%" if usage_pct is not None else "n/a"

lines = [
    ("Last backup:", F_TITLE),
    (last_backup, F_TIME),
    (f"Memory usage: {usage_str}", F_USAGE),
]

# Center block
spacing = 6
line_heights = [text_wh(drw, ln, f)[1] for ln, f in lines]
total_h = sum(line_heights) + spacing*(len(lines)-1)
y = (LH - total_h)//2
for ln, f in lines:
    tw, th = text_wh(drw, ln, f)
    drw.text(((LW - tw)//2, y), ln, font=f, fill=0)
    y += th + spacing

# Rotate and display
out = img.rotate(90, expand=True) if ORIENT=="landscape_right" else (
      img.rotate(270, expand=True) if ORIENT=="landscape_left" else img)
if out.size != (PW, PH):
    out = out.resize((PW, PH))
epd.display(epd.getbuffer(out))

# Hold 10s
time.sleep(10)
try: epd.sleep()
except Exception: pass
epdconfig.module_exit()

# Chain next script
script_dir = os.path.dirname(os.path.abspath(__file__))
next_path = os.path.join(script_dir, os.getenv("NEXT_SCRIPT", "owner-message.py"))
if os.path.isfile(next_path):
    rc = subprocess.run([sys.executable, next_path]).returncode
    sys.exit(rc)
else:
    print(f"Owner information not found.", file=sys.stderr)
    sys.exit(0)
