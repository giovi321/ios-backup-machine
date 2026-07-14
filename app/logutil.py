#!/usr/bin/env python3
"""logutil.py - shared log location and retention policy.

Two directories, split by lifetime:

- LOG_DIR (persistent, on the rootfs): the actual *.log files. It is NOT the
  zram-backed /var/log, so a power cut can never wipe a failed-run log. This
  device shuts down on power loss and is often cut abruptly, so /var/log (a
  compressed RAM disk synced to disk only periodically by armbian-ramlog) loses
  anything written since the last sync. The logs live here instead.

- RUNTIME_DIR (volatile, zram-backed /var/log): high-frequency throwaway IPC
  (backup_status.json, start_requested, stop_requested). Status is rewritten on
  every progress tick, so keeping it in RAM avoids SD-card wear, and it is
  regenerated every run, so losing it on reboot is harmless.

Per-run logs (backup-*.log / sync-*.log) accumulate one file per run, so they
are pruned here by count and age. The continuous append logs (webui/ntp/
autostart/update) are size-capped by logrotate, not by this module.
"""
import os
import glob
import time
from datetime import datetime

# Persistent: survives reboots and power loss.
LOG_DIR = os.getenv("IOSBACKUP_LOG_DIR", "/var/lib/iosbackupmachine")
# Volatile: zram-backed, cleared each boot. Throwaway runtime IPC only.
RUNTIME_DIR = os.getenv("IOSBACKUP_RUNTIME_DIR", "/var/log/iosbackupmachine")

# Per-run log retention.
LOG_KEEP_PER_KIND = int(os.getenv("IOSBACKUP_LOG_KEEP", "50"))
LOG_MAX_AGE_DAYS = int(os.getenv("IOSBACKUP_LOG_MAX_AGE_DAYS", "90"))
_PRUNE_PREFIXES = ("backup-", "sync-")


class TimestampedLog:
    """Line-timestamping wrapper around a text log file.

    Prefixes every complete line written to it with a wall-clock
    ``[YYYY-MM-DD HH:MM:SS]`` stamp, so per-run logs (backup-*/sync-*) are
    correlatable with each other and with the continuous logs
    (autostart/ntp/webui) instead of carrying only rsync's elapsed-seconds
    counter. Callers write whole lines (each ending in ``\\n``); the stamp is
    applied per line, so a single multi-line write is handled too. A partial
    trailing line, if any, is flushed with a stamp on close.
    """

    def __init__(self, fh):
        self._fh = fh
        self._buf = ""

    @staticmethod
    def _stamp():
        return datetime.now().strftime("[%Y-%m-%d %H:%M:%S] ")

    def write(self, text):
        if not text:
            return
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._fh.write(self._stamp() + line + "\n")

    def flush(self):
        try:
            self._fh.flush()
        except Exception:
            pass

    def close(self):
        if self._buf:
            try:
                self._fh.write(self._stamp() + self._buf)
            except Exception:
                pass
            self._buf = ""
        try:
            self._fh.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def open_run_log(path, mode="a"):
    """Open a per-run log line-buffered and wrap it so every written line is
    wall-clock timestamped. Returns a TimestampedLog (write/flush/close, and
    usable as a context manager)."""
    return TimestampedLog(open(path, mode, buffering=1))


def prune_logs(log_dir=None, keep_per_kind=None, max_age_days=None):
    """Delete old per-run logs: keep the newest ``keep_per_kind`` of each kind
    (backup / sync) and drop anything older than ``max_age_days``. The freshly
    created log sorts newest, so it is always kept. Best-effort; never raises."""
    log_dir = LOG_DIR if log_dir is None else log_dir
    keep_per_kind = LOG_KEEP_PER_KIND if keep_per_kind is None else keep_per_kind
    max_age_days = LOG_MAX_AGE_DAYS if max_age_days is None else max_age_days
    now = time.time()
    max_age = max_age_days * 86400
    for prefix in _PRUNE_PREFIXES:
        try:
            files = glob.glob(os.path.join(log_dir, f"{prefix}*.log"))
            files.sort(key=os.path.getmtime, reverse=True)  # newest first
        except Exception:
            continue
        for i, path in enumerate(files):
            try:
                too_many = i >= keep_per_kind
                too_old = max_age_days > 0 and (now - os.path.getmtime(path)) > max_age
                if too_many or too_old:
                    os.remove(path)
            except Exception:
                pass
