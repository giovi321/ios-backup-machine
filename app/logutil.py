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

# Persistent: survives reboots and power loss.
LOG_DIR = os.getenv("IOSBACKUP_LOG_DIR", "/var/lib/iosbackupmachine")
# Volatile: zram-backed, cleared each boot. Throwaway runtime IPC only.
RUNTIME_DIR = os.getenv("IOSBACKUP_RUNTIME_DIR", "/var/log/iosbackupmachine")

# Per-run log retention.
LOG_KEEP_PER_KIND = int(os.getenv("IOSBACKUP_LOG_KEEP", "50"))
LOG_MAX_AGE_DAYS = int(os.getenv("IOSBACKUP_LOG_MAX_AGE_DAYS", "90"))
_PRUNE_PREFIXES = ("backup-", "sync-")


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
