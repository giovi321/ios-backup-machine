#!/usr/bin/env python3
# last completion of backup (most recent subfolder, time only)
import os, sys, time, yaml
from PIL import Image, ImageDraw, ImageFont
from waveshare_epd import epd2in13_V4, epdconfig

CONFIG_PATH = os.getenv("IOSBACKUP_CONFIG", "/root/config.yaml")
BACKUP_DIR = "/media/iosbackup"

def load_config(path):
    with open(path, "r") as f:
        cfg = yaml.safe_load(f) or {}
    cfg.setdefault("orientation", "landscape_right")
    cfg.setdefault("env", {})
    return cfg

CFG = load_config(CONFIG_PATH)
for k, v in CFG.get("env", {}).items():
    os.environ[k] = str(v)

def font(sz):
    try:
        return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", sz)
    except Exception:
        return ImageFont.load_default()

F_TITLE = font(16)
F_TIME  = font(14)

def text_wh(d, t, f):
    try:
        l, t0, r, b = d.textbbox((0, 0), t, font=f)
        return r - l, b - t0
    except AttributeError:
        return d.textsize(t, font=f)

def latest_dir_mtime(root):
    try:
        with os.scandir(root) as it:
            dirs = [e for e in it if e.is_dir(follow_symlinks=False)]
        if not dirs:
            return None
        latest = max(dirs, key=lambda e: e.stat(follow_symlinks=False).st_mtime)
        return latest.stat(follow_symlinks=False).st_mtime
    except Exception:
        return None

# Init EPD
try:
    epdconfig.module_exit()
except Exception:
    pass
epd = epd2in13_V4.EPD()
epd.init()
epd.Clear(0xFF)

PW, PH = epd.width, epd.height  # 250 x 122
orient = str(CFG.get("orientation", "landscape_right")).lower()
LW, LH = (PH, PW) if orient in ("landscape_right", "landscape_left") else (PW, PH)

img = Image.new("1", (LW, LH), 255)
drw = ImageDraw.Draw(img)

ts = latest_dir_mtime(BACKUP_DIR)
if ts:
    text = time.strftime("%H:%M / %d %b %Y", time.localtime(ts))
else:
    text = "No backups found"

lines = [("Last backup:", F_TITLE), (text, F_TIME)]

line_heights = [text_wh(drw, ln, f)[1] for ln, f in lines]
spacing = 8
total_h = sum(line_heights) + spacing
y = (LH - total_h) // 2

for ln, f in lines:
    tw, th = text_wh(drw, ln, f)
    drw.text(((LW - tw)//2, y), ln, font=f, fill=0)
    y += th + spacing

# Rotate and display
if orient == "landscape_right":
    out = img.rotate(90, expand=True)
elif orient == "landscape_left":
    out = img.rotate(270, expand=True)
else:
    out = img

if out.size != (PW, PH):
    out = out.resize((PW, PH))

epd.display(epd.getbuffer(out))
time.sleep(2)
epd.sleep()
epdconfig.module_exit()
sys.exit(0)
