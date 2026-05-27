#!/usr/bin/env python3
# backup-sync.py — Double-tap / long-press: sync backups to remote server
import os, sys, time, json, subprocess, yaml
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont
from waveshare_epd import epd2in13_V4, epdconfig

import sync_manager

CONFIG_PATH = os.getenv("IOSBACKUP_CONFIG", "/root/iosbackupmachine/config.yaml")
LOG_DIR = "/var/log/iosbackupmachine"
STATUS_FILE = os.path.join(LOG_DIR, "backup_status.json")

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

def write_status(state, **extra):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        data = {"state": state, "timestamp": datetime.now().isoformat(), **extra}
        with open(STATUS_FILE, "w") as f:
            json.dump(data, f)
    except Exception:
        pass

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

BAR_H = 8
BAR_MARGIN = 16

def show_message(lines_with_fonts, percent=None):
    img = Image.new("1", (LW, LH), 255)
    drw = ImageDraw.Draw(img)
    spacing = 6
    heights = [text_wh(drw, ln, f)[1] for ln, f in lines_with_fonts]
    total_h = sum(heights) + spacing * (len(lines_with_fonts) - 1)
    if percent is not None:
        total_h += BAR_H + spacing
    y = (LH - total_h) // 2
    for ln, f in lines_with_fonts:
        tw, th = text_wh(drw, ln, f)
        drw.text(((LW - tw) // 2, y), ln, font=f, fill=0)
        y += th + spacing
    if percent is not None:
        bar_x = BAR_MARGIN
        bar_w = LW - 2 * BAR_MARGIN
        drw.rectangle([bar_x, y, bar_x + bar_w, y + BAR_H], outline=0)
        fill_w = int(bar_w * min(percent, 100) / 100)
        if fill_w > 0:
            drw.rectangle([bar_x, y, bar_x + fill_w, y + BAR_H], fill=0)
    out = img.rotate(90, expand=True) if ORIENT == "landscape_right" else (
          img.rotate(270, expand=True) if ORIENT == "landscape_left" else img)
    if out.size != (PW, PH):
        out = out.resize((PW, PH))
    epd.display(epd.getbuffer(out))

# Show syncing message
show_message([("Syncing to server...", F), ("Starting...", F_SM)], percent=0)
write_status("syncing", percent=0)

try:
    from notifications import send_notification
except ImportError:
    def send_notification(*a, **kw): pass

send_notification("sync_start")

last_display_update = [0]

def on_progress(pct, elapsed):
    write_status("syncing", percent=pct)
    now = time.time()
    if now - last_display_update[0] >= 2 or pct >= 100:
        last_display_update[0] = now
        show_message([("Syncing to server...", F), (f"{pct}%", F_SM)], percent=pct)

result = sync_manager.run_sync_with_progress(on_progress=on_progress)

if result["success"]:
    write_status("sync_complete", message=result["message"])
    show_message([("Sync complete", F), (result["message"], F_SM)], percent=100)
    send_notification("sync_complete", {"message": result["message"]})
else:
    msg = result["message"]
    if len(msg) > 40:
        msg = msg[:37] + "..."
    write_status("sync_error", message=result["message"])
    show_message([("Sync failed", F), (msg, F_SM)])
    send_notification("sync_error", {"error": result["message"]})

# Release display immediately so shutdown can use it
try:
    epd.sleep()
except Exception:
    pass
epdconfig.module_exit()

time.sleep(5)

# Chain to boot screen
script_dir = os.path.dirname(os.path.abspath(__file__))
next_path = os.path.join(script_dir, "boot-message.py")
if os.path.isfile(next_path):
    rc = subprocess.run([sys.executable, next_path]).returncode
    sys.exit(rc)
else:
    sys.exit(0)
