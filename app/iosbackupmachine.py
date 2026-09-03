#!/usr/bin/env python3
import os, re, sys, time, json, glob, select, shutil, signal, subprocess, tempfile, threading
from datetime import datetime
from periphery.gpio import GPIOError

import yaml
from PIL import Image, ImageDraw, ImageFont
from waveshare_epd import epd2in13_V4, epdconfig

try:
    from notifications import send_notification
except ImportError:
    def send_notification(*a, **kw): pass

try:
    import netutil
except ImportError:
    netutil = None

try:
    import sync_manager as _sync_manager
except ImportError:
    _sync_manager = None

try:
    import config_schema as _config_schema
except ImportError:
    _config_schema = None

try:
    import wg_crypto as _wg_crypto   # iPhone presence / trust probe for the status icon
except ImportError:
    _wg_crypto = None

import uipolicy

CONFIG_PATH = os.getenv("IOSBACKUP_CONFIG", "/root/iosbackupmachine/config.yaml")
import logutil
# Logs are persistent (rootfs); runtime IPC stays on the volatile zram /var/log.
# See logutil.py for why they are split.
LOG_DIR = logutil.LOG_DIR
RUNTIME_DIR = logutil.RUNTIME_DIR
STATUS_FILE = os.path.join(RUNTIME_DIR, "backup_status.json")
# Sentinels the web UI drops to drive the always-on daemon (which can no longer
# be started/stopped by restarting its service — that would kill the EPD owner).
START_FILE = os.path.join(RUNTIME_DIR, "start_requested")   # force a backup (auto-start off)
STOP_FILE = os.path.join(RUNTIME_DIR, "stop_requested")     # abort the current backup
INFO_FILE = os.path.join(RUNTIME_DIR, "info_requested")     # single-tap -> show system-info screen
# Dropped by the web UI before it launches the updater. The installer stops this
# daemon partway through, so the "Updating" screen has to be the LAST thing painted:
# e-ink holds it with no power to the panel, right through the reboot. The file
# lives on the volatile runtime dir, so a reboot clears it by itself.
UPDATING_FILE = os.path.join(RUNTIME_DIR, "updating")       # system update in progress -> reboot pending
UPDATING_MAX_SEC = 1800   # safety valve: an update that dies without rebooting
                          # must not strand the panel on "Updating" forever
IDLE_REFRESH_SEC = 4
WG_RECONCILE_SEC = 10   # how often the WireGuard auto-connect watcher re-checks
WG_HANDSHAKE_GRACE_SEC = 45   # tolerate 'up but no handshake yet' this long before re-connecting
DRAW_FAIL_REINIT_AFTER = 30   # consecutive draw failures before one panel re-init, then headless
BACKUP_SILENCE_TIMEOUT_SEC = 600   # idevicebackup2 silent this long -> hung; terminate it
BACKUP_MAX_DURATION_SEC = 4 * 3600 # total cap for a single backup run
TITLE = "iOS Backup Machine"

def _apply_defaults(cfg):
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

def load_config(path):
    with open(path, "r") as f:
        cfg = yaml.safe_load(f) or {}
    return _apply_defaults(cfg)

try:
    CFG = load_config(CONFIG_PATH)
except Exception as e:
    # A config that crashes the loader must not take the daemon down with it:
    # fall back to the schema defaults so backups can still run.
    print(f"[WARN] Could not load config {CONFIG_PATH} ({e}); using built-in defaults", flush=True)
    CFG = _apply_defaults(_config_schema.apply_defaults({}) if _config_schema else {})
for k, v in CFG.get("env", {}).items():
    os.environ[k] = str(v)

CUSTOM_FONT = CFG.get("font_path") or "/root/iosbackupmachine/UbuntuMono-Regular.ttf"

def font(sz):
    try:
        return ImageFont.truetype(CUSTOM_FONT, sz)
    except Exception:
        return ImageFont.load_default()

F_SM = font(12)
F_MD = font(12)
F_LG = font(12)
F_14 = font(14)

# Process-wide shutdown latch. The SIGTERM/SIGINT handler sets it; the Animator
# thread (the sole EPD drawer) notices it, paints the owner screen one last time,
# sleeps the panel so the image persists after power-off, and exits.
SHUTDOWN = threading.Event()


# ---------------------------------------------------------------------------
# Main-loop heartbeat watchdog. The main loop (and every long-running hold it
# can legitimately sit in: backup output, post-backup holds) calls _heartbeat();
# if no beat lands within stall_sec the process is wedged somewhere without a
# watchdog of its own, so we log and exit 1 — Restart=on-failure brings back a
# healthy daemon. Time and exit are injectable so tests can drive it without
# threads or os._exit.
# ---------------------------------------------------------------------------
class LoopWatchdog:
    def __init__(self, stall_sec, exit_fn=None, time_fn=None, log_fn=None):
        self.stall_sec = stall_sec
        self._exit = exit_fn or os._exit
        self._time = time_fn or time.monotonic
        self._log = log_fn or (lambda m: print(m, flush=True))
        self._last_beat = self._time()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.thread = None

    def beat(self):
        with self._lock:
            self._last_beat = self._time()

    def check(self):
        """One watchdog pass; returns True when the loop proved stalled."""
        with self._lock:
            stalled = (self._time() - self._last_beat) > self.stall_sec
        if stalled:
            self._log(f"[FATAL] main loop stalled (no heartbeat for >{int(self.stall_sec)}s)")
            self._exit(1)
            return True
        return False

    def _run(self):
        while not self._stop.wait(5):
            if self.check():
                return

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self._stop.set()


_loop_watchdog = None

def _heartbeat():
    wd = _loop_watchdog
    if wd is not None:
        try: wd.beat()
        except Exception: pass


# ---------------------------------------------------------------------------
# Shared drawing helpers (folded in from the former standalone screen scripts:
# boot-message.py, button-info.py, owner-message.py, unplug-notify.py).
# The display service is now the single owner of the EPD — nothing else draws.
# ---------------------------------------------------------------------------

def draw_small_power_icon(drw, x, y, size=10):
    """Small power-on indicator (circle with stem)."""
    cx, cy = x + size // 2, y + size // 2
    r = size // 2
    drw.arc((cx - r, cy - r, cx + r, cy + r), start=300, end=240, fill=0, width=1)
    drw.line((cx, cy - r, cx, cy - 1), fill=0, width=1)


def draw_project_icon(drw, cx, cy, size=36):
    """1-bit project icon: rounded-rect outline + downward arrow."""
    half = size // 2
    x0, y0 = cx - half, cy - half
    x1, y1 = cx + half, cy + half
    drw.rounded_rectangle((x0, y0, x1, y1), radius=size // 6, outline=0, width=2)
    arrow_w = size * 2 // 5
    margin = size // 5
    drw.line((cx, y0 + margin, cx, y1 - margin), fill=0, width=2)
    head_y = cy + margin // 2
    drw.line((cx - arrow_w // 2, head_y, cx, y1 - margin), fill=0, width=2)
    drw.line((cx + arrow_w // 2, head_y, cx, y1 - margin), fill=0, width=2)


# ---------------------------------------------------------------------------
# Status icons (drawn on every live screen): power (always on) + vpn / internet
# / wifi / iphone, each crossed out with a "/" when inactive. Connectivity is
# sampled by a background thread (have_connectivity blocks on a socket test) and
# cached, so the draw path never does network I/O.
# ---------------------------------------------------------------------------
STATUS_BAR_H = 14   # vertical space the icon row reserves at the bottom of a screen

_icon_status = {"vpn": False, "internet": False, "wifi": False, "iphone": "absent"}
_icon_status_lock = threading.Lock()


def _icon_slash(drw, x, y, sz):
    """Diagonal '/' through an icon to mark it inactive."""
    drw.line((x - 1, y + sz, x + sz, y - 1), fill=0, width=1)


def draw_icon_vpn(drw, x, y, sz=10, on=True):
    """Minimal padlock = VPN."""
    drw.arc((x + 2, y, x + sz - 2, y + sz - 1), start=180, end=360, fill=0)        # shackle
    drw.rectangle((x + 1, y + sz // 2, x + sz - 1, y + sz), outline=0, width=1)    # body
    if not on:
        _icon_slash(drw, x, y, sz)


def draw_icon_internet(drw, x, y, sz=10, on=True):
    """Minimal globe = internet."""
    drw.ellipse((x, y, x + sz, y + sz), outline=0, width=1)
    drw.line((x, y + sz // 2, x + sz, y + sz // 2), fill=0, width=1)               # equator
    drw.ellipse((x + sz // 3, y, x + sz - sz // 3, y + sz), outline=0, width=1)    # meridian
    if not on:
        _icon_slash(drw, x, y, sz)


def draw_icon_wifi(drw, x, y, sz=10, on=True):
    """Minimal Wi-Fi = dot + two arcs."""
    cx = x + sz // 2
    drw.ellipse((cx - 1, y + sz - 2, cx + 1, y + sz), fill=0)                      # dot
    drw.arc((x + 2, y + sz - 6, x + sz - 2, y + sz + 2), start=205, end=335, fill=0)
    drw.arc((x, y + sz - 10, x + sz, y + sz - 2), start=215, end=325, fill=0)
    if not on:
        _icon_slash(drw, x, y, sz)


def draw_icon_iphone(drw, x, y, sz=10, state="absent"):
    """iPhone presence, three states:
       'absent'    -> phone, slashed (not plugged / not seen by usbmux)
       'untrusted' -> phone + closed padlock (plugged, but Trust not granted or
                      the phone is locked, so lockdown info can't be read)
       'trusted'   -> phone + open padlock (Trust granted; the VPN config can be
                      decrypted)."""
    if state is True:
        state = "trusted"
    elif not state:
        state = "absent"
    px0, px1 = x + 2, x + sz - 2
    drw.rounded_rectangle((px0, y, px1, y + sz), radius=1, outline=0, width=1)
    drw.line((px0 + 1, y + 2, px1 - 1, y + 2), fill=0, width=1)                    # speaker
    cx = (px0 + px1) // 2
    if state == "absent":
        drw.ellipse((cx - 1, y + sz - 3, cx + 1, y + sz - 1), fill=0)              # home dot
        _icon_slash(drw, x, y, sz)
        return
    # Clear the screen so the glyph reads on white. untrusted -> a small closed
    # padlock (locked); trusted -> a checkmark (Trust granted, config decryptable).
    drw.rectangle((px0 + 1, y + 4, px1 - 1, y + sz - 1), fill=255)
    if state == "trusted":
        drw.line((px0 + 1, y + 6, px0 + 2, y + 8), fill=0, width=1)                # checkmark
        drw.line((px0 + 2, y + 8, px1 - 1, y + 5), fill=0, width=1)
    else:
        drw.arc((cx - 1, y + 4, cx + 1, y + 6), start=180, end=360, fill=0)        # closed shackle
        drw.rectangle((cx - 1, y + 6, cx + 1, y + 8), fill=0)                      # solid body (locked)


def _iphone_presence():
    """iPhone USB state for the status icon: 'absent' | 'untrusted' | 'trusted'.

    'trusted' = lockdown info is readable (SerialNumber), which is exactly what
    udid-mode decryption needs, so the VPN can come up. 'untrusted' = usbmux sees
    the device (a UDID) but lockdown is not accessible: Trust not granted, or the
    phone is locked. A running backup implies a present, trusted device, so skip
    the probe then to avoid piling lockdown queries onto idevicebackup2."""
    if _backup_running:
        return "trusted"
    if _wg_crypto is None:
        return "absent"
    try:
        if not _wg_crypto.get_iphone_udid():
            return "absent"
    except Exception:
        return "absent"
    try:
        return "trusted" if _wg_crypto.get_iphone_serial() else "untrusted"
    except Exception:
        return "untrusted"


def _compute_icon_status():
    """Sample VPN / internet / WiFi reachability and iPhone presence (blocking)."""
    vpn = internet = wifi = False
    try:
        iface = CFG.get("wireguard", {}).get("interface_name", "wg0")
        vpn = subprocess.run(["ip", "link", "show", iface],
                             capture_output=True, timeout=3).returncode == 0
    except Exception:
        pass
    if netutil is not None:
        try: wifi = netutil.get_wifi_ip() is not None
        except Exception: pass
        try: internet = bool(netutil.have_connectivity(timeout=2))
        except Exception: pass
    return {"vpn": vpn, "internet": internet, "wifi": wifi, "iphone": _iphone_presence()}


def get_icon_status():
    with _icon_status_lock:
        return dict(_icon_status)


def draw_status_bar(drw, LW, LH):
    """Bottom-left status row drawn LAST on every live screen: power (always on)
    then vpn / internet / wifi / iphone, each slashed when inactive. Reads the
    cached status only — no network I/O on the draw path.

    The icon area is cleared to white first, so even if a screen's text strayed
    into the bottom strip the icons can never end up overlapping it. Screens also
    reserve STATUS_BAR_H of bottom space, so normally nothing is here to clear."""
    st = get_icon_status()
    sz, gap = 10, 4
    n = 5
    icon_end = 3 + n * (sz + gap)
    drw.rectangle((0, LH - STATUS_BAR_H, icon_end, LH), fill=255)
    y = LH - sz - 2
    x = 3
    draw_small_power_icon(drw, x, y, size=sz);            x += sz + gap
    draw_icon_vpn(drw, x, y, sz, st["vpn"]);              x += sz + gap
    draw_icon_internet(drw, x, y, sz, st["internet"]);    x += sz + gap
    draw_icon_wifi(drw, x, y, sz, st["wifi"]);            x += sz + gap
    draw_icon_iphone(drw, x, y, sz, st["iphone"]);        x += sz + gap


def _normpath(p):
    if not p:
        return p
    return os.path.abspath(os.path.expanduser(os.path.expandvars(p)))


def get_ip_addr():
    try:
        out = subprocess.check_output(["hostname", "-I"], text=True, timeout=5).strip()
        return out.split()[0] if out else "No IP"
    except Exception:
        return "No IP"


def get_soc_temp():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
            return round(int(f.read().strip()) / 1000, 1)
    except Exception:
        return None


def _device_to_mountpoint(dev):
    try:
        with open("/proc/mounts", "r") as f:
            rows = [ln.split() for ln in f.read().splitlines()]
        mp = None
        for src, mnt, *_ in rows:
            if src == dev and (not mp or len(mnt) > len(mp)):
                mp = mnt
        return mp
    except Exception:
        return None


def get_free_disk_pct():
    dev = CFG.get("disk_device")
    mp = _device_to_mountpoint(dev) if dev and str(dev).startswith("/dev/") else None
    if not mp:
        mp = _normpath(CFG.get("backup_dir")) or "/"
    try:
        st = os.statvfs(mp)
        total = st.f_blocks * st.f_frsize
        if total <= 0:
            return None
        return round(st.f_bavail * st.f_frsize / total * 100, 1)
    except Exception:
        return None


def get_last_backup_str():
    bd = _normpath(CFG.get("backup_dir"))
    if not bd or not os.path.isdir(bd):
        return "No backups"
    try:
        mtimes = [e.stat(follow_symlinks=True).st_mtime
                  for e in os.scandir(bd) if e.is_dir(follow_symlinks=True)]
        if not mtimes:
            return "No backups"
        return time.strftime("%H:%M / %d %b %Y", time.localtime(max(mtimes)))
    except Exception:
        return "No backups"


def get_vpn_status():
    try:
        iface = CFG.get("wireguard", {}).get("interface_name", "wg0")
        r = subprocess.run(["ip", "link", "show", iface], capture_output=True, text=True, timeout=3)
        return "VPN: connected" if r.returncode == 0 else "VPN: off"
    except Exception:
        return "VPN: off"


def _wifi_nickname_for_ssid(ssid):
    """Nickname configured for ``ssid`` in config.yaml (read live), or ''."""
    if not ssid:
        return ""
    try:
        with open(CONFIG_PATH, "r") as f:
            live = yaml.safe_load(f) or {}
        for net in (live.get("wifi", {}).get("networks", []) or []):
            if (net.get("ssid") or "") == ssid:
                return (net.get("nickname") or "").strip()
    except Exception:
        pass
    return ""


def get_connection_line():
    """Active network for the info screen: WiFi (with nickname/SSID) vs iPhone
    hotspot vs Ethernet vs none.

    The type is decided from interface IPs (a single fast `ip addr` read), and the
    WiFi SSID is queried ONLY when actually on WiFi — so the info screen never
    spawns the wireless tools (iwgetid/iw/wpa_cli), which can each block for
    seconds while on the iPhone hotspot, stalling the button."""
    ip = typ = None
    if netutil is not None:
        try:
            ip, typ = netutil.get_active_ip()
        except Exception:
            ip = typ = None
    if typ == "wifi":
        ssid = None
        try:
            ssid = netutil.get_wifi_ssid()
        except Exception:
            ssid = None
        name = _wifi_nickname_for_ssid(ssid) or ssid
        return f"Net: WiFi {name}" if name else "Net: WiFi"
    if typ == "usb_iphone":
        return "Net: iPhone hotspot"
    if ip:
        return "Net: Ethernet"
    return "Net: none"


def _ts_full(iso):
    """Format an ISO timestamp exactly like the last-backup line ('%H:%M / %d %b
    %Y'), so the Sync line matches the Backup line on the info screen."""
    try:
        return datetime.fromisoformat(iso).strftime("%H:%M / %d %b %Y")
    except Exception:
        return ""


def get_last_sync_str():
    """Last/current remote-sync result for the info screen. The timestamp is
    formatted like get_last_backup_str() so both lines read consistently."""
    try:
        with open(STATUS_FILE, "r") as f:
            st = json.load(f)
        state = st.get("state")
        if state == "syncing":
            return "running"
        if state == "sync_complete":
            return ("OK " + _ts_full(st.get("timestamp"))).strip()
        if state == "sync_error":
            return ("failed " + _ts_full(st.get("timestamp"))).strip()
    except Exception:
        pass
    try:
        logs = glob.glob(os.path.join(LOG_DIR, "sync-*.log"))
        if logs:
            newest = max(logs, key=os.path.getmtime)
            return time.strftime("%H:%M / %d %b %Y", time.localtime(os.path.getmtime(newest)))
    except Exception:
        pass
    return "none"


def build_button_info_lines():
    """Lines for the single-tap system-info screen: (text, big?). Fully guarded:
    it must never raise or block, or the single-tap would silently do nothing."""
    try:
        now = datetime.now()
        temp = get_soc_temp()
        free = get_free_disk_pct()
        tstr = f"{temp}C" if temp is not None else "n/a"
        fstr = f"{free}%" if free is not None else "n/a"
        # Date + time share one line so the 6 status lines + the bottom icon strip
        # all fit on the panel without overlapping.
        return [
            (now.strftime("%d %b %Y  %H:%M"), True),
            (get_connection_line(), False),
            (f"IP: {get_ip_addr()}", False),
            (get_vpn_status(), False),
            (f"Backup: {get_last_backup_str()}", False),
            (f"Sync: {get_last_sync_str()}", False),
            (f"Temp {tstr}  SD {fstr}", False),
        ]
    except Exception:
        now = datetime.now()
        return [
            (now.strftime("%d %b %Y"), True),
            (now.strftime("%H:%M"), True),
            ("Status unavailable", False),
        ]


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

    def _wrap_text(self, drw, text, font, max_w):
        """Word-wrap `text` to lines no wider than max_w px, so long messages
        don't overflow the display. Returns a list of lines."""
        words = str(text).split()
        if not words:
            return [str(text)]
        lines, cur = [], words[0]
        for w in words[1:]:
            test = cur + " " + w
            if self._text_wh(drw, test, font)[0] <= max_w:
                cur = test
            else:
                lines.append(cur)
                cur = w
        lines.append(cur)
        return lines

    def _now_str(self):
        return datetime.now().strftime("%H:%M / %d %b %Y").lower()

    def _draw_power_icon(self, drw, x, y, size=10):
        """Draw a small power-on icon (circle with line) at (x, y)."""
        cx, cy = x + size // 2, y + size // 2
        r = size // 2
        drw.arc((cx - r, cy - r, cx + r, cy + r), start=300, end=240, fill=0, width=1)
        drw.line((cx, cy - r, cx, cy - 1), fill=0, width=1)

    def _show_full(self, img):
        """Rotate a logical image to the panel and push it as a full refresh.
        Used for the static screens (boot, info, interrupted, owner)."""
        out = self._rotate_for_display(img)
        if out.size != (self.pw, self.ph):
            out = out.resize((self.pw, self.ph))
        self._display_full(out)

    def _draw_boot(self):
        """Boot / idle screen: project icon + title + owner info."""
        LW, LH = self._logical_size()
        img = Image.new('1', (LW, LH), 255)
        drw = ImageDraw.Draw(img)
        ICON = 28   # leaves room for the status strip below the owner lines
        title = TITLE
        tw, th = self._text_wh(drw, title, F_14)
        owner = [str(l) for l in CFG.get("owner_lines", []) if str(l).strip()]
        oh = [self._text_wh(drw, l, F_SM)[1] for l in owner]
        gap_it, gap_to, ls = 8, 6, 4
        total = ICON + gap_it + th + gap_to + sum(oh) + max(0, len(owner) - 1) * ls
        y = max(2, (LH - STATUS_BAR_H - total) // 2)   # leave room for the status row
        draw_project_icon(drw, LW // 2, y + ICON // 2, ICON)
        y += ICON + gap_it
        drw.text(((LW - tw) // 2, y), title, font=F_14, fill=0); y += th + gap_to
        for l in owner:
            w, h = self._text_wh(drw, l, F_SM)
            drw.text(((LW - w) // 2, y), l, font=F_SM, fill=0); y += h + ls
        draw_status_bar(drw, LW, LH)
        self._show_full(img)

    def _draw_info(self, lines):
        """Single-tap system-info screen. lines: list of (text, big?)."""
        LW, LH = self._logical_size()
        img = Image.new('1', (LW, LH), 255)
        drw = ImageDraw.Draw(img)
        spacing = 4
        heights = [self._text_wh(drw, t, F_14 if big else F_SM)[1] if t else 4 for t, big in lines]
        total = sum(heights) + spacing * (len(lines) - 1)
        y = max(2, (LH - STATUS_BAR_H - total) // 2)   # leave room for the status row
        for t, big in lines:
            if t:
                f = F_14 if big else F_SM
                w, h = self._text_wh(drw, t, f)
                drw.text(((LW - w) // 2, y), t, font=f, fill=0); y += h + spacing
            else:
                y += 4 + spacing
        draw_status_bar(drw, LW, LH)
        self._show_full(img)

    def _draw_interrupted(self, when_str):
        """Backup-interrupted screen: header + timestamp + owner info."""
        LW, LH = self._logical_size()
        img = Image.new('1', (LW, LH), 255)
        drw = ImageDraw.Draw(img)
        owner = [str(l) for l in CFG.get("owner_lines", []) if str(l).strip()]
        lines = [("Backup interrupted", F_14), (when_str or self._now_str(), F_SM), ("", None)]
        lines += [(l, F_SM) for l in owner]
        spacing = 5
        heights = [self._text_wh(drw, t, f)[1] if t else 4 for t, f in lines]
        total = sum(heights) + spacing * (len(lines) - 1)
        y = max(2, (LH - STATUS_BAR_H - total) // 2)   # leave room for the status row
        for t, f in lines:
            if t:
                w, h = self._text_wh(drw, t, f)
                drw.text(((LW - w) // 2, y), t, font=f, fill=0); y += h + spacing
            else:
                y += 4 + spacing
        draw_status_bar(drw, LW, LH)
        self._show_full(img)

    def draw_owner(self):
        """Power-off screen: owner info only (persists on e-paper after power cut)."""
        LW, LH = self._logical_size()
        img = Image.new('1', (LW, LH), 255)
        drw = ImageDraw.Draw(img)
        owner = [str(l) for l in CFG.get("owner_lines", []) if str(l).strip()]
        heights = [self._text_wh(drw, l, F_14)[1] for l in owner]
        total = sum(heights) + max(0, len(owner) - 1) * 6
        y = (LH - total) // 2
        for l in owner:
            w, h = self._text_wh(drw, l, F_14)
            drw.text(((LW - w) // 2, y), l, font=F_14, fill=0); y += h + 6
        self._show_full(img)

    def draw_updating(self):
        """System-update screen: the last frame before the installer stops this
        daemon and reboots. No status icons — they would freeze mid-update and
        read as live. E-ink holds this image with the panel asleep, so it is what
        the user sees for the whole update and the reboot that follows."""
        LW, LH = self._logical_size()
        img = Image.new('1', (LW, LH), 255)
        drw = ImageDraw.Draw(img)
        lines = [("Updating", F_14), ("Device will reboot", F_SM),
                 ("Web UI back shortly", F_SM)]
        spacing = 6
        heights = [self._text_wh(drw, t, f)[1] for t, f in lines]
        total = sum(heights) + spacing * (len(lines) - 1)
        y = max(2, (LH - total) // 2)
        for t, f in lines:
            w, h = self._text_wh(drw, t, f)
            drw.text(((LW - w) // 2, y), t, font=f, fill=0); y += h + spacing
        self._show_full(img)

    def draw(self, subtitle="", percent=None, animate=True, center_block=None,
             show_tail_lines=None, show_header=True, screen="normal", info_lines=None,
             full=False):
        # Static, non-"normal" screens render once via full refresh.
        if screen == "boot":
            return self._draw_boot()
        if screen == "info":
            return self._draw_info(info_lines or [])
        if screen == "interrupted":
            return self._draw_interrupted(subtitle)
        if screen == "owner":
            return self.draw_owner()
        if screen == "updating":
            return self.draw_updating()
        # screen in ("normal", "complete"): the header/percent/center-block layout below.
        LW, LH = self._logical_size()
        content_bottom = LH - STATUS_BAR_H   # all text must stay above the status strip
        img = Image.new('1', (LW, LH), 255)
        drw = ImageDraw.Draw(img)

        if show_header:
            drw.text((4, 2), TITLE, font=F_LG, fill=0)
            now_s = self._now_str()
            tw, th = self._text_wh(drw, now_s, F_SM)
            drw.text((LW - tw - 4, 2), now_s, font=F_SM, fill=0)

            if percent is None:
                lines = []
                for p in subtitle.split("\n"):
                    if p.strip():
                        lines.extend(self._wrap_text(drw, p, F_MD, LW - 8))
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

                lines = []
                for p in subtitle.split("\n"):
                    if p.strip():
                        lines.extend(self._wrap_text(drw, p, F_MD, LW - 8))
                y2 = y + h + 6
                for ln in lines:
                    tw2, th2 = self._text_wh(drw, ln, F_MD)
                    drw.text(((LW - tw2)//2, y2), ln, font=F_MD, fill=0); y2 += th2 + 2
        else:
            if subtitle:
                lines = self._wrap_text(drw, subtitle, F_MD, LW - 8)
                y = 6
                for ln in lines:
                    tw, th = self._text_wh(drw, ln, F_MD)
                    drw.text(((LW - tw)//2, y), ln, font=F_MD, fill=0); y += th + 2

        # center_block belongs to the "complete" result screen only. Guard against
        # a stale center_block (left in the Animator state by a previous result
        # screen) bleeding onto a normal screen and overlapping its subtitle.
        if center_block and screen == "complete":
            lines = []
            for ln in center_block.strip().splitlines():
                if ln.strip():
                    lines.extend(self._wrap_text(drw, ln, F_SM, LW - 8))
            total_h = sum(self._text_wh(drw, ln, F_SM)[1] for ln in lines) + (len(lines)-1)*4
            top = 24 if show_header else 4
            y_start = top + max(0, ((content_bottom - top) - total_h) // 2)
            for ln in lines:
                tw2, th2 = self._text_wh(drw, ln, F_SM)
                drw.text(((LW - tw2)//2, y_start), ln, font=F_SM, fill=0); y_start += th2 + 4

        if show_tail_lines and show_header:
            y0 = 70 if percent is not None else 46
            for i, ln in enumerate(show_tail_lines[:3]):
                ly = y0 + i*14
                if ly + 12 > content_bottom:
                    break   # keep tail output above the status strip
                drw.text((4, ly), ln[:44], font=F_SM, fill=0)

        if animate and not center_block and show_header:
            base_y = LH - 11; base_x = LW - 50; sz, gap = 6, 6
            for i in range(4):
                x0 = base_x + i*(sz+gap)
                if self.anim % 4 == i: drw.rectangle((x0, base_y, x0+sz, base_y+sz), fill=0)
                else:                  drw.rectangle((x0, base_y, x0+sz, base_y+sz), outline=0, width=1)
            self.anim = (self.anim + 1) % 4

        # Status icons drawn LAST so screen text can never paint over them.
        draw_status_bar(drw, LW, LH)

        out = self._rotate_for_display(img)
        if out.size != (self.pw, self.ph):  # safety. try to avoid resize churn for partials.
            out = out.resize((self.pw, self.ph))

        # One-shot full refresh requested for a screen-type transition (e.g.
        # backup-complete -> sync, sync -> result): clears the previous screen so
        # it can't ghost through the partial-refresh updates that follow.
        if full:
            self._display_full(out)
            self._partial_ready = False
            self._partial_count = 0
            return

        # choose refresh path
        use_partial = show_header and ((percent is not None) or self._force_partial)

        # force an occasional full refresh to clear ghosting
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

class NullPanel:
    """Headless stand-in for Panel: same interface, every method a no-op, so a
    display that can't init (or dies at runtime) doesn't take down backups,
    sync and notifications with it."""
    def __init__(self):
        self.pw = self.ph = 0
        self._mode = "none"
        self._partial_ready = False

    def prepare_partial(self, base_img=None): pass
    def draw(self, **kw): pass
    def draw_owner(self): pass
    def draw_updating(self): pass
    def sleep(self): pass

class Animator:
    """The single EPD drawer. Runs at 1 Hz for the whole process lifetime:
    animated screens (waiting / backup / sync) are redrawn every tick so the
    bottom-right squares animate; static screens (boot / info / interrupted /
    complete / owner) are drawn once and then skipped, so the e-ink doesn't
    flash. Nothing else in the process touches the EPD, which is what makes this
    the 'single e-paper owner'. On shutdown it paints the owner screen and
    sleeps the panel so the image persists after power-off."""
    def __init__(self, panel):
        self.panel = panel
        self.lock = threading.Lock()
        self.state = {
            "screen": "boot",
            "subtitle": "",
            "percent": None,
            "animate": False,
            "center_block": None,
            "show_tail_lines": None,
            "show_header": True,
            "info_lines": None,
        }
        self.running = False
        self.thread = None
        self.exit_code = 0          # fatal main-loop exit sets this to 1
        self._draw_failures = 0     # consecutive draw exceptions (panel may be dying)
        self._last = None
        self._last_layout = None     # screen-type signature of the last drawn frame
        self._force_full = False     # one-shot: next draw is a clean full refresh
        self._pending = {}           # updates made while the info screen holds the panel

    def set(self, _force=False, **kwargs):
        """Update what the next tick draws.

        Held back while the single-tap info screen is up, so that screen
        survives its window no matter who tries to draw over it — this is the
        one place the rule is enforced, instead of at every call site (where it
        was previously missed, letting a sync tick wipe the info screen half a
        second after the tap). The button listener passes _force to place and
        later restore the screen. Held-back updates are queued, not dropped, so
        resume() can still land a one-shot screen (a backup result) that was
        drawn while the panel was busy showing info."""
        if not _force and _info_window.active(_now()):
            with self.lock:
                self._pending.update(kwargs)
            return
        with self.lock:
            self.state.update(kwargs)

    def cancel_info(self):
        """Give up the info screen without restoring anything, for an operation
        that takes the panel for minutes (a backup). Queued updates go with it,
        so a later resume() can't apply state that is by then stale."""
        _info_window.cancel()
        with self.lock:
            self._pending = {}

    def resume(self, fallback):
        """Leave the info screen: go back to `fallback`, then apply whatever was
        drawn while it was up (see uipolicy.merge_resume)."""
        with self.lock:
            self.state.update(uipolicy.merge_resume(fallback, self._pending))
            self._pending = {}
        self.request_full()

    def request_full(self):
        """Make the next rendered frame a full refresh (clears ghosting on a
        screen-type transition). One-shot; consumed by the next tick."""
        self._force_full = True

    def get_state(self):
        with self.lock:
            return dict(self.state)

    def _do_shutdown(self, exit_code=None):
        # Paint the final screen one last time, crisp full refresh, then sleep
        # the panel so the e-paper holds the image after PiSugar cuts power.
        # A system update stops this daemon partway through, so it gets the
        # "Updating" screen rather than the power-off owner screen — painting it
        # here rather than relying on a tick landing first removes the race with
        # the installer's `systemctl stop`.
        if exit_code is None:
            exit_code = self.exit_code
        try:
            if _updating_requested():
                self.panel.draw_updating()
            else:
                self.panel.draw_owner()
        except Exception:
            pass
        try:
            self.panel.sleep()
        except Exception:
            pass
        # Don't orphan a running idevicebackup2 on the way out (KillMode=mixed
        # covers `systemctl stop`; this covers the os._exit paths).
        try:
            proc = _backup_proc
            if proc is not None and proc.poll() is None:
                proc.terminate()
        except Exception:
            pass
        os._exit(exit_code)

    def _handle_draw_failure_streak(self):
        """The panel has failed DRAW_FAIL_REINIT_AFTER draws in a row: try one
        full re-init (a stale SPI/GPIO handle can recover); if that also fails,
        drop to a NullPanel so the journal isn't spammed 1 Hz forever."""
        self._draw_failures = 0
        print(f"[DRAW] {DRAW_FAIL_REINIT_AFTER} consecutive failures; re-initializing panel", flush=True)
        try:
            panel = Panel()
            panel.prepare_partial()
        except Exception as e:
            print(f"[DRAW] panel re-init failed ({e}); running headless", flush=True)
            panel = NullPanel()
        self.panel = panel
        self._last = None
        self._last_layout = None
        self._force_full = True

    def _tick(self):
        while self.running:
            if SHUTDOWN.is_set():
                self._do_shutdown(); return
            with self.lock:
                s = dict(self.state)
            # Auto-detect a screen change and force ONE full refresh on it, so the
            # previous screen can never ghost/overlap behind the new one. This is
            # the single mechanism that guarantees one screen at a time.
            # For static screens the key includes the actual text, so two
            # different results (e.g. "Sync failed" -> "Sync complete") still
            # trigger a clearing full refresh. Animated screens (backup/sync
            # progress) redraw via partial every tick, so their content is excluded.
            if s.get("animate"):
                content = None
            else:
                il = s.get("info_lines")
                content = (s.get("subtitle"), s.get("center_block"),
                           tuple(tuple(x) for x in il) if il else None)
            layout = (s.get("screen"), bool(s.get("show_header")),
                      s.get("percent") is None, content)
            if layout != self._last_layout:
                self._force_full = True
                self._last_layout = layout
            # Redraw every tick for animated screens; otherwise only on change,
            # so a static screen isn't re-flashed once a second. A pending
            # full-refresh request also forces a redraw even if state is unchanged.
            if self._force_full or s.get("animate") or s != self._last:
                full = self._force_full
                self._force_full = False
                try:
                    self.panel.draw(full=full, **s)
                    self._last = s
                    self._draw_failures = 0
                except Exception as e:
                    self._draw_failures += 1
                    print(f"[DRAW] {e}", flush=True)
                    if self._draw_failures >= DRAW_FAIL_REINIT_AFTER:
                        self._handle_draw_failure_streak()
            if SHUTDOWN.wait(1):    # wakes immediately when shutdown is requested
                self._do_shutdown(); return

    def start(self):
        if self.running: return
        self.running = True
        self.thread = threading.Thread(target=self._tick, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=2)

def ensure_dir(p): os.makedirs(p, exist_ok=True)

def fmt_bytes(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024

def write_status(state, **extra):
    """Atomic status write — tmp file + rename so partial reads can't happen."""
    tmp = None
    try:
        ensure_dir(RUNTIME_DIR)
        data = {"state": state, "timestamp": datetime.now().isoformat(), **extra}
        tmp = STATUS_FILE + f".tmp.{os.getpid()}"
        with open(tmp, "w") as f:
            json.dump(data, f)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
        os.replace(tmp, STATUS_FILE)
    except Exception:
        if tmp:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass

def _boot_time():
    """Wall-clock time the kernel booted, from /proc/uptime. 0 if unreadable,
    which makes every sentinel look post-boot (fail towards showing the screen)."""
    try:
        with open("/proc/uptime") as f:
            return time.time() - float(f.read().split()[0])
    except Exception:
        return 0.0

def _updating_requested():
    """True while a system update is running (web UI drops UPDATING_FILE).

    Two ways the sentinel stops counting:

    - It predates this boot. The update ends in a reboot, and RUNTIME_DIR is
      meant to go with it — but armbian-ramlog syncs zram /var/log back to disk
      and restores it, so the file can outlive the reboot it was supposed to
      die in. Comparing against boot time is what actually retires it, while
      still keeping the screen up across the daemon restart the installer does
      mid-update (same boot).
    - It is older than UPDATING_MAX_SEC. Backstop for an update that dies
      without ever rebooting, so the panel isn't stranded on "Updating".
    """
    try:
        mtime = os.stat(UPDATING_FILE).st_mtime
    except Exception:
        return False
    if mtime < _boot_time():
        return False
    return (time.time() - mtime) < UPDATING_MAX_SEC

_idevice_id_warned = False

def get_connected_udids():
    """Return list of currently connected iPhone UDIDs.

    Every probe failure (tool missing, hung, unstartable) reads as an empty
    list — callers treat it as no-device and keep polling — but each failure
    mode is logged distinctly, once, so 'idevice_id is broken' is never
    mistaken for 'no phone plugged in'."""
    global _idevice_id_warned
    try:
        out = subprocess.run(["idevice_id", "-l"], capture_output=True, text=True,
                             timeout=10).stdout.strip()
        if out:
            return [u.strip() for u in out.splitlines() if u.strip()]
    except FileNotFoundError:
        if not _idevice_id_warned:
            _idevice_id_warned = True
            print("[WARN] idevice_id not found (libimobiledevice not installed?)", flush=True)
    except subprocess.TimeoutExpired:
        if not _idevice_id_warned:
            _idevice_id_warned = True
            print("[WARN] idevice_id -l timed out; treating as no device", flush=True)
    except OSError as e:
        if not _idevice_id_warned:
            _idevice_id_warned = True
            print(f"[WARN] idevice_id -l failed: {e}", flush=True)
    return []

def device_present():
    return bool(get_connected_udids())


def _apple_usb_signature():
    """Identity of the Apple USB device(s) the kernel has enumerated (vendor
    05ac), read straight from sysfs so it doesn't depend on usbmux — which is
    exactly what may be stuck. The signature folds in fields that change when the
    phone re-enumerates (a fresh plug, or iOS bringing the data interface up when
    you unlock a phone that was in USB Restricted Mode), so those transitions are
    detected as a change. Empty string means no Apple device is plugged."""
    sigs = []
    try:
        for vid_path in glob.glob("/sys/bus/usb/devices/*/idVendor"):
            try:
                with open(vid_path) as f:
                    if f.read().strip().lower() != "05ac":
                        continue
            except Exception:
                continue
            dev = os.path.dirname(vid_path)
            parts = [os.path.basename(dev)]
            for attr in ("devnum", "bNumInterfaces", "bConfigurationValue"):
                try:
                    with open(os.path.join(dev, attr)) as f:
                        parts.append(f.read().strip())
                except Exception:
                    parts.append("?")
            sigs.append(":".join(parts))
    except Exception:
        pass
    return ",".join(sorted(sigs))


_last_usbmux_refresh = 0.0
_last_apple_sig = None
_usbmux_backoff = 0.0
USBMUX_BACKOFF_MIN = 8      # first retry gap for a device that stays invisible
USBMUX_BACKOFF_MAX = 120    # cap, so a phone left plugged+locked doesn't spam restarts/logs

def _maybe_refresh_usbmux(logf=None, udids_seen=None):
    """Work around the usbmuxd libusb-hotplug gap on this ARM image: usbmuxd runs
    persistently but doesn't get hotplug events, so an iPhone plugged after boot
    stays invisible to idevice_* (idevice_id -l empty) until usbmuxd is restarted.

    Restart usbmuxd so the device becomes visible whenever the kernel sees an
    Apple device but usbmux does not. Handles every plug/lock order:
      - a fresh plug, or a re-enumeration (iOS bringing the data interface up when
        you unlock a phone that was in USB Restricted Mode) changes the sysfs
        signature -> restart immediately and reset the backoff.
      - a device that stays invisible (still locked/restricted) is retried with
        exponential backoff (8s, 16s, ... capped at 120s), so unlocking it later
        is picked up quickly without a restart/log storm if it's left locked.
    Once the device is visible we stop; never restarts during a backup (that
    would drop idevicebackup2's usbmux session).

    Pass udids_seen (from a device_allowed() call) to avoid a redundant
    idevice_id -l spawn; omit it and this probes usbmux itself."""
    global _last_usbmux_refresh, _last_apple_sig, _usbmux_backoff
    if _backup_running:
        return
    if udids_seen is None:
        udids_seen = get_connected_udids()
    if udids_seen:
        return   # usbmux already sees a device -> nothing to do
    sig = _apple_usb_signature()
    if not sig:
        _last_apple_sig = None    # no Apple device -> the empty list is correct
        return
    now = time.time()
    changed = (sig != _last_apple_sig)
    if changed:
        _usbmux_backoff = USBMUX_BACKOFF_MIN
    elif (now - _last_usbmux_refresh) < _usbmux_backoff:
        return   # same stuck device, still backing off
    else:
        _usbmux_backoff = min(_usbmux_backoff * 2, USBMUX_BACKOFF_MAX)
    _last_apple_sig = sig
    _last_usbmux_refresh = now
    if logf:
        logf.write("[USBMUX] Apple device in sysfs but idevice_id -l empty "
                   f"({'new/changed' if changed else 'retry'}); restarting usbmuxd\n")
        logf.flush()
    try:
        subprocess.run(["systemctl", "restart", "usbmuxd"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    except Exception:
        pass


def _sync_running():
    """True if a remote sync (backup-sync.py) is active — used to keep a backup
    and a sync mutually exclusive (never run both / show both)."""
    try:
        return subprocess.run(["pgrep", "-f", "backup-sync.py"],
                              capture_output=True, timeout=5).returncode == 0
    except Exception:
        return False

def _manual_start_requested():
    """True if a fresh 'start backup' request (web UI / double-tap) is pending.

    The sentinel only counts for 15s so it can't silently fire a backup minutes
    later when a phone is next plugged in. Peeks only — the main loop consumes
    the file when it acts on it."""
    try:
        return (os.path.exists(START_FILE)
                and time.time() - os.path.getmtime(START_FILE) <= 15)
    except Exception:
        return False


def device_allowed():
    """
    Check whether a connected device is allowed to trigger backup.
    Returns (allowed: bool, udid: str or None, reason: str).
    """
    udids = get_connected_udids()
    if not udids:
        return False, None, "no_device"

    # Re-read config each time so web UI changes take effect immediately
    try:
        with open(CONFIG_PATH, "r") as f:
            live_cfg = yaml.safe_load(f) or {}
    except Exception:
        live_cfg = {}

    # Check auto_start
    bk = live_cfg.get("backup", {})
    if not bk.get("auto_start", True):
        return False, udids[0], "auto_start_disabled"

    # Check device filter
    df = live_cfg.get("device_filter", {})
    if df.get("enabled", False):
        allowed_udids = [d.get("udid", "") for d in df.get("allowed_devices", [])]
        for u in udids:
            if u in allowed_udids:
                return True, u, "allowed"
        # No connected device is in the allowed list
        return False, udids[0], "device_rejected"

    # Filter disabled - all devices allowed
    return True, udids[0], "allowed"

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
    logutil.prune_logs()   # trim old per-run logs (count + age)
    return f, path

def _backup_dir_writable(path):
    """Probe-write a temp file and delete it. A backup dir that is read-only
    (failed disk remounted ro, wrong mount options) must fail the pre-backup
    gate here instead of crashing idevicebackup2 mid-run."""
    try:
        fd, tmp = tempfile.mkstemp(prefix=".wprobe-", dir=path)
        try:
            os.write(fd, b"0")
        finally:
            os.close(fd)
        os.remove(tmp)
        return True
    except OSError:
        return False

def check_backup_mount(logf, ui):
    """Return True if the backup folder is mounted and writable; else show an
    error via the Animator and return False. The daemon stays alive (single EPD
    owner)."""
    ensure_dir(CFG["backup_dir"])
    marker = os.path.join(CFG["backup_dir"], CFG["marker_file"])
    if not os.path.exists(marker):
        msg = "Backup folder not found or not mounted"
        print(f"[ERROR] {msg} (missing {marker})")
        if logf: logf.write(f"[ERROR] {msg} (missing {marker})\n")
        write_status("error", message=msg)
        ui.set(screen="normal", subtitle="Backup folder not found.", percent=None,
               animate=False, show_header=True)
        return False
    if not _backup_dir_writable(CFG["backup_dir"]):
        msg = "Backup storage read-only / not writable"
        print(f"[ERROR] {msg} ({CFG['backup_dir']})")
        if logf: logf.write(f"[ERROR] {msg} ({CFG['backup_dir']})\n")
        write_status("error", message=msg)
        ui.set(screen="normal", subtitle="Backup storage is\nread-only.", percent=None,
               animate=False, show_header=True)
        return False
    return True

ROOT_MIN_FREE_MB = 200
BACKUP_MIN_FREE_MB = 500

def _disk_space_problems(backup_dir, statvfs_fn=None):
    """Free-space shortfalls that should block a backup from starting. An
    unreadable filesystem fails open (empty list): statvfs lying is rarer than
    the backup actually needing the space."""
    if statvfs_fn is None:
        statvfs_fn = getattr(os, "statvfs", None)   # absent on non-POSIX dev machines
    if statvfs_fn is None:
        return []
    problems = []
    try:
        st = statvfs_fn("/")
        root_free_mb = (st.f_bavail * st.f_frsize) // (1024 * 1024)
        if root_free_mb < ROOT_MIN_FREE_MB:
            problems.append(f"Root disk: {root_free_mb}MB free (need {ROOT_MIN_FREE_MB}MB)")
    except Exception:
        pass
    try:
        st = statvfs_fn(backup_dir)
        backup_free_mb = (st.f_bavail * st.f_frsize) // (1024 * 1024)
        if backup_free_mb < BACKUP_MIN_FREE_MB:
            problems.append(f"Backup drive: {backup_free_mb}MB free (need {BACKUP_MIN_FREE_MB}MB)")
    except Exception:
        pass
    return problems

def check_disk_space(logf, ui):
    """Pre-backup disk-space gate. Returns False (after explaining on the
    display/status) when there is too little room to start; a backup that runs
    into ENOSPC mid-way fails as a corrupt backup, not a clean error."""
    problems = _disk_space_problems(CFG.get("backup_dir", "/media/iosbackup/"))
    if not problems:
        return True
    msg = "Not enough disk space.\n" + "\n".join(problems)
    print(f"[ERROR] {msg}", flush=True)
    if logf: logf.write(f"[ERROR] Disk space gate: {msg}\n")
    write_status("error", message=msg)
    ui.set(screen="normal", subtitle=msg, percent=None, animate=False, show_header=True)
    time.sleep(4)
    return False

def verify_backup_integrity(backup_dir, logf):
    """Check backup completed with valid Manifest.plist."""
    try:
        # Find the most recently modified backup folder
        entries = []
        for e in os.scandir(backup_dir):
            if e.is_dir(follow_symlinks=True):
                try:
                    entries.append((e.stat().st_mtime, e.path))
                except Exception:
                    pass
        if not entries:
            return False, "No backup folders found"
        entries.sort(reverse=True)
        latest = entries[0][1]
        manifest = os.path.join(latest, "Manifest.plist")
        if not os.path.exists(manifest):
            return False, "Manifest.plist missing"
        # Check it's parseable
        import plistlib
        with open(manifest, "rb") as f:
            plistlib.load(f)
        return True, "OK"
    except Exception as e:
        return False, str(e)

def _is_progress_line(ln):
    """Check if a line is a progress bar (e.g. '[====] 42% Finished')."""
    return bool(re.match(r'\s*\[=*\s*\]\s*\d+%', ln))

def _backup_watchdog_limits():
    """Silence timeout / total cap for a backup run. Configurable under the
    `backup:` section (hang_timeout_sec, max_duration_sec); read defensively so
    a config schema that doesn't know those keys yet can't break us."""
    bk = CFG.get("backup", {})
    if not isinstance(bk, dict):
        bk = {}
    try: silence = int(bk.get("hang_timeout_sec", BACKUP_SILENCE_TIMEOUT_SEC))
    except (TypeError, ValueError): silence = BACKUP_SILENCE_TIMEOUT_SEC
    try: cap = int(bk.get("max_duration_sec", BACKUP_MAX_DURATION_SEC))
    except (TypeError, ValueError): cap = BACKUP_MAX_DURATION_SEC
    return max(30, silence), max(300, cap)

def _terminate_then_kill(proc, logf, why):
    print(f"[WATCHDOG] {why}; terminating idevicebackup2", flush=True)
    if logf: logf.write(f"[WATCHDOG] {why}; terminating idevicebackup2\n")
    try: proc.terminate()
    except Exception: return
    try:
        proc.wait(timeout=5)
        return
    except Exception:
        pass
    try: proc.kill()
    except Exception: pass
    try: proc.wait(timeout=5)
    except Exception: pass

def tee_and_parse(proc, logf, on_line, silence_timeout=BACKUP_SILENCE_TIMEOUT_SEC,
                  total_cap=BACKUP_MAX_DURATION_SEC, device_gone=None, device_poll_sec=30):
    """Pump idevicebackup2's merged stdout/stderr to the journal, the run log and
    the parser (char-at-a-time; the parser tracks progress-bar rewrites via \\r).

    Watchdogs, mirroring sync_manager's SCAN_KILL_SEC/STALL_KILL_SEC pattern:
    `silence_timeout` seconds without ANY output means the backup hung (device
    still attached, nothing progressing) and a run past `total_cap` is bounded;
    both terminate, then SIGKILL 5s later. `device_gone` (polled every
    device_poll_sec) is the third unplug layer: the phone vanished but the
    process is still alive. Returns "eof" | "silent" | "overtime" | "unplugged"
    so the caller can report why the stream ended."""
    fd = proc.stdout.fileno()
    cur_line = ""
    start = time.monotonic()
    last_output = start
    last_dev_poll = start
    outcome = "eof"
    reasons = {
        "silent": f"no output for {silence_timeout}s",
        "overtime": f"backup exceeded {total_cap}s",
        "unplugged": "device vanished mid-backup but process still running",
    }
    while True:
        now = time.monotonic()
        if now - last_output > silence_timeout:
            outcome = "silent"; break
        if now - start > total_cap:
            outcome = "overtime"; break
        if device_gone is not None and now - last_dev_poll >= device_poll_sec:
            last_dev_poll = now
            try:
                gone = device_gone()
            except Exception:
                gone = False
            if gone and proc.poll() is None:
                outcome = "unplugged"; break
        wait = max(0.05, min(1.0, silence_timeout - (now - last_output)))
        try:
            ready, _, _ = select.select([fd], [], [], wait)
        except (OSError, ValueError):
            break
        if not ready:
            continue
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        last_output = time.monotonic()
        _heartbeat()
        text = chunk.decode("utf-8", "replace")
        sys.stdout.write(text); sys.stdout.flush()
        for ch in text:
            if ch in ("\n", "\r"):
                # Only log non-progress lines (progress bars are noise)
                if cur_line and logf and not _is_progress_line(cur_line):
                    logf.write(cur_line + "\n")
                on_line("__LINE_BREAK__")
                cur_line = ""
            else:
                cur_line += ch
                on_line(ch)
    # Flush remaining
    if cur_line and logf and not _is_progress_line(cur_line):
        logf.write(cur_line + "\n")
    if outcome != "eof":
        _terminate_then_kill(proc, logf, reasons[outcome])
    return outcome

def get_disk_usage_pct(device_path: str):
    try:
        out = subprocess.run(["df", "-P", device_path], capture_output=True, text=True, check=True, timeout=5).stdout.splitlines()
        if len(out) >= 2:
            tokens = out[-1].split()
            for tok in tokens:
                if tok.endswith("%") and tok[:-1].isdigit():
                    return int(tok[:-1])
    except Exception as e:
        print(f"[WARN] df failed for {device_path}: {e}")
    return None

def _check_encryption(logf, ui):
    """Check if backup encryption is enabled on the connected device.
    Retries once to avoid false negatives from timing issues.
    Warns on the e-ink only if confirmed disabled, but proceeds anyway.
    """
    backup_dir = CFG.get("backup_dir", "/media/iosbackup/")
    for attempt in range(2):
        try:
            r = subprocess.run(
                ["idevicebackup2", "-i", "encryption", backup_dir],
                capture_output=True, text=True, timeout=10
            )
            out = (r.stdout + r.stderr).lower()
            if "enabled" in out:
                if logf: logf.write("[ENC] Encryption is enabled on device.\n")
                try:
                    with open(CONFIG_PATH, "r") as f:
                        live = yaml.safe_load(f) or {}
                    enc = live.get("backup_encryption", {})
                    if not enc.get("encryption_confirmed", False):
                        enc["encryption_confirmed"] = True
                        live["backup_encryption"] = enc
                        # Atomic write so a power loss mid-save can't truncate config.yaml.
                        if _config_schema:
                            _config_schema.atomic_save(live, CONFIG_PATH)
                        else:
                            with open(CONFIG_PATH, "w") as f:
                                yaml.dump(live, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
                except Exception:
                    pass
                return True
            elif "disabled" in out or "not encrypted" in out:
                if attempt == 0:
                    time.sleep(2)
                    continue  # retry once
                if logf: logf.write("[ENC] WARNING: Encryption is NOT enabled. Backup will be unencrypted.\n")
                ui.set(subtitle="Warning: encryption OFF.\nEnable via web UI.\nProceeding unencrypted...",
                       percent=None, animate=False, show_header=True)
                time.sleep(4)
                return False
            else:
                # Ambiguous output — don't warn, just proceed
                if logf: logf.write(f"[ENC] Could not determine encryption status, proceeding.\n")
                return None
        except Exception as e:
            if logf: logf.write(f"[ENC] Could not check encryption status: {e}\n")
            return None

def run_backup(panel, logf, ui, _retry=0):
    global _backup_proc
    if _retry == 0:
        # Fresh backup — clear any stale stop request from a previous run.
        try:
            if os.path.exists(STOP_FILE):
                os.remove(STOP_FILE)
        except Exception:
            pass
    if not check_backup_mount(logf, ui):
        return 2
    if not check_disk_space(logf, ui):
        return 2
    if shutil.which("idevicebackup2") is None:
        msg = "idevicebackup2 not installed"
        print(f"[ERROR] {msg}", flush=True)
        if logf: logf.write(f"[ERROR] {msg}\n")
        write_status("error", message=msg)
        ui.set(screen="normal", subtitle=f"Error:\n{msg}", percent=None,
               animate=False, show_header=True)
        return 2
    _check_encryption(logf, ui)
    cmd = ["idevicebackup2", "backup", CFG["backup_dir"]]
    print(f"[CMD] {' '.join(cmd)}", flush=True)
    if logf: logf.write(f"[CMD] {' '.join(cmd)}\n")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=0, universal_newlines=True)
    _backup_proc = proc
    pct, encrypted, last_ui = None, False, 0
    last_pct = None
    cur_line = ""

    ui.set(
        subtitle="Welcome.\nEnter device password when prompted.\nBackup will start soon.",
        percent=None, animate=True, show_header=True
    )

    def error_and_wait(user_msg, code=None, tail=None):
        """Show the error, then hold the screen until there is a reason to let go.

        Holding until the iPhone is unplugged is what keeps the error readable.
        But this used to wait on the cable and nothing else, which made the daemon
        deaf for as long as the phone stayed in: a "Start Backup" from the web UI
        wrote its sentinel, the UI reported the request accepted, and nothing
        happened — then the sentinel was still inside its 15s window when the user
        unplugged and replugged, so the backup fired on the plug instead, looking
        like an unwanted auto-start. One deaf wait, two symptoms.

        It now releases on the same three conditions the post-backup hold uses:
        the phone leaving, a fresh manual request, or shutdown.
        Does NOT exit — lets the unplug handler show its screen."""
        # Enhance disk space errors with more context
        if code in (105, 106):
            user_msg = "Not enough disk space.\nCheck root filesystem (df -h)."
        elif code == 102:
            user_msg = "iPhone is out of space.\nFree space on iPhone\nand retry."
        # Truncate long messages for the small display
        lines = user_msg.split("\n")
        truncated = []
        for ln in lines:
            if len(ln) > 30:
                truncated.append(ln[:27] + "...")
            else:
                truncated.append(ln)
        display_msg = "Error:\n" + "\n".join(truncated)

        write_status("error", message=user_msg, code=code)
        ui.set(subtitle=display_msg, percent=pct if pct is not None else 0,
               animate=False, show_header=True)
        if logf: logf.write(f"[ERROR] {user_msg} code={code} tail='{tail or ''}'\n")
        send_notification("backup_error", {"error": user_msg, "code": code})

        # Hold the error on screen until the phone leaves, the user asks for
        # another backup, or we are shutting down. Same policy as the post-backup
        # hold, and deliberately the same function: keeping one of these two waits
        # in step with the other by hand is what let them drift apart. A sync
        # cannot be running here, since a backup is what got us to this line.
        print("[ERROR] Holding error screen (unplug, new request, or shutdown)...", flush=True)
        while not SHUTDOWN.is_set():
            _heartbeat()
            if uipolicy.should_release_hold(device_present=device_present(),
                                            sync_running=False,
                                            manual_start=_manual_start_requested()):
                break
            time.sleep(1)

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
                    write_status("backing_up", percent=pct, encrypted=encrypted)
                    last_pct = pct
                    last_ui = time.time()

            # Only treat as error if we can extract a known error code.
            # Avoids false positives from lines that mention "error" in passing.
            code = extract_error_code(ln)
            if code is not None:
                msg = resolve_error_message(code)
                error_and_wait(msg, code, ln[-80:])
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

    write_status("backing_up", percent=0)
    # Resolve and cache the webhook auth header NOW, while the phone is attached.
    # It is decrypted with the phone's serial, and every later notification -
    # backup_complete, and the whole auto-sync - fires after the e-ink has said
    # "Backup completed" and the user has unplugged.
    try:
        import notifications as _notif
        if logf:
            _notif.set_logger(lambda m: logf.write(m + "\n"))
        _notif.prime_auth()
    except Exception as _e:
        if logf: logf.write(f"[NOTIFY] could not prime webhook auth: {_e}\n")
    send_notification("backup_start")
    silence_sec, cap_sec = _backup_watchdog_limits()
    outcome = tee_and_parse(proc, logf, feed_parser, silence_timeout=silence_sec,
                            total_cap=cap_sec,
                            device_gone=lambda: not device_present())
    proc.wait(); rc = proc.returncode
    ts_end = datetime.now().strftime("%H:%M / %d %b %Y")
    if outcome in ("silent", "overtime"):
        # Watchdog killed a hung/runaway backup; the phone may still be plugged,
        # so the device_present() check below can't catch this — report it here,
        # through the same interrupted flow as a mid-backup unplug.
        reason_txt = ("Backup hung (no progress)" if outcome == "silent"
                      else "Backup exceeded time limit")
        if logf: logf.write(f"[INTERRUPT] {reason_txt}; watchdog killed idevicebackup2\n")
        write_status("interrupted", reason=reason_txt)
        ui.set(screen="interrupted", subtitle=ts_end, percent=None, animate=False)
        send_notification("backup_error", {"error": reason_txt})
        return 0
    # outcome "unplugged": rc!=0 with the device gone — the interrupted branch
    # below reports it through the existing flow.
    if rc == 0:
        # Verify backup integrity
        ok, integrity_msg = verify_backup_integrity(CFG["backup_dir"], logf)
        if not ok:
            if logf: logf.write(f"[WARN] Backup integrity check: {integrity_msg}\n")

        usage = get_disk_usage_pct(CFG["disk_device"])
        usage_str = f"{usage}%" if usage is not None else "n/a"
        owner = CFG["owner_lines"]
        center = (
            f"Backup completed at {ts_end}.\n"
            f"{usage_str} memory usage.\n"
            f"  \n"
            f"{owner[0]}\n{owner[1]}\n{owner[2]}\n{owner[3]}"
        )
        write_status("complete", usage=usage_str, completed_at=ts_end, verified=ok)
        ui.set(screen="complete", subtitle="", percent=None, animate=False,
               center_block=center, show_header=False)
        ui.request_full()   # clean transition from backup progress
        if logf: logf.write(f"[OK] completed at {ts_end} usage={usage_str}\n")
        send_notification("backup_complete", {
            "usage": usage_str, "timestamp": ts_end,
            "device": CFG.get("owner_lines", [""])[0],
            "verified": ok,
        })

        # Auto-sync decision. Claim the sync slot (status=syncing) BEFORE the
        # "Backup completed" pause, so a sync triggered during it (web Sync Now /
        # long-press, both of which refuse when status==syncing) can't race the
        # auto-sync — backup and sync stay mutually exclusive.
        sync_cfg = CFG.get("sync", {})
        do_autosync = bool(sync_cfg.get("enabled") and sync_cfg.get("auto_sync") and _sync_manager)
        if do_autosync:
            # Power-aware: skip auto-sync on low battery (unless charging).
            try:
                import power as _power
                _ok, _reason = _power.sync_allowed(sync_cfg.get("min_battery_percent", 35))
            except Exception:
                _ok, _reason = True, ""
            if not _ok:
                _failure = _sync_manager.build_failure("battery_low", detail=_reason)
                if logf: logf.write(f"[SYNC] Skipped auto-sync: {_reason}\n")
                write_status("sync_error", message=_failure["message"],
                             reason_code=_failure["reason_code"])
                send_notification("sync_error",
                                  {"error": _failure["message"],
                                   **_sync_manager.notification_payload(_failure)})
                do_autosync = False
            else:
                write_status("syncing", percent=0)   # claim the slot now

        time.sleep(2)   # let the user see "Backup completed"

        if do_autosync:
            # Auto-sync logs to its own sync-*.log (consistent with a manual sync),
            # not the backup log; the backup log just gets a pointer.
            sync_ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            sync_logpath = os.path.join(LOG_DIR, f"sync-{sync_ts}.log")
            try:
                synclogf = logutil.open_run_log(sync_logpath)
                synclogf.write("auto-sync after backup\n")
                logutil.prune_logs()   # trim old per-run logs (count + age)
            except Exception:
                synclogf = None
            if logf: logf.write(f"[SYNC] Auto-sync started; see {os.path.basename(sync_logpath)}\n")

            send_notification("sync_start")
            ui.set(screen="normal", subtitle="Syncing to remote server...", percent=0,
                   animate=True, show_header=True)
            ui.request_full()   # clear the backup-complete screen before sync progress

            # The sync log is written by sync_manager's SyncLogWriter; this
            # callback only drives the e-ink and the status file, so the auto-sync
            # and the manual sync can no longer log different things.
            def _sync_progress(info):
                pct = info["pct"]
                if info.get("total"):
                    sub = f"{fmt_bytes(info['bytes'])} / {fmt_bytes(info['total'])} | {info['speed']}"
                else:
                    sub = f"{fmt_bytes(info['bytes'])} | {info['speed']}"
                ui.set(subtitle=sub, percent=pct, animate=True, show_header=True)
                write_status("syncing", percent=pct,
                             bytes=info.get("bytes", 0),
                             total=info.get("total", 0),
                             speed=info.get("speed", ""),
                             eta_seconds=info.get("eta_seconds"))

            try:
                result = _sync_manager.run_sync_with_progress(
                    backup_dir=CFG.get("backup_dir"), on_progress=_sync_progress,
                    log_file=synclogf)
                if result["success"]:
                    if synclogf: synclogf.write(f"[OK] {result['message']}\n")
                    write_status("sync_complete", message=result["message"])
                    ui.set(screen="complete", subtitle="", percent=None, animate=False,
                           center_block=f"Sync complete.\n{result['message']}", show_header=True)
                    ui.request_full()
                    send_notification("sync_complete", {"message": result["message"]})
                else:
                    # The structured failure carries the reason code, exit status,
                    # how far the transfer got and the post-mortem findings. It
                    # goes to MQTT / webhook whole, so an automation can act on
                    # reason_code rather than matching an English sentence.
                    failure = result.get("failure") or {
                        "message": result["message"], "reason_code": "internal_error"}
                    if synclogf and not failure.get("logged"):
                        synclogf.write(f"[ERROR] {failure['message']}\n")
                    write_status("sync_error", message=failure["message"],
                                 reason_code=failure.get("reason_code"))
                    ui.set(screen="complete", subtitle="", percent=None, animate=False,
                           center_block=f"Sync failed.\n{failure['message'][:60]}", show_header=True)
                    ui.request_full()
                    send_notification("sync_error",
                                      {"error": failure["message"],
                                       **_sync_manager.notification_payload(failure)})
                time.sleep(5)
            except Exception as e:
                failure = _sync_manager.build_failure(
                    "internal_error",
                    detail=f"the sync runner raised {type(e).__name__}: {e}")
                if synclogf: synclogf.write(f"[ERROR] {failure['message']}\n")
                write_status("sync_error", message=failure["message"],
                             reason_code=failure["reason_code"])
                send_notification("sync_error",
                                  {"error": failure["message"],
                                   **_sync_manager.notification_payload(failure)})
            finally:
                if synclogf:
                    try: synclogf.close()
                    except Exception: pass

        return 0
    else:
        # Interrupted (web-UI Stop) or iPhone unplugged mid-backup: show the
        # interrupted screen, not a generic error, and don't retry.
        stop_req = False
        try:
            if os.path.exists(STOP_FILE):
                os.remove(STOP_FILE); stop_req = True
        except Exception:
            pass
        if stop_req or not device_present():
            reason_txt = "Stopped from web UI" if stop_req else "iPhone unplugged"
            if logf: logf.write(f"[INTERRUPT] {reason_txt}\n")
            write_status("interrupted", reason=reason_txt)
            ui.set(screen="interrupted", subtitle=ts_end, percent=None, animate=False)
            if not stop_req:
                send_notification("device_disconnected", {"timestamp": ts_end})
            return 0
        # Retry once on failure
        if _retry == 0:
            if logf: logf.write("[RETRY] Backup failed, retrying once...\n")
            ui.set(screen="normal", subtitle="Backup failed.\nRetrying...", percent=None,
                   animate=True, show_header=True)
            time.sleep(3)
            return run_backup(panel, logf, ui, _retry=1)
        send_notification("backup_error", {"error": "Unknown error, rc!=0"})
        error_and_wait("Unknown error.\nCheck logs.", None, "rc!=0")

# ---------------------------------------------------------------------------
# PiSugar button listener (single-tap → system-info screen for 30s)
# ---------------------------------------------------------------------------
_backup_running = False
# Live idevicebackup2 process (or None) — _do_shutdown terminates it so a
# daemon exit can't orphan a running backup.
_backup_proc = None
# The single authority on whether the single-tap info screen is up. Enforced in
# Animator.set(), so no drawing path has to remember to check it.
_info_window = uipolicy.InfoWindow()


def _now():
    """Clock for the info-screen window. One indirection so its timing can be
    driven deterministically in tests instead of with sleeps."""
    return time.time()

def _pisugar_button_listener(ui):
    """Show the system-info screen on a PiSugar single-tap.

    PiSugar signals the tap by running single_tap_shell, which touches INFO_FILE
    (the same flag-file mechanism the double-tap uses for start_requested). We
    watch for that flag here instead of polling the pisugar socket: pisugar-server
    has no 'get button_press' query — it answers 'Invalid request.' — so the old
    socket poll never fired.

    The tap always wins, whatever the device is doing. It used to be dropped
    outright while a backup or sync was running (and the flag was consumed
    anyway, so the tap vanished), and even when allowed the next sync tick
    repainted over it within half a second — either way the button looked dead.
    The screen that was up beforehand is restored when the window closes."""
    while True:
        try:
            _handle_info_tap(ui)
        except Exception:
            pass
        time.sleep(0.5)


def _handle_info_tap(ui):
    """One poll of the single-tap info screen.

    Raises it on a fresh tap, and puts the previous screen back once the window
    has closed — actively, rather than waiting for a passive redraw that may
    never come (during a sync, or while a post-backup result is being held)."""
    now = _now()
    if os.path.exists(INFO_FILE):
        try:
            os.remove(INFO_FILE)   # consume the request
        except Exception:
            pass
        _info_window.open(now, ui.get_state())
        ui.set(_force=True, screen="info", info_lines=build_button_info_lines(),
               percent=None, animate=False, show_header=False)
        ui.request_full()
    prev = _info_window.take_restore(now)
    if prev is not None:
        ui.resume(prev)


def _status_icon_updater(ui):
    """Refresh the connectivity status behind the on-screen icons every few
    seconds, off the draw thread (have_connectivity blocks on a socket test).
    On a change, force one refresh so even a static screen (idle) updates."""
    prev = None
    while not SHUTDOWN.is_set():
        st = _compute_icon_status()
        with _icon_status_lock:
            _icon_status.update(st)
        # Repaint whenever the sampled state differs from what was last painted,
        # INCLUDING the first sample (prev is None). Without the first-sample
        # paint, a daemon (re)started while already connected keeps the boot
        # screen's default all-off icons: every later sample matches the first,
        # so no change is ever detected and the status bar never repaints.
        # A full refresh flashes the panel. During an update the icons churn as
        # the network drops, and the "Updating" screen would strobe for no gain,
        # so leave it painted exactly once.
        if st != prev and not _updating_requested():
            try:
                ui.request_full()
            except Exception:
                pass
        prev = st
        if SHUTDOWN.wait(5):
            return

# ---------------------------------------------------------------------------
# WireGuard auto-connect reconciler
# ---------------------------------------------------------------------------
def _wg_should_connect_for(triggers):
    """Return True if a configured auto-connect trigger source is currently
    reachable. 'wifi' -> a WiFi IP is present; 'iphone' -> a USB iPhone-hotspot
    IP is present; 'boot' keeps its historical "any connectivity" meaning so a
    boot-only user still reconnects when a link returns. An empty trigger list
    means nothing auto-connects."""
    if netutil is None:
        return False
    wifi = netutil.get_wifi_ip()
    usb = netutil.get_usb_iphone_ip()
    if "boot" in triggers and (wifi or usb):
        return True
    if "wifi" in triggers and wifi:
        return True
    if "iphone" in triggers and usb:
        return True
    return False


def _wg_autoconnect_watcher(logf):
    """Background reconciler: while auto-connect is enabled and WireGuard is
    down, bring it up as soon as a selected connection source becomes available.

    This is the reliable backstop for the event-driven paths (boot service +
    NetworkManager dispatcher), which fire only on a connection 'up' event and
    so miss the case the user hit: enabling the iPhone hotspot while the phone
    is already plugged in emits no fresh 'up' event for the already-enumerated
    USB interface (and on a non-NetworkManager image the dispatcher isn't even
    installed). Polling every WG_RECONCILE_SEC catches connectivity whenever and
    however it appears. Config is re-read each tick so web-UI changes apply live."""
    import wg_manager as _wg
    last_err = None       # dedupe repeated failure logs across a streak of attempts
    dead_since = None     # first time we saw the interface up but not yet handshaked
    while not SHUTDOWN.is_set():
        try:
            try:
                with open(CONFIG_PATH, "r") as f:
                    live = yaml.safe_load(f) or {}
            except Exception:
                live = {}
            wg_cfg = live.get("wireguard", {})
            iface = wg_cfg.get("interface_name", "wg0")
            if not (wg_cfg.get("enabled") and wg_cfg.get("auto_connect")):
                last_err = None
                dead_since = None
            elif _wg.is_interface_up(iface):
                # 'Interface exists' is not 'connected': a tunnel can be up but
                # never handshake (endpoint unreachable, or a pre-NTP wrong clock
                # the server rejects). Verify a handshake actually completed; if
                # not within the grace window, tear it down so the next tick
                # reconnects cleanly. A tunnel that handshaked at least once is
                # left alone (its handshake time stays set even when idle).
                ts = _wg.latest_handshake(iface)
                if ts is None:
                    # Could not read handshake state (wg missing/error/timeout):
                    # leave the tunnel alone — cycling it would not fix the probe.
                    dead_since = None
                elif ts > 0:
                    last_err = None
                    dead_since = None
                else:
                    if dead_since is None:
                        dead_since = time.time()
                    elif time.time() - dead_since > WG_HANDSHAKE_GRACE_SEC:
                        if logf: logf.write(f"[WG] {iface} up but no handshake after "
                                            f"{WG_HANDSHAKE_GRACE_SEC}s — reconnecting\n")
                        _wg.stop_wireguard(iface)
                        dead_since = None
            elif _wg_should_connect_for(wg_cfg.get("auto_connect_on", ["iphone"])):
                dead_since = None
                ok, err = _wg.start_wireguard(iface)
                if ok:
                    last_err = None
                    if logf: logf.write(f"[WG] Auto-connected {iface} (connectivity available)\n")
                elif err != last_err:
                    last_err = err
                    if logf: logf.write(f"[WG] Auto-connect attempt failed: {err}\n")
        except Exception as e:
            if logf: logf.write(f"[WG] Watcher error: {e}\n")
        if SHUTDOWN.wait(WG_RECONCILE_SEC):
            return


# ---------------------------------------------------------------------------
# NTP sync attempt at startup
# ---------------------------------------------------------------------------
def _try_ntp_sync():
    """Attempt NTP sync if configured and internet is available."""
    try:
        ntp_cfg = CFG.get("ntp", {})
        if not ntp_cfg.get("enabled", True):
            return
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ntp-sync.py")
        if os.path.exists(script):
            subprocess.Popen(
                [sys.executable, script],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env={**os.environ, "IOSBACKUP_CONFIG": CONFIG_PATH}
            )
    except Exception:
        pass

def _request_shutdown(signum, frame):
    """SIGTERM/SIGINT handler — the Animator thread paints the owner screen and exits."""
    SHUTDOWN.set()


def _setup_completed():
    try:
        with open(CONFIG_PATH, "r") as f:
            return bool((yaml.safe_load(f) or {}).get("setup_completed", False))
    except Exception:
        return False


def main():
    global _backup_running, _loop_watchdog

    # Single EPD owner: on shutdown the Animator paints the owner screen and sleeps
    # the panel so the image persists after PiSugar cuts power.
    signal.signal(signal.SIGTERM, _request_shutdown)
    try:
        signal.signal(signal.SIGINT, _request_shutdown)
    except Exception:
        pass

    try:
        logf, logpath = log_open()
        print(f"[LOG] writing to {logpath}")
        if logf: logf.write(f"[LOG] writing to {logpath}\n")
    except OSError as e:
        # A broken log dir must not stop the daemon; journal stdout still works.
        logf = None
        print(f"[WARN] could not open run log ({e}); logging to journal only", flush=True)

    # Try NTP sync in background
    _try_ntp_sync()

    # WireGuard auto-connect now runs in a background reconciler thread (started
    # below) instead of a one-shot attempt here, so the VPN comes up whenever a
    # connection appears — including the iPhone hotspot being toggled on after
    # the phone is already plugged in, not just at boot.

    try:
        p = Panel()
        p.prepare_partial()  # enable partial for text screens
    except Exception as e:
        # No display (panel missing, SPI/GPIO wedged, stuck BUSY): run headless.
        # Backups, sync and notifications all still work without the EPD.
        print(f"[WARN] display init failed ({e}); running headless", flush=True)
        p = NullPanel()

    ui = Animator(p)
    ui.start()
    # Boot / idle screen immediately (folded in from boot-message.py).
    ui.set(screen="boot", subtitle="", percent=None, animate=False, show_header=True)

    # Single-tap → system-info screen (folded in from button-info.py).
    btn_thread = threading.Thread(target=_pisugar_button_listener, args=(ui,), daemon=True)
    btn_thread.start()

    # WireGuard auto-connect reconciler: brings the VPN up as soon as a selected
    # connection source is available, and re-checks every WG_RECONCILE_SEC.
    wg_thread = threading.Thread(target=_wg_autoconnect_watcher, args=(logf,), daemon=True)
    wg_thread.start()

    # Status-icon updater: samples VPN/internet/WiFi/iPhone for the on-screen icons.
    status_thread = threading.Thread(target=_status_icon_updater, args=(ui,), daemon=True)
    status_thread.start()

    _last_reject_udid = None
    _sync_dead_logged = False     # so we don't spam the log
    _prev_state = None            # for full-refresh on status transitions
    try:
        # Don't clobber an in-progress sync's status if one is already running
        # (covers a daemon restart while backup-sync.py is alive).
        _initial = {}
        try:
            with open(STATUS_FILE, "r") as _sf:
                _initial = json.load(_sf)
        except Exception:
            pass
        if _initial.get("state") != "syncing":
            write_status("waiting")

        # Tidy up a sentinel left by an update that has already ended (see
        # _updating_requested); leaving it would keep re-testing it every tick.
        if not _updating_requested():
            try:
                os.remove(UPDATING_FILE)
            except OSError:
                pass

        # Pre-flight: without idevicebackup2 every backup would crash on the
        # first plug-in. Fail loudly now (journal + status) instead.
        if shutil.which("idevicebackup2") is None:
            msg = "idevicebackup2 not installed"
            print(f"[ERROR] {msg}", flush=True)
            if logf: logf.write(f"[ERROR] {msg}\n")
            write_status("error", message=msg)

        # Failsafe against a wedged main loop: every long-running hold beats,
        # so silence past the backup hang timeout + margin means a genuine
        # wedge -> exit 1 and let Restart=on-failure bring back a healthy daemon.
        def _wd_log(m):
            print(m, flush=True)
            if logf:
                try: logf.write(m + "\n")
                except Exception: pass
        _silence_sec, _ = _backup_watchdog_limits()
        _loop_watchdog = LoopWatchdog(stall_sec=_silence_sec + 100, log_fn=_wd_log)
        _loop_watchdog.start()

        def show(**kw):
            # Passive redraw. Animator.set() is what holds the info screen back,
            # so this no longer needs its own guard.
            ui.set(**kw)

        while True:
            # The Animator owns the EPD; this loop only decides what state to show.
            _heartbeat()
            if SHUTDOWN.is_set():
                time.sleep(0.2); continue   # Animator paints owner + exits

            # A system update preempts everything: the installer is about to stop
            # this daemon and reboot, so nothing else on screen is worth showing.
            if _updating_requested():
                if _prev_state != "updating":
                    ui.request_full()
                    _prev_state = "updating"
                ui.cancel_info()   # the update owns the panel; don't hand it back
                ui.set(_force=True, screen="updating")
                time.sleep(1)
                continue

            # External sync (backup-sync.py) writes the status file with state=syncing/...
            # We own the EPD, so draw its UI from here.
            try:
                with open(STATUS_FILE, "r") as _sf:
                    _st = json.load(_sf)
            except Exception:
                _st = {}
            _state = _st.get("state")

            # Crash detection: if status says syncing but the writer process is gone
            # and the file is stale, declare failure so the UI doesn't hang forever.
            if _state == "syncing":
                try:
                    _age = time.time() - os.stat(STATUS_FILE).st_mtime
                except Exception:
                    _age = 0
                if _age > 60:
                    try:
                        _pg = subprocess.run(["pgrep", "-f", "backup-sync.py"],
                                             capture_output=True, text=True, timeout=5)
                        _alive = _pg.returncode == 0
                    except Exception:
                        _alive = True
                    if not _alive:
                        if not _sync_dead_logged and logf:
                            logf.write(f"[SYNC] backup-sync.py is gone but status stuck at 'syncing' (age={int(_age)}s); marking as failed\n")
                            _sync_dead_logged = True
                        write_status("sync_error", message="Sync process died unexpectedly.")
                        _state = "sync_error"
                        _st = {"state": "sync_error", "message": "Sync process died unexpectedly."}
                else:
                    _sync_dead_logged = False

            # A change in the status-file state is a screen-type transition;
            # request one full refresh so the previous screen can't ghost through.
            state_changed = (_state != _prev_state)
            _prev_state = _state
            if state_changed and _state in ("syncing", "sync_complete", "sync_error"):
                ui.request_full()

            if _state == "syncing":
                pct = _st.get("percent", 0) or 0
                b = _st.get("bytes", 0) or 0
                tot = _st.get("total", 0) or 0
                spd = _st.get("speed", "") or ""
                stalled = bool(_st.get("stalled", False))
                stalled_sec = int(_st.get("stalled_seconds", 0))
                scanning = bool(_st.get("scanning", False))
                scan_sec = int(_st.get("scan_seconds", 0))
                if scanning:
                    sub = f"Syncing to remote server...\nBuilding file list ({scan_sec}s)"
                elif stalled:
                    sub = f"Sync STALLED\nNo progress for {stalled_sec}s ({pct}%)"
                elif b and tot:
                    sub = f"Syncing to remote server...\n{fmt_bytes(b)} / {fmt_bytes(tot)} | {spd}"
                else:
                    sub = "Syncing to remote server...\nPreparing..."
                # ALWAYS set the ui state every iteration during a sync. ui.set just
                # updates a dict (cheap) and guarantees the Animator's next 1Hz tick
                # draws current sync state — protects against external overrides
                # (rejected device, etc.) reverting the display. The one thing it
                # deliberately loses to is the single-tap info screen, which
                # Animator.set() holds for its window and then hands back.
                ui.set(screen="normal", subtitle=sub, percent=pct, animate=True, show_header=True)
                time.sleep(0.5)
                continue

            # Web UI / double-tap "Start Backup" sentinel. Short 15s window so it
            # only takes effect if an iPhone is connected right around the request
            # — it won't silently fire a backup minutes later when one is plugged.
            manual_start = _manual_start_requested()
            if not manual_start:
                try:
                    if os.path.exists(START_FILE):
                        os.remove(START_FILE)   # stale; don't fire it later
                except Exception:
                    pass

            # --- Device handling (only once first-time setup is complete) ---
            if _setup_completed():
                allowed, udid, reason = device_allowed()
            else:
                allowed, udid, reason = False, None, "setup_pending"

            # usbmuxd hotplug workaround: device_allowed() found no device, but the
            # phone may be plugged and just invisible to usbmux (plugged after boot).
            # Restart usbmuxd so it appears. Rate-limited, skipped during a backup;
            # udids_seen=[] reuses the empty result above (no extra idevice_id spawn).
            if reason == "no_device":
                _maybe_refresh_usbmux(logf, udids_seen=[])

            # Manual start forces a backup when auto-start is off (still honours the
            # device filter — won't override a rejected device).
            if manual_start and not allowed and reason == "auto_start_disabled" and udid:
                allowed = True

            # Mutual exclusion: never start a backup while a remote sync is
            # running (and vice-versa — backup-sync.py checks for a live backup).
            # Prevents the two operations and their screens from overlapping.
            if allowed and _sync_running():
                time.sleep(0.3)
                continue

            if allowed:
                try:
                    if os.path.exists(START_FILE):
                        os.remove(START_FILE)   # consume the request
                except Exception:
                    pass
                ui.cancel_info()   # a backup owns the panel from here
                _backup_running = True
                write_status("connected", udid=udid)
                send_notification("device_connected", {"udid": udid})
                ui.set(screen="normal", subtitle="Device detected. Preparing...",
                       percent=None, animate=True, show_header=True)
                ui.request_full()   # clean transition from the boot/idle screen
                try: subprocess.run(["idevicepair", "validate"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
                except Exception: pass
                run_backup(p, logf, ui)
                _backup_running = False
                # Keep the result screen (complete/interrupted/error) up until the
                # iPhone is unplugged, so we don't immediately re-back-up the same
                # device — but let go for work the user asked for meanwhile. This
                # wait used to watch only the cable, so a sync started from the web
                # UI (or a fresh backup request) stayed invisible for its whole run
                # and the panel sat on the previous result. Breaking out is safe:
                # the mutual-exclusion check above still refuses to re-backup a
                # device while a sync is live.
                while not SHUTDOWN.is_set():
                    _heartbeat()
                    if uipolicy.should_release_hold(
                            device_present=device_present(),
                            sync_running=_sync_running(),
                            manual_start=_manual_start_requested()):
                        break
                    time.sleep(1)
                _last_reject_udid = None
                continue
            elif reason == "device_rejected" and udid:
                show(screen="normal", subtitle=f"Device not allowed:\n{udid[:20]}...",
                     percent=None, animate=False, show_header=True)
                if udid != _last_reject_udid:
                    _last_reject_udid = udid
                    if logf: logf.write(f"[REJECT] Device {udid} not in allowed list\n")
                    try:
                        with open(CONFIG_PATH, "r") as _f:
                            _lcfg = yaml.safe_load(_f) or {}
                    except Exception:
                        _lcfg = {}
                    if _lcfg.get("backup", {}).get("notify_on_rejected", True):
                        send_notification("device_rejected", {"udid": udid})
            else:
                # Idle (incl. auto-start disabled): persist a sync result if one
                # is pending, else show the boot/idle screen. No "auto-backup
                # disabled" message — start via double-tap or the web UI.
                _last_reject_udid = None
                if _state in ("sync_complete", "sync_error"):
                    msg = (_st.get("message", "") or "")[:60]
                    head = "Sync complete" if _state == "sync_complete" else "Sync failed"
                    show(screen="complete", subtitle="", percent=None, animate=False,
                         center_block=f"{head}\n{msg}", show_header=True)
                else:
                    show(screen="boot", subtitle="", percent=None, animate=False, show_header=True)
            time.sleep(0.3)
    except Exception as e:
        if logf:
            logf.write(f"[FATAL] main loop: {e}\n")
        print(f"[FATAL] main loop: {e}", flush=True)
        # Fatal must exit NON-ZERO (the old unconditional os._exit(0) meant
        # Restart=on-failure never restarted a crashed daemon): the Animator
        # paints the final screen and exits 1, and the os._exit(1) below is the
        # fallback for when the Animator thread itself is wedged.
        ui.exit_code = 1
        SHUTDOWN.set()
        time.sleep(2)
        os._exit(1)
    finally:
        _backup_running = False
        try: logf.close()
        except Exception: pass

if __name__ == "__main__":
    main()
