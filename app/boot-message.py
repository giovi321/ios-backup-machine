#!/usr/bin/env python3
# boot-message.py — Boot screen: project icon + "iOS Backup Machine" + owner info
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

CUSTOM_FONT = CFG.get("font_path") or "/root/iosbackupmachine/UbuntuMono-Regular.ttf"

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

def draw_project_icon(drw, cx, cy, size=36):
    """Draw 1-bit project icon: rounded rect outline + black downward arrow."""
    half = size // 2
    x0, y0 = cx - half, cy - half
    x1, y1 = cx + half, cy + half
    r = size // 6

    # Rounded rectangle outline
    drw.rounded_rectangle((x0, y0, x1, y1), radius=r, outline=0, width=2)

    # Black downward arrow centered inside
    arrow_w = size * 2 // 5
    margin = size // 5
    arrow_top = y0 + margin
    arrow_bot = y1 - margin
    arrow_head_y = cy + margin // 2
    # Stem
    drw.line((cx, arrow_top, cx, arrow_bot), fill=0, width=2)
    # Arrowhead
    drw.line((cx - arrow_w // 2, arrow_head_y, cx, arrow_bot), fill=0, width=2)
    drw.line((cx + arrow_w // 2, arrow_head_y, cx, arrow_bot), fill=0, width=2)

def draw_small_power_icon(drw, x, y, size=10):
    """Small power-on indicator."""
    cx, cy = x + size // 2, y + size // 2
    r = size // 2
    drw.arc((cx - r, cy - r, cx + r, cy + r), start=300, end=240, fill=0, width=1)
    drw.line((cx, cy - r, cx, cy - 1), fill=0, width=1)

# Init EPD
try:
    epdconfig.module_exit()
except Exception:
    pass
epd = epd2in13_V4.EPD()
epd.init()
epd.Clear(0xFF)

PW, PH = epd.width, epd.height
orient = str(CFG.get("orientation", "landscape_right")).lower()
if orient in ("landscape_right", "landscape_left"):
    LW, LH = PH, PW
else:
    LW, LH = PW, PH

img = Image.new("1", (LW, LH), 255)
drw = ImageDraw.Draw(img)

# Small power icon in bottom-left
draw_small_power_icon(drw, 4, LH - 14, size=10)

# Measure elements
ICON_SIZE = 36
icon_height = ICON_SIZE

title_text = "iOS Backup Machine"
title_w, title_h = text_wh(drw, title_text, F)

lines = [ln for ln in CFG["owner_lines"] if str(ln).strip()]
line_heights = [text_wh(drw, ln, F_SM)[1] for ln in lines]

gap_icon_title = 8
gap_title_owner = 6
line_spacing = 4

total_h = (icon_height + gap_icon_title + title_h + gap_title_owner
           + sum(line_heights) + (len(lines) - 1) * line_spacing)

y = (LH - total_h) // 2

# Draw project icon centered
draw_project_icon(drw, LW // 2, y + ICON_SIZE // 2, size=ICON_SIZE)
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
