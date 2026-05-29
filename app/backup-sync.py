#!/usr/bin/env python3
# backup-sync.py — Double-tap / long-press / web UI: sync backups to remote server
#
# Does NOT touch the e-paper display. iosbackupmachine.py owns the EPD and reads
# this script's status writes from backup_status.json to render sync UI.
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


# ---------- Setup ----------
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

# ---------- Guards ----------
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

# Initial status: iosbackupmachine.py picks this up and starts drawing sync UI
write_status("syncing", percent=0, bytes=0, total=0, speed="")
logf.write("[INFO] status set to syncing — display owned by iosbackupmachine.py\n")

# ---------- Notifications (non-fatal) ----------
try:
    from notifications import send_notification
except ImportError:
    def send_notification(*a, **kw): pass

send_notification("sync_start")

# ---------- Run sync ----------
import sync_manager


def on_progress(info):
    pct = info.get("pct", 0)
    elapsed = info.get("elapsed", 0.0)
    write_status(
        "syncing",
        percent=pct,
        bytes=info.get("bytes", 0),
        total=info.get("total", 0),
        speed=info.get("speed", ""),
    )
    logf.write(f"[SYNC] {pct}% ({elapsed:.0f}s)\n")


try:
    result = sync_manager.run_sync_with_progress(on_progress=on_progress, log_file=logf)
except Exception as e:
    tb = traceback.format_exc()
    logf.write(f"[ERROR] sync raised: {e}\n{tb}")
    result = {"success": False, "message": f"Sync error: {e}", "duration": 0}

if result["success"]:
    logf.write(f"[OK] {result['message']}\n")
    write_status("sync_complete", message=result["message"])
    send_notification("sync_complete", {"message": result["message"]})
else:
    msg = result["message"]
    logf.write(f"[ERROR] {msg}\n")
    write_status("sync_error", message=msg)
    send_notification("sync_error", {"error": msg})

logf.close()
sys.exit(0)
