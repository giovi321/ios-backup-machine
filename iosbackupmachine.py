#!/usr/bin/env python3
import os, re, sys, time, subprocess, threading
from datetime import datetime
from periphery.gpio import GPIOError

import yaml
from PIL import Image, ImageDraw, ImageFont
from waveshare_epd import epd2in13_V4, epdconfig

CONFIG_PATH = os.getenv("IOSBACKUP_CONFIG", "/root/config.yaml")
LOG_DIR = "/var/log/iosbackupmachine"
IDLE_REFRESH_SEC = 4
TITLE = "iOS Backup Machine"

def load_config(path):
    with open(path, "r") as f:
        cfg = yaml.safe_load(f) or {}
    cfg.setdefault("backup_dir", "/media/iosbackup/")
    cfg.setdefault("marker_file", ".foldermarker")
    cfg.setdefault("disk_device", "/dev/mmcblk1")
    cfg.setdefault("orientation", "landscape_right")
    cfg.setdefault("owner_lines", ["Name", "telephone", "email", "message"])
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

CUSTOM_FONT = CFG.get("font_path")

def font(sz):
    try:
        return ImageFont.truetype(CUSTOM_FONT, sz)
    except Exception:
        return ImageFont.load_default()

F_SM = font(12)
F_MD = font(12)
F_LG = font(12)

class Panel:
    def __init__(self):
        try: epdconfig.module_exit()
        except Exception: pass
        self.epd = epd2in13_V4.EPD()
        self.pw, self.ph = self.epd.width, self.epd.height
        self.orient = str(CFG.get("orientation","landscape_right")).lower()
        self.anim = 0
        self._mode = "none"          # "none" | "full" | "partial"
        self._partial_ready = False

        # resolve API variants once
        self._disp       = getattr(self.epd, "display", None)
        self._disp_base  = getattr(self.epd, "display_Base", None) or getattr(self.epd, "displayPartBaseImage", None)
        self._disp_part  = getattr(self.epd, "display_Partial", None) or getattr(self.epd, "displayPartial", None)
        self._init_full0 = getattr(self.epd, "init_Full", None)
        self._init_part0 = getattr(self.epd, "init_Part", None) or getattr(self.epd, "init_fast", None)

        self._init_full(first=True)
        self.epd.Clear(0xFF)
        self._mode = "full"

        self._force_partial = False
        self._partial_count = 0
        self._partial_reset_every = 100

    def prepare_partial(self, base_img=None):
        # set a white base if none provided, then enter partial mode
        if base_img is None:
            LW, LH = self._logical_size()
            base_img = Image.new('1', (LW, LH), 255)
            base_img = self._rotate_for_display(base_img)
            if base_img.size != (self.pw, self.ph):
                base_img = base_img.resize((self.pw, self.ph))
        self._display_set_base_then_partial(base_img)
        self._force_partial = True
        self._partial_count = 0

    def _safe_init_call(self, fn):
        # Handle EBUSY by releasing lines and retrying once.
        try:
            fn(); return
        except GPIOError as e:
            if getattr(e, "errno", None) == 16:
                try: epdconfig.module_exit()
                except Exception: pass
                time.sleep(0.2)
                fn(); return
            raise

    def _init_full(self, first=False):
        if not first and self._mode == "full":
            return  # already in full
        if self._init_full0:
            self._safe_init_call(self._init_full0)
        else:
            def _init():
                if "mode" in self.epd.init.__code__.co_varnames: self.epd.init(0)
                else: self.epd.init()
            self._safe_init_call(_init)
        self._mode = "full"

    def _init_part(self):
        if self._mode == "partial":
            return  # already in partial
        if self._init_part0:
            self._safe_init_call(self._init_part0)
        else:
            def _init():
                if "mode" in self.epd.init.__code__.co_varnames: self.epd.init(1)
            try: self._safe_init_call(_init)
            except Exception: pass
        self._mode = "partial"

    def _getbuf(self, img):
        return self.epd.getbuffer(img)

    def _display_full(self, img):
        buf = self._getbuf(img)
        self._init_full()
        if self._disp: self._disp(buf)
        elif self._disp_base: self._disp_base(buf)
        else: self.epd.display(buf)  # fallback
        self._partial_ready = False  # next partial must re-set base

    def _display_set_base_then_partial(self, img):
        buf = self._getbuf(img)
        if self._disp_part and self._disp_base:
            if not self._partial_ready:
                self._init_full()
                self._disp_base(buf)     # set the base once
                self._init_part()        # switch to partial once
                self._partial_ready = True
            else:
                self._init_part()
            self._disp_part(buf)         # partial refresh
        else:
            self._display_full(img)

    def _logical_size(self):
        if self.orient in ("landscape_right", "landscape_left"):
            return (self.ph, self.pw)
        return (self.pw, self.ph)

    def _rotate_for_display(self, img):
        if self.orient == "landscape_right":
            return img.rotate(90, expand=True)
        elif self.orient == "landscape_left":
            return img.rotate(270, expand=True)
        return img

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
                lines = [p for p in subtitle.split("\n") if p.strip()]
                sizes = [self._text_wh(drw, ln, F_MD) for ln in lines]
                total_h = sum(h for _, h in sizes) + 2*(len(lines)-1)
                top, bottom = 26, LH - 20
                y = top + max(0, ((bottom - top) - total_h)//2)
                for ln, (tw2, th2) in zip(lines, sizes):
                    drw.text(((LW - tw2)//2, y), ln, font=F_MD, fill=0); y += th2 + 2
            else:
                x, y, w, h = 4, 46, LW - 8, 18
                drw.rectangle((x, y, x+w, y+h), outline=0, width=2)
                fill_w = int(max(0, min(100, percent)) * (w - 4) / 100)
                if fill_w > 0:
                    drw.rectangle((x+2, y+2, x+2+fill_w, y+h-2), fill=0)

                # centered percent label and background box
                pct = f"{percent:3d}%"
                cx = x + w // 2
                cy = y + h // 2
                try:
                    bbox = drw.textbbox((cx, cy), pct, font=F_MD, anchor="mm")
                    pad = 2
                    bg = (bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad)
                    drw.rectangle(bg, fill=255)
                    drw.text((cx, cy), pct, font=F_MD, fill=0, anchor="mm")
                except TypeError:
                    ptw, pth = self._text_wh(drw, pct, F_MD)
                    px = cx - ptw // 2
                    py = cy - pth // 2
                    drw.rectangle((px - 2, py - 2, px + ptw + 2, py + pth + 2), fill=255)
                    drw.text((px, py), pct, font=F_MD, fill=0)

                lines = [p for p in subtitle.split("\n") if p.strip()]
                y2 = y + h + 6
                for ln in lines:
                    tw2, th2 = self._text_wh(drw, ln, F_MD)
                    drw.text(((LW - tw2)//2, y2), ln, font=F_MD, fill=0); y2 += th2 + 2
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
                drw.text(((LW - tw2)//2, y_start), ln, font=F_SM, fill=0); y_start += th2 + 4

        if show_tail_lines and show_header:
            y0 = 70 if percent is not None else 46
            for i, ln in enumerate(show_tail_lines[:3]):
                drw.text((4, y0 + i*14), ln[:44], font=F_SM, fill=0)

        if animate and not center_block and show_header:
            base_y = LH - 16; base_x = LW - 50; sz, gap = 6, 6
            for i in range(4):
                x0 = base_x + i*(sz+gap)
                if self.anim % 4 == i: drw.rectangle((x0, base_y, x0+sz, base_y+sz), fill=0)
                else:                  drw.rectangle((x0, base_y, x0+sz, base_y+sz), outline=0, width=1)
            self.anim = (self.anim + 1) % 4

        out = self._rotate_for_display(img)
        if out.size != (self.pw, self.ph):  # safety. try to avoid resize churn for partials.
            out = out.resize((self.pw, self.ph))

        # choose refresh path
        use_partial = show_header and ((percent is not None) or self._force_partial)

        # optional hygiene: force an occasional full refresh to clear ghosting
        if use_partial and self._partial_count >= self._partial_reset_every:
            self._display_full(out)
            # re-enter partial on next frame
            self._partial_ready = False
            self._partial_count = 0
            return

        if use_partial:
            self._display_set_base_then_partial(out)
            self._partial_count += 1
        else:
            self._display_full(out)

    def sleep(self):
        try: self.epd.sleep()
        finally:
            try: epdconfig.module_exit()
            except Exception: pass

class Animator:
    """1 Hz UI animator so the bottom-right squares always animate."""
    def __init__(self, panel):
        self.panel = panel
        self.lock = threading.Lock()
        self.state = {
            "subtitle": "",
            "percent": None,
            "animate": True,
            "center_block": None,
            "show_tail_lines": None,
            "show_header": True,
        }
        self.running = False
        self.thread = None

    def set(self, **kwargs):
        with self.lock:
            self.state.update(kwargs)

    def _tick(self):
        while self.running:
            with self.lock:
                s = dict(self.state)
            # draw at 1 Hz so the four squares animate each second
            self.panel.draw(**s)
            time.sleep(1)

    def start(self):
        if self.running: return
        self.running = True
        self.thread = threading.Thread(target=self._tick, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=1)

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

def run_backup(panel, logf, ui):
    check_backup_mount(panel, logf)
    cmd = ["idevicebackup2", "backup", CFG["backup_dir"]]
    print(f"[CMD] {' '.join(cmd)}", flush=True)
    if logf: logf.write(f"[CMD] {' '.join(cmd)}\n")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=0, universal_newlines=True)
    pct, encrypted, last_ui = None, False, 0
    last_pct = None
    cur_line = ""

    ui.set(
        subtitle="Welcome.\nEnter device password when prompted.\nBackup will start soon.",
        percent=None, animate=True, show_header=True
    )

    def error_and_exit(user_msg, code=None, tail=None):
        ui.set(subtitle=f"Error: {user_msg}", percent=pct if pct is not None else 0,
               show_tail_lines=([tail] if tail else None), animate=True, show_header=True)
        if logf: logf.write(f"[ERROR] {user_msg} code={code} tail='{tail or ''}'\n")
        time.sleep(2)
        ui.stop()
        panel.sleep()
        sys.exit(1)

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
                ui.set(subtitle="Preparing backup...", percent=pct, animate=True, show_header=True)

            m = re.search(r"(\d+)%\s*[Ff]inished", ln)
            if m:
                pct = int(m.group(1))
                if pct != last_pct:
                    subtitle = "Backing up (encrypted)..." if encrypted else "Backing up (not encrypted)..."
                    ui.set(subtitle=subtitle, percent=pct, animate=True, show_header=True)
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
                    ui.set(subtitle="Starting backup...", percent=None, animate=True, show_header=True)
                else:
                    subtitle = "Backing up (encrypted)..." if encrypted else "Backing up (not encrypted)..."
                    ui.set(subtitle=subtitle, percent=pct, animate=True, show_header=True)
                last_ui = time.time()

    tee_and_parse(proc, logf, feed_parser)
    proc.wait(); rc = proc.returncode
    ts_end = datetime.now().strftime("%H:%M / %d %b %Y")
    if rc == 0:
        usage = get_disk_usage_pct(CFG["disk_device"])
        usage_str = f"{usage}%" if usage is not None else "n/a"
        owner = CFG["owner_lines"]
        center = (
            f"Backup completed at {ts_end}.\n"
            f"{usage_str} memory usage.\n"
            f"  \n"
            f"{owner[0]}\n{owner[1]}\n{owner[2]}\n{owner[3]}"
        )
        ui.stop()
        panel.draw("", percent=None, animate=False, center_block=center, show_header=False)
        if logf: logf.write(f"[OK] completed at {ts_end} usage={usage_str}\n")
        time.sleep(2)
        return 0
    else:
        error_and_exit("Unknown error.\nCheck logs.", None, "rc!=0")

def main():
    logf, logpath = log_open()
    print(f"[LOG] writing to {logpath}")
    if logf: logf.write(f"[LOG] writing to {logpath}\n")
    p = Panel()
    p.prepare_partial()  # enable partial for text screens

    ui = Animator(p)
    ui.start()
    try:
        ui.set(subtitle="Waiting for iPhone...", percent=None, animate=True, show_header=True)
        while True:
            if device_present():
                ui.set(subtitle="Device detected. Preparing...", percent=None, animate=True, show_header=True)
                try: subprocess.run(["idevicepair", "validate"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except Exception: pass
                run_backup(p, logf, ui); break
            else:
                time.sleep(0.3)
    except KeyboardInterrupt:
        if logf: logf.write("[INTERRUPT]\n")
    finally:
        try: ui.stop()
        except Exception: pass
        try: p.sleep()
        except Exception: pass
        try: logf.close()
        except Exception: pass

if __name__ == "__main__":
    main()
