#!/usr/bin/env python3
# boot-message.py — Boot screen: power icon + "iOS Backup Machine" + owner info
import os, sys, time, yaml
from PIL import Image, ImageDraw, ImageFont
from waveshare_epd import epd2in13_V4, epdconfig

CONFIG_PATH = os.getenv("IOSBACKUP_CONFIG", "/root/iosbackupmachine/config.yaml")

def load_config(path):
    with open(path, "r") as f:
        cfg = yaml.safe_load(f) or {}
    cfg.setdefault("owner_lines", ["Name", "email", "phone", "message"])
    cfg.setdefault("orientation", "landscape_right")
    cfg.setdefault("env", {})
    return cfg

CFG = load_config(CONFIG_PATH)
for k, v in CFG.get("env", {}).items():
    os.environ[k] = str(v)

CUSTOM_FONT = CFG.get("font_path")

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

def draw_power_icon(drw, cx, cy, r=18, stem=10, width=2):
    """Draw IEC 5009 power symbol: circle arc with gap at top + vertical stem."""
    # Arc with gap at top (PIL: 0=3 o'clock, 90=6 o'clock, 270=12 o'clock)
    # Gap from ~240 to ~300 degrees (upper portion)
    bbox = (cx - r, cy - r, cx + r, cy + r)
    drw.arc(bbox, start=-60, end=240, fill=0, width=width)
    # Vertical stem through the gap
    drw.line((cx, cy - r - 2, cx, cy - r + stem), fill=0, width=width)

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

orient = str(CFG.get("orientation", "landscape_right")).lower()
if orient in ("landscape_right", "landscape_left"):
    LW, LH = PH, PW  # 122 x 250
else:
    LW, LH = PW, PH

img = Image.new("1", (LW, LH), 255)
drw = ImageDraw.Draw(img)

# Measure all elements
ICON_R = 16
ICON_STEM = 9
icon_height = 2 * ICON_R + 4  # circle diameter + stem overshoot

title_text = "iOS Backup Machine"
title_w, title_h = text_wh(drw, title_text, F)

lines = [ln for ln in CFG["owner_lines"] if str(ln).strip()]
line_heights = [text_wh(drw, ln, F_SM)[1] for ln in lines]

gap_icon_title = 10
gap_title_owner = 8
line_spacing = 5

total_h = (icon_height + gap_icon_title + title_h + gap_title_owner
           + sum(line_heights) + (len(lines) - 1) * line_spacing)

y = (LH - total_h) // 2

# Draw power icon
icon_cy = y + 2 + ICON_R  # center of the circle
draw_power_icon(drw, LW // 2, icon_cy, r=ICON_R, stem=ICON_STEM, width=2)
y += icon_height + gap_icon_title

# Draw title
drw.text(((LW - title_w) // 2, y), title_text, font=F, fill=0)
y += title_h + gap_title_owner

# Draw owner lines
for ln in lines:
    tw, th = text_wh(drw, ln, F_SM)
    drw.text(((LW - tw) // 2, y), ln, font=F_SM, fill=0)
    y += th + line_spacing

# Rotate logical to physical
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
