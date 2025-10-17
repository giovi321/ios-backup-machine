#!/usr/bin/env python3
import os, sys, time, yaml
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont
from waveshare_epd import epd2in13_V4, epdconfig

CFG_PATH = os.getenv("IOSBACKUP_CONFIG", "/root/config.yaml")

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

def font(sz):
    try:
        return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", sz)
    except Exception:
        return ImageFont.load_default()

F_L = font(16)
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

# Border box
margin = 0
for t in range(0,1): # 1 pixel thickness
    drw.rectangle((margin+t, margin+t, LW-margin-t-1, LH-margin-t-1), outline=0, width=1)

# Header line
ts = datetime.now().strftime("%H:%M / %d %b %Y").lower()
title = f"Backup interrupted at {ts}"

# Owner lines
owner = [ln for ln in CFG.get("owner_lines", []) if str(ln).strip()]

# Measure block height
lines = [title, ""] + owner
heights = [(text_wh(drw, ln, F_L if i==0 else F_S)[1]) for i,ln in enumerate(lines)]
total_h = sum(heights) + (len(lines)-1)*6
y = (LH - total_h)//2

for i, ln in enumerate(lines):
    font_use = F_L if i==0 else F_S
    tw, th = text_wh(drw, ln, font_use)
    drw.text(((LW - tw)//2, y), ln, font=font_use, fill=0)
    y += th + 6

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
time.sleep(2)
epd.sleep()
epdconfig.module_exit()
sys.exit(0)
PY

