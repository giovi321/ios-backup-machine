#!/usr/bin/env python3
# backup-sync.py — Double-tap: sync backups to remote server, show result on e-paper
import os, sys, time, subprocess, yaml
from PIL import Image, ImageDraw, ImageFont
from waveshare_epd import epd2in13_V4, epdconfig

import sync_manager

CONFIG_PATH = os.getenv("IOSBACKUP_CONFIG", "/root/iosbackupmachine/config.yaml")

def load_config(path):
    try:
        with open(path, "r") as f:
            cfg = yaml.safe_load(f) or {}
    except Exception:
        cfg = {}
    cfg.setdefault("orientation", "landscape_right")
    cfg.setdefault("env", {})
    cfg.setdefault("sync", {"enabled": False})
    return cfg

CFG = load_config(CONFIG_PATH)
for k, v in CFG.get("env", {}).items():
    os.environ[k] = str(v)

CUSTOM_FONT = CFG.get("font_path") or "/root/iosbackupmachine/UbuntuMono-Regular.ttf"
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

# Guard: skip if backup is running
def backup_running():
    try:
        out = subprocess.run(
            ["pgrep", "-f", "python.*iosbackupmachine\\.py"],
            capture_output=True, text=True)
        return out.returncode == 0
    except Exception:
        return False

if backup_running():
    print("[INFO] backup in progress, skipping sync", file=sys.stderr)
    sys.exit(0)

if not CFG.get("sync", {}).get("enabled", False):
    print("[INFO] sync is disabled in config", file=sys.stderr)
    sys.exit(0)

# Init EPD
try:
    epdconfig.module_exit()
except Exception:
    pass
epd = epd2in13_V4.EPD()
epd.init()
epd.Clear(0xFF)

PW, PH = epd.width, epd.height
LW, LH = (PH, PW) if ORIENT in ("landscape_right", "landscape_left") else (PW, PH)

def show_message(lines_with_fonts):
    """Draw centered lines on display."""
    img = Image.new("1", (LW, LH), 255)
    drw = ImageDraw.Draw(img)
    spacing = 6
    heights = [text_wh(drw, ln, f)[1] for ln, f in lines_with_fonts]
    total_h = sum(heights) + spacing * (len(lines_with_fonts) - 1)
    y = (LH - total_h) // 2
    for ln, f in lines_with_fonts:
        tw, th = text_wh(drw, ln, f)
        drw.text(((LW - tw) // 2, y), ln, font=f, fill=0)
        y += th + spacing
    out = img.rotate(90, expand=True) if ORIENT == "landscape_right" else (
          img.rotate(270, expand=True) if ORIENT == "landscape_left" else img)
    if out.size != (PW, PH):
        out = out.resize((PW, PH))
    epd.display(epd.getbuffer(out))

# Show syncing message
show_message([
    ("Syncing to server...", F),
    ("Please wait.", F_SM),
])

# Run sync
result = sync_manager.run_sync()

# Show result
if result["success"]:
    show_message([
        ("Sync complete", F),
        (result["message"], F_SM),
    ])
else:
    msg = result["message"]
    # Truncate long error messages for the small display
    if len(msg) > 40:
        msg = msg[:37] + "..."
    show_message([
        ("Sync failed", F),
        (msg, F_SM),
    ])

# Hold 5 seconds
time.sleep(5)
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
    sys.exit(0)
