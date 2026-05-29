#!/usr/bin/env python3
# backup-sync.py — Double-tap / long-press / web UI: sync backups to remote server
import os, sys, time, json, subprocess, yaml, traceback
from datetime import datetime

CONFIG_PATH = os.getenv("IOSBACKUP_CONFIG", "/root/iosbackupmachine/config.yaml")
LOG_DIR = "/var/log/iosbackupmachine"
STATUS_FILE = os.path.join(LOG_DIR, "backup_status.json")

# Make sibling modules importable when run via Popen from webui
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)


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


def fmt_bytes(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def backup_running():
    try:
        out = subprocess.run(["pgrep", "-f", "idevicebackup2"],
                             capture_output=True, text=True)
        return out.returncode == 0
    except Exception:
        return False


def another_sync_running():
    """True if another backup-sync.py process is running."""
    try:
        out = subprocess.run(["pgrep", "-f", "backup-sync.py"],
                             capture_output=True, text=True)
        if out.returncode != 0:
            return False
        my_pid = os.getpid()
        my_ppid = os.getppid()
        for pid_str in out.stdout.split():
            try:
                pid = int(pid_str)
            except ValueError:
                continue
            if pid != my_pid and pid != my_ppid:
                return True
        return False
    except Exception:
        return False


def kill_stale_rsync(logf):
    """Kill orphaned rsync processes left behind by previous interrupted runs."""
    try:
        r = subprocess.run(["pkill", "-9", "-f", "/usr/bin/rsync"],
                           capture_output=True, text=True)
        if r.returncode == 0:
            logf.write("[INFO] killed stale rsync processes\n")
    except Exception as e:
        logf.write(f"[WARN] kill_stale_rsync failed: {e}\n")


# ---------- Setup: config + log file FIRST ----------
CFG = load_config(CONFIG_PATH)
for k, v in CFG.get("env", {}).items():
    os.environ[k] = str(v)

os.makedirs(LOG_DIR, exist_ok=True)
ts = datetime.now().strftime("%Y%m%d-%H%M%S")
logpath = os.path.join(LOG_DIR, f"sync-{ts}.log")
try:
    logf = open(logpath, "a", buffering=1)
except Exception as e:
    print(f"[FATAL] cannot open {logpath}: {e}", file=sys.stderr)
    write_status("sync_error", message=f"Cannot write log: {e}")
    sys.exit(1)

logf.write(f"[{ts}] backup-sync.py starting (pid={os.getpid()})\n")

# ---------- Guards (visible in dashboard + log) ----------
if not CFG.get("sync", {}).get("enabled", False):
    logf.write("[SKIP] sync is disabled in config\n")
    write_status("sync_error", message="Sync is disabled in settings.")
    logf.close()
    sys.exit(0)

if another_sync_running():
    logf.write("[SKIP] another backup-sync.py is already running\n")
    logf.close()
    sys.exit(0)

if backup_running():
    logf.write("[SKIP] backup (idevicebackup2) in progress\n")
    write_status("sync_error", message="Backup in progress — sync skipped.")
    logf.close()
    sys.exit(0)

# Clean up any orphaned rsync processes from previous interrupted runs
kill_stale_rsync(logf)

# ---------- Status BEFORE EPD init so dashboard updates even if display fails ----------
write_status("syncing", percent=0)

# ---------- EPD setup (best effort, non-fatal) ----------
ORIENT = str(CFG.get("orientation", "landscape_right")).lower()
CUSTOM_FONT = CFG.get("font_path") or "/root/iosbackupmachine/UbuntuMono-Regular.ttf"

epd = None
F = F_SM = None
PW = PH = LW = LH = 0
_Image = _ImageDraw = None
try:
    from PIL import Image as _Image, ImageDraw as _ImageDraw, ImageFont
    from waveshare_epd import epd2in13_V4, epdconfig

    def _font(sz):
        try:
            return ImageFont.truetype(CUSTOM_FONT, sz)
        except Exception:
            return ImageFont.load_default()

    F = _font(14)
    F_SM = _font(12)
    try:
        epdconfig.module_exit()
    except Exception:
        pass
    epd = epd2in13_V4.EPD()
    epd.init()
    epd.Clear(0xFF)
    PW, PH = epd.width, epd.height
    LW, LH = (PH, PW) if ORIENT in ("landscape_right", "landscape_left") else (PW, PH)
    logf.write(f"[INFO] EPD initialized {PW}x{PH}, orientation={ORIENT}\n")
except Exception as e:
    logf.write(f"[WARN] EPD init failed, continuing without display: {e}\n")
    epd = None

BAR_H = 8
BAR_MARGIN = 16


def text_wh(d, t, f):
    try:
        l, t0, r, b = d.textbbox((0, 0), t, font=f)
        return r - l, b - t0
    except AttributeError:
        return d.textsize(t, font=f)


def show_message(lines_with_fonts, percent=None):
    if epd is None or _Image is None:
        return
    try:
        img = _Image.new("1", (LW, LH), 255)
        drw = _ImageDraw.Draw(img)
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
    except Exception as e:
        logf.write(f"[WARN] show_message failed: {e}\n")


show_message([("Syncing to remote server...", F), ("Preparing...", F_SM)], percent=0)

# ---------- Notifications (non-fatal) ----------
try:
    from notifications import send_notification
except ImportError:
    def send_notification(*a, **kw): pass

send_notification("sync_start")

# ---------- Run sync ----------
import sync_manager

last_display_update = [0]


def on_progress(info):
    pct = info["pct"]
    elapsed = info["elapsed"]
    write_status("syncing", percent=pct)
    logf.write(f"[SYNC] {pct}% ({elapsed:.0f}s)\n")
    now = time.time()
    if now - last_display_update[0] >= 2 or pct >= 100:
        last_display_update[0] = now
        if info.get("total"):
            sub = f"{fmt_bytes(info['bytes'])} / {fmt_bytes(info['total'])} | {info['speed']}"
        else:
            sub = f"{fmt_bytes(info['bytes'])} | {info['speed']}"
        show_message([("Syncing to remote server...", F), (sub, F_SM)], percent=pct)


try:
    result = sync_manager.run_sync_with_progress(on_progress=on_progress, log_file=logf)
except Exception as e:
    tb = traceback.format_exc()
    logf.write(f"[ERROR] sync raised: {e}\n{tb}")
    result = {"success": False, "message": f"Sync error: {e}", "duration": 0}

if result["success"]:
    logf.write(f"[OK] {result['message']}\n")
    write_status("sync_complete", message=result["message"])
    show_message([("Sync complete", F), (result["message"], F_SM)], percent=100)
    send_notification("sync_complete", {"message": result["message"]})
else:
    msg = result["message"]
    logf.write(f"[ERROR] {msg}\n")
    short = msg[:37] + "..." if len(msg) > 40 else msg
    write_status("sync_error", message=msg)
    show_message([("Sync failed", F), (short, F_SM)])
    send_notification("sync_error", {"error": msg})

logf.close()

# Release display so shutdown / boot-message can use it
if epd is not None:
    try:
        epd.sleep()
    except Exception:
        pass
    try:
        from waveshare_epd import epdconfig as _epdconfig
        _epdconfig.module_exit()
    except Exception:
        pass

time.sleep(5)

# Chain to boot screen
next_path = os.path.join(SCRIPT_DIR, "boot-message.py")
if os.path.isfile(next_path):
    rc = subprocess.run([sys.executable, next_path]).returncode
    sys.exit(rc)
else:
    sys.exit(0)
