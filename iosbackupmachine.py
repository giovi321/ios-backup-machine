
#!/usr/bin/env python3
# ios_backup_display_epaper.py (config-enabled, orientation-correct drawing)
import os, re, sys, time, subprocess
from datetime import datetime
from periphery.gpio import GPIOError

import yaml
from PIL import Image, ImageDraw, ImageFont
from waveshare_epd import epd2in13_V4, epdconfig

CONFIG_PATH = os.getenv("IOSBACKUP_CONFIG", "/root/config.yaml")
LOG_DIR = "/var/log/iosbackup"
IDLE_REFRESH_SEC = 4
TITLE = "iOS Backup Machine"

def load_config(path):
    with open(path, "r") as f:
        cfg = yaml.safe_load(f) or {}
    cfg.setdefault("backup_dir", "/media/iosbackup/")
    cfg.setdefault("marker_file", ".foldermarker")
    cfg.setdefault("disk_device", "/dev/mmcblk1")
    cfg.setdefault("orientation", "landscape_right")
    cfg.setdefault("owner_lines", ["Property owner", "contact", "message"])
    cfg.setdefault("error_codes", {})
    cfg.setdefault("env", {})
    ec = {}
    for k, v in cfg["error_codes"].items():
        try: ec[int(k)] = str(v)
        except Exception: pass
    cfg["error_codes"] = ec
    return cfg

CFG = load_config(CONFIG_PATH)
for k, v in CFG.get("env", {}).items():
    os.environ[k] = str(v)

def font(sz):
    try: return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", sz)
    except Exception: return ImageFont.load_default()
F_SM, F_MD, F_LG = font(12), font(14), font(20)

class Panel:
    def _try_init(self, partial=False, attempts=2):
        for i in range(attempts):
            try:
                if partial:
                    if hasattr(self.epd, "init_Part"): self.epd.init_Part()
                    elif "mode" in self.epd.init.__code__.co_varnames: self.epd.init(1)
                    else: self.epd.init()
                else:
                    if hasattr(self.epd, "init_Full"): self.epd.init_Full()
                    elif "mode" in self.epd.init.__code__.co_varnames: self.epd.init(0)
                    else: self.epd.init()
                return
            except GPIOError as e:
                if e.errno != 16 or i == attempts - 1:
                    raise
                try: epdconfig.module_exit()
                except Exception: pass
                time.sleep(0.2)

    def _init_full(self):
        self._try_init(partial=False)

    def _init_part(self):
        self._try_init(partial=True)

    def _display_full(self, img):
        buf = self.epd.getbuffer(img)
        self._init_full()
        if hasattr(self.epd, "display"):
            self.epd.display(buf)
        else:
            self.epd.display_Base(buf)
        self._partial_base_set = False

    def _display_set_base_then_partial(self, img):
        buf = self.epd.getbuffer(img)
        if hasattr(self.epd, "display_Base") and hasattr(self.epd, "display_Partial"):
            if not getattr(self, "_partial_base_set", False):
                self._init_full(); self.epd.display_Base(buf); self._init_part(); self._partial_base_set = True
            else:
                self._init_part()
            self.epd.display_Partial(buf)
        else:
            self._display_full(img)

    def __init__(self):
        try: epdconfig.module_exit()
        except Exception: pass
        self.epd = epd2in13_V4.EPD()
        self.pw, self.ph = self.epd.width, self.epd.height
        self.orient = str(CFG.get("orientation","landscape_right")).lower()
        self.anim = 0

        if hasattr(self.epd, "init_Full"):
            self.epd.init_Full()
        else:
            try:
                if "mode" in self.epd.init.__code__.co_varnames:
                    self.epd.init(0)
                else:
                    self.epd.init()
            except AttributeError:
                self.epd.init()
        self.epd.Clear(0xFF)
        self._partial_base_set = False

    def _logical_size(self):
        # Logical canvas size used for drawing
        if self.orient in ("landscape_right", "landscape_left"):
            return (self.ph, self.pw)  # swap for landscape
        return (self.pw, self.ph)     # portrait not used here but supported

    def _rotate_for_display(self, img):
        # Rotate logical canvas to physical portrait buffer size
        if self.orient == "landscape_right":
            return img.rotate(90, expand=True)   # matches boot script convention
        elif self.orient == "landscape_left":
            return img.rotate(270, expand=True)
        else:
            return img  # portrait as-is

    def _text_wh(self, drw, text, font):
        try:
            l,t,r,b = drw.textbbox((0,0), text, font=font)
            return (r-l, b-t)
        except AttributeError:
            return drw.textsize(text, font=font)

    def _now_str(self):
        return datetime.now().strftime("%H:%M / %d %b %Y").lower()

    def draw(self, subtitle, percent=None, animate=True, center_block=None, show_tail_lines=None, show_header=True):
        LW, LH = self._logical_size()
        img = Image.new('1', (LW, LH), 255)
        drw = ImageDraw.Draw(img)

        if show_header:
            drw.text((4, 2), TITLE, font=F_LG, fill=0)
            now_s = self._now_str()
            tw, th = self._text_wh(drw, now_s, F_SM)
            drw.text((LW - tw - 4, 2), now_s, font=F_SM, fill=0)

            if percent is None:
                # centered subtitle block under header
                lines = [p for p in subtitle.split("\n") if p.strip()]
                sizes = [self._text_wh(drw, ln, F_MD) for ln in lines]
                total_h = sum(h for _, h in sizes) + 2*(len(lines)-1)
                top, bottom = 26, LH - 20
                y = top + max(0, ((bottom - top) - total_h)//2)
                for ln, (tw2, th2) in zip(lines, sizes):
                    drw.text(((LW - tw2)//2, y), ln, font=F_MD, fill=0)
                    y += th2 + 2
            else:
                # progress bar
                x, y, w, h = 4, 46, LW - 8, 18
                drw.rectangle((x, y, x+w, y+h), outline=0, width=2)
                fill_w = int(max(0, min(100, percent)) * (w - 4) / 100)
                if fill_w > 0:
                    drw.rectangle((x+2, y+2, x+2+fill_w, y+h-2), fill=0)
                pct = f"{percent:3d}%"
                ptw, pth = self._text_wh(drw, pct, F_MD)
                px = x + (w - ptw) // 2
                py = y + (h - pth) // 2 - 1
                drw.rectangle((px - 2, py, px + ptw + 2, py + pth + 1), fill=255)
                drw.text((px, py), pct, font=F_MD, fill=0)

                # centered subtitle below bar
                lines = [p for p in subtitle.split("\n") if p.strip()]
                y2 = y + h + 6
                for ln in lines:
                    tw2, th2 = self._text_wh(drw, ln, F_MD)
                    drw.text(((LW - tw2)//2, y2), ln, font=F_MD, fill=0)
                    y2 += th2 + 2

        else:
            if subtitle:
                tw, th = self._text_wh(drw, subtitle, F_MD)
                drw.text(((LW - tw)//2, 6), subtitle, font=F_MD, fill=0)

        if center_block:
            lines = [ln for ln in center_block.strip().splitlines() if ln.strip()]
            total_h = sum(self._text_wh(drw, ln, F_SM)[1] for ln in lines) + (len(lines)-1)*4
            y_start = (LH - total_h) // 2
            for ln in lines:
                tw2, th2 = self._text_wh(drw, ln, F_SM)
                drw.text(((LW - tw2)//2, y_start), ln, font=F_SM, fill=0)
                y_start += th2 + 4

        if show_tail_lines and show_header:
            y0 = 70 if percent is not None else 46
            for i, ln in enumerate(show_tail_lines[:3]):
                drw.text((4, y0 + i*14), ln[:44], font=F_SM, fill=0)

        if animate and not center_block and show_header:
            base_y = LH - 16
            base_x = LW - 50
            sz, gap = 6, 6
            for i in range(4):
                x0 = base_x + i*(sz+gap)
                if self.anim % 4 == i:
                    drw.rectangle((x0, base_y, x0+sz, base_y+sz), fill=0)
                else:
                    drw.rectangle((x0, base_y, x0+sz, base_y+sz), outline=0, width=1)
            self.anim = (self.anim + 1) % 4

        out = self._rotate_for_display(img)
        if out.size != (self.pw, self.ph):
            out = out.resize((self.pw, self.ph))
        if show_header and (percent is not None):
            self._display_set_base_then_partial(out)
        else:
            self._display_full(out)


    def sleep(self):
        try: self.epd.sleep()
        except Exception: pass
        try: epdconfig.module_exit()
        except Exception: pass

def ensure_dir(p): os.makedirs(p, exist_ok=True)

def device_present():
    try:
        out = subprocess.run(["idevice_id", "-l"], capture_output=True, text=True).stdout.strip()
        return bool(out)
    except FileNotFoundError:
        return False

def extract_error_code(text: str):
    for pat in [r"Error\s*Code[: ]+(\d+)", r"ErrorCode[: ]+(\d+)", r"MBErrorDomain/(\d+)", r"\(Error\s*Code\s*(\d+)\)", r"mobilebackup2\s*\(\s*(-?\d+)\s*\)"]:
        m = re.search(pat, text, re.I)
        if m:
            try: return int(m.group(1))
            except Exception: pass
    return None

def resolve_error_message(code: int) -> str:
    return CFG["error_codes"].get(code, "Unknown error. Check logs.")

def log_open():
    ensure_dir(LOG_DIR)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(LOG_DIR, f"backup-{ts}.log")
    f = open(path, "a", buffering=1)
    f.write(f"[{ts}] backup started\n")
    return f, path

def check_backup_mount(panel, logf):
    ensure_dir(CFG["backup_dir"])
    marker = os.path.join(CFG["backup_dir"], CFG["marker_file"])
    if not os.path.exists(marker):
        msg = "Backup folder not found or not mounted"
        print(f"[ERROR] {msg} (missing {marker})")
        if logf: logf.write(f"[ERROR] {msg} (missing {marker})\n")
        panel.draw("Backup folder not found.", percent=None, animate=False, show_header=True)
        panel.sleep()
        sys.exit(2)

def tee_and_parse(proc, logf, on_line):
    while True:
        ch = proc.stdout.read(1)
        if ch == "" or ch is None:
            break
        sys.stdout.write(ch); sys.stdout.flush()
        if logf: logf.write(ch)
        if ch in ["\n", "\r"]:
            on_line("__LINE_BREAK__")
        else:
            on_line(ch)

def get_disk_usage_pct(device_path: str):
    try:
        out = subprocess.run(["df", "-P", device_path], capture_output=True, text=True, check=True).stdout.splitlines()
        if len(out) >= 2:
            tokens = out[-1].split()
            for tok in tokens:
                if tok.endswith("%") and tok[:-1].isdigit():
                    return int(tok[:-1])
    except Exception as e:
        print(f"[WARN] df failed for {device_path}: {e}")
    return None

def run_backup(panel, logf):
    check_backup_mount(panel, logf)
    cmd = ["idevicebackup2", "backup", CFG["backup_dir"]]
    print(f"[CMD] {' '.join(cmd)}", flush=True)
    if logf: logf.write(f"[CMD] {' '.join(cmd)}\n")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=0, universal_newlines=True)
    pct, encrypted, last_ui = None, False, 0
    last_pct = None
    cur_line = ""
    panel.draw(
        "Welcome.\nEnter device password when prompted.\nBackup will start soon.",
        percent=None, animate=False, show_header=True
    )

    def error_and_exit(user_msg, code=None, tail=None):
        panel.draw(f"Error: {user_msg}", percent=pct if pct is not None else 0, animate=False, show_tail_lines=([tail] if tail else None), show_header=True)
        center = "Wrong device password."
#        panel.draw("", percent=None, animate=False, center_block=center, show_header=False)
        if logf: logf.write(f"[ERROR] {user_msg} code={code} tail='{tail or ''}'\n")
        panel.sleep(); sys.exit(1)
        error_and_exit(msg, code, ln[-80:])

    def feed_parser(tok: str):
        nonlocal cur_line, pct, encrypted, last_ui, last_pct
        if tok == "__LINE_BREAK__":
            ln = cur_line; cur_line = ""
            if not ln: return

            # detect encryption mode
            if "Backup will be encrypted." in ln:
                encrypted = True
            elif "Backup will not be encrypted." in ln:
                encrypted = False
            elif re.search(r"\bEncryption enabled\b", ln, re.I):
                encrypted = True
            elif re.search(r"\bEncryption disabled\b", ln, re.I):
                encrypted = False

            if ln.startswith("Sending '") and "Status.plist" in ln:
                panel.draw("Preparing backup...", percent=pct, show_header=True)

            m = re.search(r"(\d+)%\s*[Ff]inished", ln)
            if m:
                pct = int(m.group(1))
                if pct != last_pct:
                    subtitle = "Backing up (encrypted)..." if encrypted else "Backing up (not encrypted)..."
                    panel.draw(subtitle, percent=pct, show_header=True)
                    last_pct = pct
                    last_ui = time.time()

            if re.search(r"\berror\b", ln, re.I):
                code = extract_error_code(ln)
                msg = resolve_error_message(code) if code is not None else "Unknown error. Check logs."
                error_and_exit(msg, code, ln[-80:])
            return
        else:
            cur_line += tok
            if time.time() - last_ui >= IDLE_REFRESH_SEC:
                if pct is None:
                    panel.draw("Starting backup...", percent=None, show_header=True)
                else:
                    subtitle = "Backing up (encrypted)..." if encrypted else "Backing up (not encrypted)..."
                    panel.draw(subtitle, percent=pct, show_header=True)
                last_ui = time.time()

    tee_and_parse(proc, logf, feed_parser)
    proc.wait(); rc = proc.returncode
    ts_end = datetime.now().strftime("%H:%M / %d %b %Y")
    if rc == 0:
        usage = get_disk_usage_pct(CFG["disk_device"])
        usage_str = f"{usage}%" if usage is not None else "n/a"
        owner = CFG["owner_lines"]
        # ensure ts_end is defined earlier: ts_end = datetime.now().strftime("%H:%M %d %b %y").lower()
        center = (
            f"Backup completed at {ts_end}.\n"
            f"{usage_str} memory usage.\n \n"
            f"{owner[0]}\n{owner[1]}\n{owner[2]}"
        )
        panel.draw("", percent=None, animate=False, center_block=center, show_header=False)
        if logf: logf.write(f"[OK] completed at {ts_end} usage={usage_str}\n")
    else:
        error_and_exit("Unknown error.\nCheck logs.", None, "rc!=0")
    time.sleep(2)
    return rc


def main():
    logf, logpath = log_open()
    print(f"[LOG] writing to {logpath}")
    if logf: logf.write(f"[LOG] writing to {logpath}\n")
    p = Panel()
    try:
        p.draw("Waiting for iPhone...", percent=None, animate=False, show_header=True)
        while True:
            if device_present():
                p.draw("Device detected. Preparing...", percent=None, animate=False, show_header=True)
                try: subprocess.run(["idevicepair", "validate"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except Exception: pass
                run_backup(p, logf); break
            else:
                time.sleep(2)
    except KeyboardInterrupt:
        if logf: logf.write("[INTERRUPT]\n")
    finally:
        try: p.sleep()
        except Exception: pass
        try: logf.close()
        except Exception: pass

if __name__ == "__main__":
    main()
