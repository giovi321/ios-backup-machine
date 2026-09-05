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
are pruned here by count, age and an aggregate size cap, and each file is
additionally capped by the writer — pruning only happens as a run starts, so it
cannot bound the run in progress. The continuous append logs
(webui/ntp/autostart/update) are size-capped by logrotate, not by this module.
"""
import io
import os
import glob
import re
import sys
import time
from collections import deque
from datetime import datetime

# Persistent: survives reboots and power loss.
LOG_DIR = os.getenv("IOSBACKUP_LOG_DIR", "/var/lib/iosbackupmachine")
# Volatile: zram-backed, cleared each boot. Throwaway runtime IPC only.
RUNTIME_DIR = os.getenv("IOSBACKUP_RUNTIME_DIR", "/var/log/iosbackupmachine")

# Per-run log retention.
LOG_KEEP_PER_KIND = int(os.getenv("IOSBACKUP_LOG_KEEP", "50"))
LOG_MAX_AGE_DAYS = int(os.getenv("IOSBACKUP_LOG_MAX_AGE_DAYS", "90"))
# Aggregate size cap per kind: even 50 tiny runs of each kind cannot exceed it.
LOG_MAX_BYTES_PER_KIND = int(os.getenv("IOSBACKUP_LOG_MAX_BYTES_PER_KIND",
                                       str(100 * 1024 * 1024)))
_PRUNE_PREFIXES = ("backup-", "sync-")
# Per-FILE cap. The aggregate cap above is only enforced at prune time — as a
# run starts — so nothing bounds the file being written: an idevicebackup2 or
# rsync error loop fills the rootfs within one run, and the daemon's backup log
# is one file for the whole daemon lifetime, not one per backup. Kept an order
# of magnitude below LOG_MAX_BYTES_PER_KIND so a single file can never exceed
# the aggregate cap on its own — that is the case where the size sweep below
# would have to delete the log that is still being written.
LOG_MAX_BYTES_PER_FILE = int(os.getenv("IOSBACKUP_LOG_MAX_BYTES_PER_FILE",
                                       str(8 * 1024 * 1024)))
# Once capped, keep this many bytes of the most recent lines and append them on
# close: the run's verdict ([OK]/[ERROR]/[POSTMORTEM]) is written last, and a
# log that stops mid-run tells the user nothing about how it ended.
_CAPPED_TAIL_BYTES = 64 * 1024


class TimestampedLog:
    """Line-timestamping wrapper around a text log file.

    Prefixes every complete line written to it with a wall-clock
    ``[YYYY-MM-DD HH:MM:SS]`` stamp, so per-run logs (backup-*/sync-*) are
    correlatable with each other and with the continuous logs
    (autostart/ntp/webui) instead of carrying only rsync's elapsed-seconds
    counter. Callers write whole lines (each ending in ``\\n``); the stamp is
    applied per line, so a single multi-line write is handled too. A partial
    trailing line, if any, is flushed with a stamp on close.

    The file is also capped at ``max_bytes``: past it further output is
    suppressed and only the last few lines are held in memory and appended on
    close, so one runaway run cannot fill the rootfs while its verdict — which
    is written last — still reaches the log. ``stamp=False`` writes text
    through unchanged, for a log that formats its own lines.
    """

    def __init__(self, fh, max_bytes=None, start_bytes=0, stamp=True):
        self._fh = fh
        self._buf = ""
        self._failures = 0
        self._dead = False
        self._stamped = stamp
        # <= 0 disables the cap, matching prune_logs' parameter convention.
        self._max = LOG_MAX_BYTES_PER_FILE if max_bytes is None else max_bytes
        self._written = start_bytes
        self._capped = False
        self._dropped = 0
        self._tail = deque()          # (text, nbytes) of the suppressed lines
        self._tail_bytes = 0
        # Scaled down for a small cap so a capped file stays near its cap.
        self._tail_max = (min(_CAPPED_TAIL_BYTES, max(1, self._max // 8))
                          if self._max > 0 else _CAPPED_TAIL_BYTES)

    @staticmethod
    def _stamp():
        return datetime.now().strftime("[%Y-%m-%d %H:%M:%S] ")

    def write(self, text):
        if not text or self._dead:
            return
        if not self._stamped:
            # An unstamped log is written by several threads at once (the main
            # loop, the WireGuard watcher, the notification threads); holding a
            # partial line here would give them something to race on.
            self._emit(text)
            return
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._emit(self._stamp() + line + "\n")

    def _emit(self, s):
        """Write one formatted chunk, enforcing the per-file cap. Never
        raises; see _put."""
        # Bytes, not characters: a backup log full of UTF-8 filenames would
        # otherwise overrun the cap by up to 4x.
        n = len(s.encode("utf-8", "replace"))
        if self._capped:
            self._keep_tail(s, n)
            return
        if self._max > 0 and self._written + n > self._max:
            self._capped = True
            self._keep_tail(s, n)
            self._put(f"[logutil] log reached its {self._max}-byte cap; further "
                      f"output is suppressed and the last lines are appended "
                      f"when the run ends\n")
            return
        self._written += n
        self._put(s)

    def _keep_tail(self, s, n):
        """Hold the most recent suppressed lines, bounded in bytes rather than
        in lines — one subprocess emitting a multi-megabyte line without a
        newline would otherwise make the tail as unbounded as the file was."""
        self._dropped += 1
        self._tail.append((s, n))
        self._tail_bytes += n
        while self._tail_bytes > self._tail_max and len(self._tail) > 1:
            self._tail_bytes -= self._tail.popleft()[1]

    def _put(self, s):
        try:
            self._fh.write(s)
            self._failures = 0
        except Exception as e:
            # ENOSPC / EROFS must never reach the caller: logf.write is
            # called unguarded all over iosbackupmachine.py, including from
            # inside its fatal handler. The first failure goes to stderr so
            # it still reaches the journal; repeated failures stop trying.
            if self._failures == 0:
                print(f"[logutil] log write failed: {e}", file=sys.stderr)
            self._failures += 1
            if self._failures >= 10:
                self._dead = True

    def flush(self):
        try:
            self._fh.flush()
        except Exception:
            pass

    def close(self):
        if self._buf:
            self._emit(self._stamp() + self._buf)
            self._buf = ""
        if self._capped:
            self._put(f"[logutil] {self._dropped} suppressed line(s); the last "
                      f"{len(self._tail)} follow\n")
            for s, _ in self._tail:
                self._put(s)
            self._tail.clear()
            self._tail_bytes = 0
        try:
            self._fh.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def open_run_log(path, mode="a", stamp=True, max_bytes=None):
    """Open a per-run log line-buffered and wrap it so every written line is
    wall-clock timestamped. Returns a TimestampedLog (write/flush/close, and
    usable as a context manager). The writer caps the file at ``max_bytes``
    (LOG_MAX_BYTES_PER_FILE by default), since nothing else bounds a run that
    is still producing output; ``stamp=False`` is for a log that already
    formats its own lines."""
    try:
        # Appending reuses what is on disk (a same-second restart, or a log
        # reopened after a crash), so the budget starts from what is already
        # there — otherwise every reopen hands the same file a fresh cap.
        start = os.path.getsize(path) if "a" in mode else 0
    except OSError:
        start = 0
    return TimestampedLog(open(path, mode, buffering=1), max_bytes=max_bytes,
                          start_bytes=start, stamp=stamp)


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def stamp_stream(src, dst):
    """Copy ``src`` to ``dst`` line by line, prefixing each with the log stamp.

    The shell-produced logs (update.log, and backup-sync.py's output redirected
    into autostart.log) are plain stdout redirects, so they cannot use
    TimestampedLog. Piping them through this keeps every log in the system on one
    format, which is the whole point of stamping them: a sync failure can be
    lined up against what the updater or the button handler was doing at that
    second.

    ANSI colour codes are stripped — install.sh writes them for a terminal, and
    in a file the web UI renders verbatim they are noise. Blank lines stay blank
    rather than becoming a lone timestamp.
    """
    for line in src:
        line = _ANSI_RE.sub("", line.rstrip("\n").rstrip("\r"))
        if line.strip():
            dst.write(datetime.now().strftime("[%Y-%m-%d %H:%M:%S] ") + line + "\n")
        else:
            dst.write("\n")
        try:
            dst.flush()      # a truncated update must still show what it reached
        except Exception:
            pass


def _stamp_stdin():
    """``python3 logutil.py --stamp`` — the entry point the shell pipes into.

    Reads stdin with errors='replace' so a stray non-UTF-8 byte from a subprocess
    cannot kill the pipeline and take the rest of the log with it.
    """
    src = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace")
    stamp_stream(src, sys.stdout)


def prune_logs(log_dir=None, keep_per_kind=None, max_age_days=None,
               max_bytes_per_kind=None):
    """Delete old per-run logs: keep the newest ``keep_per_kind`` of each kind
    (backup / sync), drop anything older than ``max_age_days``, then cap each
    kind's aggregate size at ``max_bytes_per_kind`` by deleting oldest first.
    The freshly created log sorts newest, so all three rules leave it alone.
    Best-effort; never raises."""
    log_dir = LOG_DIR if log_dir is None else log_dir
    keep_per_kind = LOG_KEEP_PER_KIND if keep_per_kind is None else keep_per_kind
    max_age_days = LOG_MAX_AGE_DAYS if max_age_days is None else max_age_days
    max_bytes_per_kind = (LOG_MAX_BYTES_PER_KIND
                          if max_bytes_per_kind is None else max_bytes_per_kind)
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
        if max_bytes_per_kind <= 0:
            continue
        try:
            sized = []
            total = 0
            for path in files:                          # newest first still
                if not os.path.exists(path):            # pruned above
                    continue
                try:
                    size = os.path.getsize(path)
                except Exception:
                    size = 0
                sized.append((path, size))
                total += size
            # sized[1:] spares the newest: it is the log being written (the
            # auto-sync prune runs while the daemon holds its backup log open),
            # so deleting it frees nothing until the writer closes and only
            # makes the running job vanish from the Logs page.
            for path, size in reversed(sized[1:]):      # oldest first
                if total <= max_bytes_per_kind:
                    break
                os.remove(path)
                total -= size
        except Exception:
            pass


if __name__ == "__main__":
    if "--stamp" in sys.argv[1:]:
        _stamp_stdin()
    else:
        print("usage: logutil.py --stamp   (stamp stdin, write to stdout)",
              file=sys.stderr)
        sys.exit(2)
