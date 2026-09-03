#!/usr/bin/env python3
# backup-sync.py — Double-tap / long-press / web UI: sync backups to remote server
#
# Does NOT touch the e-paper display. iosbackupmachine.py owns the EPD and reads
# this script's status writes from backup_status.json to render sync UI.
import os, sys, time, json, subprocess, yaml, traceback
from datetime import datetime

CONFIG_PATH = os.getenv("IOSBACKUP_CONFIG", "/root/iosbackupmachine/config.yaml")

# Make sibling modules importable when run via Popen from webui
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import logutil
# Persistent logs on the rootfs; runtime status on the volatile zram /var/log.
LOG_DIR = logutil.LOG_DIR
RUNTIME_DIR = logutil.RUNTIME_DIR
STATUS_FILE = os.path.join(RUNTIME_DIR, "backup_status.json")


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
    """Atomic status write: tmp file + rename, so concurrent readers never see partial JSON."""
    try:
        os.makedirs(RUNTIME_DIR, exist_ok=True)
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
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


logf = None      # set by main() once the run log is open


def _probe_failed(name, exc):
    """A guard probe that cannot run must fail closed (assume busy): an
    overlapping rsync/backup is worse than a skipped sync. The failure goes to
    the journal (stderr) and, once open, the run log."""
    msg = f"[WARN] {name} probe failed ({exc}); assuming busy"
    print(msg, file=sys.stderr)
    if logf is not None:
        try:
            logf.write(msg + "\n")
        except Exception:
            pass


def backup_running():
    try:
        out = subprocess.run(["pgrep", "-f", "idevicebackup2"],
                             capture_output=True, text=True)
        return out.returncode == 0
    except Exception as e:
        _probe_failed("backup_running", e)
        return True


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
    except Exception as e:
        _probe_failed("another_sync_running", e)
        return True


def sync_in_progress():
    """True if a sync is already running — either another backup-sync.py or the
    in-process auto-sync in iosbackupmachine.py (status 'syncing' + a live rsync).
    Guards against kill_stale_rsync() killing an active auto-sync's rsync."""
    try:
        with open(STATUS_FILE, "r") as f:
            state = (json.load(f) or {}).get("state")
    except FileNotFoundError:
        return False       # no status yet: nothing has ever synced
    except Exception as e:
        _probe_failed("sync_in_progress status-file", e)
        return True        # unparseable status: assume a sync is active
    if state != "syncing":
        return False
    try:
        return subprocess.run(["pgrep", "-f", "/usr/bin/rsync"],
                              capture_output=True).returncode == 0
    except Exception as e:
        _probe_failed("sync_in_progress pgrep", e)
        return True


def kill_stale_rsync(logf):
    """Kill orphaned rsync processes left behind by previous interrupted runs."""
    try:
        r = subprocess.run(["pkill", "-9", "-f", "/usr/bin/rsync"],
                           capture_output=True, text=True)
        if r.returncode == 0:
            logf.write("[INFO] killed stale rsync processes\n")
    except Exception as e:
        logf.write(f"[WARN] kill_stale_rsync failed: {e}\n")


def main():
    global logf

    # ---------- Setup ----------
    CFG = load_config(CONFIG_PATH)
    for k, v in CFG.get("env", {}).items():
        os.environ[k] = str(v)

    os.makedirs(LOG_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    logpath = os.path.join(LOG_DIR, f"sync-{ts}.log")
    try:
        logf = logutil.open_run_log(logpath)
        logutil.prune_logs()   # trim old per-run logs (count + age + size)
    except Exception as e:
        print(f"[FATAL] cannot open {logpath}: {e}", file=sys.stderr)
        write_status("sync_error", message=f"Cannot write log: {e}")
        sys.exit(1)

    logf.write(f"backup-sync.py starting (pid={os.getpid()})\n")

    # ---------- Notifications (non-fatal) ----------
    # Wired before every early exit (disabled sync, guards, battery) so delivery
    # diagnostics from those paths land in this run's log instead of discarded
    # stdout when launched from the web UI.
    try:
        from notifications import send_notification
        import notifications as _notif
        # Delivery problems (a 403, an unavailable auth header) used to reach
        # only the journal; put them in the sync log the web UI actually serves.
        _notif.set_logger(lambda m: logf.write(m + "\n"))
    except ImportError:
        _notif = None
        def send_notification(*a, **kw): pass

    # ---------- Guards ----------
    if not CFG.get("sync", {}).get("enabled", False):
        logf.write("[SKIP] sync is disabled in config\n")
        write_status("sync_error", message="Sync is disabled in settings.")
        logf.close()
        sys.exit(0)

    if another_sync_running():
        logf.write("[SKIP] another backup-sync.py is already running\n")
        write_status("sync_skipped", message="Another sync is already running.")
        logf.close()
        sys.exit(0)

    if backup_running():
        logf.write("[SKIP] backup (idevicebackup2) in progress\n")
        write_status("sync_error", message="Backup in progress — sync skipped.")
        logf.close()
        sys.exit(0)

    if sync_in_progress():
        logf.write("[SKIP] a sync is already in progress (in-process or external)\n")
        write_status("sync_skipped", message="A sync is already in progress.")
        logf.close()
        sys.exit(0)

    # Power-aware: refuse to start a sync on low battery (unless charging).
    # Fail-open if the battery can't be read.
    try:
        import power
        _threshold = CFG.get("sync", {}).get("min_battery_percent", 35)
        _batt_ok, _batt_reason = power.sync_allowed(_threshold)
    except Exception:
        _batt_ok, _batt_reason = True, ""
    if not _batt_ok:
        # Same structured shape as any other sync failure, so a subscriber handling
        # sync_error does not need a special case for the ones that never started.
        import sync_manager as _sm
        _failure = _sm.build_failure("battery_low", detail=_batt_reason)
        logf.write(f"[SKIP] {_failure['message']}\n")
        write_status("sync_error", message=_failure["message"],
                     reason_code=_failure["reason_code"])
        try:
            send_notification("sync_error", {"error": _failure["message"],
                                             **_sm.notification_payload(_failure)})
            if _notif:
                _notif.flush()   # this path exits immediately; see the tail flush
        except Exception:
            pass
        logf.close()
        sys.exit(0)

    # Clean up any orphaned rsync processes from previous interrupted runs
    kill_stale_rsync(logf)

    # Initial status: iosbackupmachine.py picks this up and starts drawing sync UI
    write_status("syncing", percent=0, bytes=0, total=0, speed="")
    logf.write("[INFO] status set to syncing — display owned by iosbackupmachine.py\n")

    send_notification("sync_start")

    # ---------- Run sync ----------
    import sync_manager

    # Progress lines are written by sync_manager's SyncLogWriter, which owns the
    # per-run log for the whole transfer. This callback only drives the status file
    # that the display daemon and the dashboard read.
    def on_progress(info):
        write_status(
            "syncing",
            percent=info.get("pct", 0),
            bytes=info.get("bytes", 0),
            total=info.get("total", 0),
            speed=info.get("speed", ""),
            eta_seconds=info.get("eta_seconds"),
            stalled=bool(info.get("stalled", False)),
            stalled_seconds=int(info.get("stalled_seconds", 0)),
            scanning=bool(info.get("scanning", False)),
            scan_seconds=int(info.get("scan_seconds", 0)),
        )

    try:
        result = sync_manager.run_sync_with_progress(on_progress=on_progress, log_file=logf)
    except Exception as e:
        tb = traceback.format_exc()
        logf.write(f"[ERROR] sync raised: {e}\n{tb}")
        result = sync_manager._fail("internal_error",
                                    detail=f"the sync runner raised {type(e).__name__}: {e}")

    if result["success"]:
        logf.write(f"[OK] {result['message']}\n")
        write_status("sync_complete", message=result["message"])
        send_notification("sync_complete", {"message": result["message"]})
    else:
        # The failure payload carries the reason code, the exit status, how far the
        # transfer got and what the post-mortem probes found. Pass it on whole: MQTT
        # and webhook consumers get the same verdict the log has, and can key off
        # reason_code instead of matching English.
        failure = result.get("failure") or {"message": result["message"],
                                            "reason_code": "internal_error"}
        if not failure.get("logged"):
            logf.write(f"[ERROR] {failure['message']}\n")   # pre-flight failures only
        write_status("sync_error", message=failure["message"],
                     reason_code=failure.get("reason_code"))
        send_notification("sync_error",
                          {"error": failure["message"],
                           **sync_manager.notification_payload(failure)})

    # Notifications are delivered on background threads. Wait for them before the
    # interpreter exits, or they are killed mid-request and the result of every
    # manual sync is lost without a trace. notifications.flush is also registered
    # with atexit; calling it here keeps the wait visible where the exit happens,
    # and lets a delivery that timed out be recorded in the log.
    try:
        import notifications as _notifications
        if not _notifications.flush():
            logf.write("[WARN] notification delivery did not finish in time\n")
    except Exception as e:
        logf.write(f"[WARN] notification flush failed: {e}\n")

    logf.close()
    # Non-zero on failure so systemd and other callers can observe it; the
    # early-exit skips above are not failures and stay 0.
    sys.exit(0 if result["success"] else 2)


if __name__ == "__main__":
    main()
