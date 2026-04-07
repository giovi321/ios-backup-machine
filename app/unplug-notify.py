#!/usr/bin/env python3
import os, sys, time, yaml
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont
from waveshare_epd import epd2in13_V4, epdconfig

try:
    from notifications import send_notification
except ImportError:
    def send_notification(*a, **kw): pass

CFG_PATH = os.getenv("IOSBACKUP_CONFIG", "/root/iosbackupmachine/config.yaml")

def load_cfg(p):
    with open(p, "r") as f:
        cfg = yaml.safe_load(f) or {}
    cfg.setdefault("owner_lines", ["Property owner","contact","message"])
    cfg.setdefault("orientation", "landscape_right")
    cfg.setdefault("env", {})
    return cfg

CFG = load_cfg(CFG_PATH)
for k,v in CFG.get("env", {}).items():
    os.environ[k] = str(v)

CUSTOM_FONT = CFG.get("font_path") or "/root/iosbackupmachine/UbuntuMono-Regular.ttf"

def font(sz):
    try:
        return ImageFont.truetype(CUSTOM_FONT, sz)
    except Exception:
        return ImageFont.load_default()

F_L = font(14)
F_S = font(14)

def text_wh(d, t, f):
    try:
        l,t0,r,b = d.textbbox((0,0), t, font=f)
        return r-l, b-t0
    except AttributeError:
        return d.textsize(t, font=f)

# Init EPD
try: epdconfig.module_exit()
except Exception: pass
epd = epd2in13_V4.EPD()
epd.init()
epd.Clear(0xFF)

# Physical in portrait
PW, PH = epd.width, epd.height
orient = str(CFG.get("orientation","landscape_right")).lower()
# Logical canvas
if orient in ("landscape_right","landscape_left"):
    LW, LH = PH, PW
else:
    LW, LH = PW, PH

img = Image.new("1", (LW, LH), 255)
drw = ImageDraw.Draw(img)

# Header lines (split date to avoid overflow)
now = datetime.now()
line1 = "Backup interrupted"
line2 = now.strftime("%H:%M / %d %b %Y").lower()

# Owner lines
owner = [ln for ln in CFG.get("owner_lines", []) if str(ln).strip()]

# Build line list: header, date, spacer, owner lines
lines = [
    (line1, F_L),
    (line2, F_S),
    ("", F_S),  # spacer
] + [(ln, F_S) for ln in owner]

spacing = 5
line_heights = [text_wh(drw, ln, f)[1] if ln else 4 for ln, f in lines]
total_h = sum(line_heights) + spacing * (len(lines) - 1)
y = (LH - total_h) // 2

for ln, f in lines:
    if ln:
        tw, th = text_wh(drw, ln, f)
        drw.text(((LW - tw) // 2, y), ln, font=f, fill=0)
        y += th + spacing
    else:
        y += 4 + spacing

# Rotate to physical
if orient == "landscape_right":
    out = img.rotate(90, expand=True)
elif orient == "landscape_left":
    out = img.rotate(270, expand=True)
else:
    out = img

if out.size != (PW, PH):
    out = out.resize((PW, PH))

epd.display(epd.getbuffer(out))
send_notification("device_disconnected", {"timestamp": ts})
time.sleep(2)
epd.sleep()
epdconfig.module_exit()
sys.exit(0)

