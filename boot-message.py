#!/usr/bin/env python3
# iosboot-message.py (config-enabled, orientation-correct, with border box)
import os, sys, time, yaml
from PIL import Image, ImageDraw, ImageFont
from waveshare_epd import epd2in13_V4, epdconfig

CONFIG_PATH = os.getenv("IOSBACKUP_CONFIG", "/root/config.yaml")

def load_config(path):
    with open(path, "r") as f:
        cfg = yaml.safe_load(f) or {}
    cfg.setdefault("owner_lines", ["Property owner", "contact", "message"])
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

F = font(14)

def text_wh(d, t, f):
    try:
        l,t0,r,b = d.textbbox((0,0), t, font=f)
        return r-l, b-t0
    except AttributeError:
        return d.textsize(t, font=f)

# Init EPD
try:
    epdconfig.module_exit()
except Exception:
    pass
epd = epd2in13_V4.EPD()
epd.init()
epd.Clear(0xFF)

# Physical buffer in portrait
PW, PH = epd.width, epd.height  # 250 x 122

orient = str(CFG.get("orientation","landscape_right")).lower()
# Logical drawing size
if orient in ("landscape_right","landscape_left"):
    LW, LH = PH, PW   # landscape logical
else:
    LW, LH = PW, PH   # portrait logical

# Draw centered owner lines on logical canvas
img = Image.new("1", (LW, LH), 255)
drw = ImageDraw.Draw(img)

# Border box near full screen
margin = 0
for t in range(0, 1):  # 1-pixel thick
    drw.rectangle((margin+t, margin+t, LW-margin-t-1, LH-margin-t-1), outline=0, width=1)

lines = [ln for ln in CFG["owner_lines"] if str(ln).strip()]
line_heights = [text_wh(drw, ln, F)[1] for ln in lines]
total_h = sum(line_heights) + (len(lines)-1)*6
y = (LH - total_h)//2
for ln in lines:
    tw, th = text_wh(drw, ln, F)
    drw.text(((LW - tw)//2, y), ln, font=F, fill=0)
    y += th + 6

# Rotate logical to physical
if orient == "landscape_right":
    out = img.rotate(90, expand=True)
elif orient == "landscape_left":
    out = img.rotate(270, expand=True)
else:
    out = img  # portrait

if out.size != (PW, PH):
    out = out.resize((PW, PH))

epd.display(epd.getbuffer(out))
time.sleep(2)
epd.sleep()
epdconfig.module_exit()
sys.exit(0)
