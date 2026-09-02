#!/usr/bin/env python3
"""
sync_manager.py - Remote backup sync via rsync over SSH.

Supports SSH key and password authentication.
Credentials are decrypted from the encrypted sync config store.
When the config carries a host_key_fingerprint, the server's SSH host key is
verified and pinned before anything is transferred (see host_key.py).
"""
import fnmatch, os, sys, re, select, socket, subprocess, tempfile, threading, time, yaml

import host_key
import sync_crypto

try:
    import power
except ImportError:
    power = None

CONFIG_PATH = os.getenv("IOSBACKUP_CONFIG", "/root/iosbackupmachine/config.yaml")

# Destination-managed metadata the source never has. Excluded from the transfer
# so --delete doesn't try (and fail, noisily) to remove the remote's Syncthing
# markers / lost+found, and excluded from local_tree_size() so the progress
# denominator counts the same files rsync does.
DEST_ONLY_EXCLUDES = (".stfolder", ".stignore", ".stversions", ".stglobalstate",
                      ".stfolder/**", "~syncthing~*.tmp", ".syncthing.*.tmp",
                      "lost+found")


def _load_config():
    try:
        with open(CONFIG_PATH, "r") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _load_backup_dir():
    return _load_config().get("backup_dir", "/media/iosbackup/")


def _check_network_allowed():
    """Check if sync is allowed on the current network.
    Returns (allowed: bool, reason: str)."""
    cfg = _load_config()
    sync_cfg = cfg.get("sync", {})
    allowed = sync_cfg.get("allowed_network", "any")
    if allowed == "any":
        return True, ""
    try:
        import netutil
        wifi_ip = netutil.get_wifi_ip()
        usb_ip = netutil.get_usb_iphone_ip()
    except ImportError:
        return True, ""  # can't check, allow

    if allowed == "wifi":
        if wifi_ip:
            return True, ""
        return False, "Sync restricted to WiFi only (not connected)."
    elif allowed == "wifi_ssid":
        required_ssid = sync_cfg.get("allowed_ssid", "")
        if not wifi_ip:
            return False, "Sync restricted to WiFi (not connected)."
        if required_ssid:
            try:
                current_ssid = netutil.get_wifi_ssid() or ""
                if current_ssid != required_ssid:
                    return False, f"Sync restricted to SSID '{required_ssid}' (current: '{current_ssid}')."
            except Exception:
                pass
        return True, ""
    elif allowed == "usb":
        if usb_ip:
            return True, ""
        return False, "Sync restricted to iPhone USB tethering (not connected)."
    return True, ""


def _diagnose_unreachable(host, port):
    """Best-effort reason the sync server can't be reached right now, so a
    connection problem produces a clear, actionable message on the e-ink and
    dashboard instead of a cryptic rsync exit code.

    Returns a short reason string, or None if the server is reachable (or we
    can't introspect) — in which case the caller proceeds and lets rsync run.
    """
    if not host:
        return None
    try:
        import netutil
    except ImportError:
        netutil = None

    # Is the sync host reachable on its SSH port? A fast success means there's no
    # connectivity problem; only diagnose further when the connect actually fails
    # (this also resolves the hostname, so DNS failures over a down VPN land here).
    try:
        socket.create_connection((host, int(port)), timeout=8).close()
        return None
    except OSError:
        pass

    if netutil is None:
        return f"Sync server unreachable ({host})."

    # Unreachable — report the most fundamental missing layer first.
    wg = _load_config().get("wireguard", {})
    if not (netutil.get_wifi_ip() or netutil.get_usb_iphone_ip()):
        return "No network connection (no WiFi or iPhone hotspot)."
    if wg.get("enabled") and not netutil.get_wireguard_ip(wg.get("interface_name", "wg0")):
        return "VPN not connected (WireGuard is down)."
    if not netutil.have_connectivity():
        return "No internet connection."
    return f"Sync server unreachable ({host})."


def _prepare_sync(passphrase=None, backup_dir=None, progress=False, context=None):
    """Shared setup for run_sync and run_sync_with_progress.
    Returns (cmd, temp_files, error_dict) — error_dict is set on failure.
    temp_files is the list of paths the caller must pass to _cleanup_temp().

    ``context``, when a dict is passed in, is filled with the non-secret
    connection details (host, port, user, paths). The post-mortem needs the host
    and port to probe the remote after a failure, and lifting them out here means
    it never has to decrypt the credential store a second time."""
    net_ok, net_reason = _check_network_allowed()
    if not net_ok:
        return None, [], _fail("network_not_allowed", detail=net_reason)

    cfg = sync_crypto.decrypt_sync_config(passphrase=passphrase)
    if not cfg:
        return None, [], _fail("credentials_unavailable")

    host = cfg.get("host", "")
    port = cfg.get("port", 22)
    username = cfg.get("username", "")
    auth_method = cfg.get("auth_method", "key")
    ssh_key = cfg.get("ssh_key", "")
    password = cfg.get("password", "")
    remote_path = cfg.get("remote_path", "")
    expected_fp = cfg.get("host_key_fingerprint", "")

    if context is not None:
        context.update({"host": host, "port": port, "username": username,
                        "remote_path": remote_path, "auth_method": auth_method})

    if not host or not username or not remote_path:
        return None, [], _fail("config_incomplete",
                               detail="incomplete sync configuration (host, user or remote path)")

    # Pre-flight reachability: turn a would-be cryptic rsync connection failure
    # into a clear cause (no network / VPN down / no internet). Both the manual
    # and auto-sync paths funnel through here, so the message reaches the e-ink,
    # the dashboard, and notifications.
    reason = _diagnose_unreachable(host, port)
    if reason:
        return None, [], _fail("ssh_connection_failed", detail=reason)

    # Optional host key pinning. Runs before a single byte is transferred, and
    # fails closed: a mismatch or an unreadable key aborts the sync rather than
    # falling back to trusting whatever the server offered.
    temp_files = []
    known_hosts = None
    if expected_fp:
        known_hosts, hk_err = host_key.verify_and_write_known_hosts(host, port, expected_fp)
        if hk_err:
            return None, [], _fail("host_key_mismatch", detail=hk_err)
        if known_hosts:
            temp_files.append(known_hosts)

    if backup_dir is None:
        backup_dir = _load_backup_dir()
    if not backup_dir.endswith("/"):
        backup_dir += "/"
    if context is not None:
        context["backup_dir"] = backup_dir

    # ServerAliveInterval/CountMax detects dead connections in ~90s instead of
    # waiting for the TCP-level keepalive (default 2h).
    ssh_opts = (f"ssh -p {port} {' '.join(host_key.strict_host_key_opts(known_hosts))} "
                f"-o ConnectTimeout=15 -o ServerAliveInterval=30 -o ServerAliveCountMax=3")

    # -a (archive) but NOT -z: iOS backups are encrypted / already-compressed, so
    # gzip gains ~nothing and just burns the Radxa's weak CPU (and can bottleneck
    # the transfer). --partial + --partial-dir keep incomplete files in a stable
    # dir on the remote so a reboot mid-sync resumes; rsync excludes it from --delete.
    rsync_flags = ["-a", "--delete", "--partial", "--partial-dir=.rsync-partial",
                   "--rsync-path=/usr/bin/rsync"]
    # Leave destination-managed metadata alone. The source never has these, so
    # without excluding them --delete tries (and fails, noisily) to remove the
    # remote's Syncthing markers / lost+found — e.g. a Syncthing-managed target.
    for _pat in DEST_ONLY_EXCLUDES:
        rsync_flags += ["--exclude", _pat]
    if progress:
        # --outbuf=L line-buffers rsync's output. Without it, rsync block-buffers
        # progress2 to the pipe and emits it in bursts with long gaps, which
        # trips the stall detector even though the transfer is alive.
        rsync_flags += ["--info=progress2", "--no-inc-recursive", "--outbuf=L"]
        # --out-format names each file as it is sent, so a failure can be pinned
        # to the file rsync was on rather than to a bare percentage. The names are
        # NOT written straight to the log — SyncLogWriter samples one per minute —
        # so this costs pipe traffic, not log size. --stats appends the real
        # totals (files transferred, bytes, speedup) when the run ends.
        rsync_flags += [f"--out-format={RSYNC_OUT_FORMAT}", "--stats"]

    if auth_method == "key" and ssh_key:
        fd, key_file = tempfile.mkstemp(prefix="sync_key_", suffix=".pem")
        with os.fdopen(fd, "w") as f:
            clean_key = ssh_key.replace("\r\n", "\n").replace("\r", "\n")
            f.write(clean_key)
            if not clean_key.endswith("\n"):
                f.write("\n")
        os.chmod(key_file, 0o600)
        temp_files.append(key_file)
        ssh_opts += f" -i {key_file}"
        cmd = ["/usr/bin/rsync"] + rsync_flags + ["-e", ssh_opts, backup_dir, f"{username}@{host}:{remote_path}/"]
    elif auth_method == "password" and password:
        cmd = ["sshpass", "-p", password, "/usr/bin/rsync"] + rsync_flags + ["-e", ssh_opts, backup_dir, f"{username}@{host}:{remote_path}/"]
    else:
        _cleanup_temp(temp_files)
        return None, [], _fail("config_incomplete", detail="no SSH key or password is configured")

    return cmd, temp_files, None


def _cleanup_temp(paths):
    """Remove the temp SSH key / pinned known_hosts created by _prepare_sync."""
    for path in paths or []:
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass


_PROGRESS_RE = re.compile(r"([\d,]+)\s+(\d+)%\s+([\d.]+[kKMGT]?B/s)")

# rsync exit codes (see rsync(1)), mapped to a short human reason so the log's
# failure line is self-explanatory instead of a bare number.
_RSYNC_EXIT = {
    1: "syntax or usage error",
    2: "protocol incompatibility",
    3: "errors selecting input/output files or dirs",
    5: "error starting client-server protocol",
    10: "error in socket I/O",
    11: "error in file I/O",
    12: "error in rsync protocol data stream (connection likely dropped)",
    22: "error allocating core memory buffers",
    23: "partial transfer due to error",
    24: "partial transfer due to vanished source files",
    30: "timeout in data send/receive",
    35: "timeout waiting for daemon connection",
    255: "SSH/connection error (host unreachable, auth, or link dropped)",
}


def _rsync_exit_detail(rc):
    """Human-readable reason for an rsync return code (or signal / missing status)."""
    if rc is None:
        return "no exit status (rsync output closed before it was reaped)"
    if rc < 0:
        return f"killed by signal {-rc}"
    return _RSYNC_EXIT.get(rc, "see rsync(1) exit codes")


# ---------------------------------------------------------------------------
# Failure taxonomy
# ---------------------------------------------------------------------------
# Every way a sync can fail maps to one of these stable codes. The code is what
# MQTT / webhook consumers key off — an automation can act on
# "ssh_connection_failed" without string-matching an English sentence — and the
# summary is what a human reads. Add codes; never rename one.
SYNC_REASONS = {
    # Refused before rsync ever started.
    "sync_disabled":           "sync is disabled in settings",
    "config_incomplete":       "sync configuration is incomplete (host, user or credentials missing)",
    "credentials_unavailable": "sync credentials could not be decrypted",
    "host_key_mismatch":       "the server's SSH host key did not match the pinned fingerprint",
    "network_not_allowed":     "the current network is not allowed for sync",
    "battery_low":             "battery below the sync threshold",
    "tool_missing":            "a required tool (rsync or sshpass) is not installed",
    # Aborted by our own watchdogs.
    "scan_timeout":            "rsync produced no output while building the file list",
    "stall_timeout":           "the transfer went silent and was aborted",
    "battery_abort":           "battery dropped below the threshold mid-transfer",
    "run_timeout":             "the sync exceeded its time limit",
    # rsync exited non-zero.
    "ssh_connection_failed":   "SSH/connection error (host unreachable, auth, or link dropped)",
    "rsync_protocol_error":    "rsync protocol error (the connection most likely dropped mid-transfer)",
    "socket_io_error":         "socket I/O error",
    "file_io_error":           "file I/O error (a disk on either end may be full or failing)",
    "source_selection_error":  "rsync could not read the source files or directories",
    "partial_transfer":        "the transfer completed only partially",
    "remote_timeout":          "the remote stopped responding and rsync timed out",
    "out_of_memory":           "rsync ran out of memory",
    "rsync_usage_error":       "rsync rejected its own arguments (a bug or a bad config value)",
    "killed_by_signal":        "rsync was killed by a signal (out-of-memory killer, or an external kill)",
    "no_exit_status":          "rsync's output closed before an exit status could be read",
    "rsync_error":             "rsync exited with an error",
    # Everything else.
    "internal_error":          "an unexpected error occurred inside the sync runner",
}

# rsync(1) exit code -> reason code.
_EXIT_REASON = {
    1: "rsync_usage_error",
    2: "rsync_protocol_error",
    3: "source_selection_error",
    5: "rsync_protocol_error",
    10: "socket_io_error",
    11: "file_io_error",
    12: "rsync_protocol_error",
    22: "out_of_memory",
    23: "partial_transfer",
    24: "partial_transfer",
    30: "remote_timeout",
    35: "remote_timeout",
    255: "ssh_connection_failed",
}

_SIGNAL_NAMES = {2: "SIGINT", 9: "SIGKILL", 11: "SIGSEGV", 13: "SIGPIPE", 15: "SIGTERM"}


def reason_for_exit(rc):
    """Map an rsync return code to a SYNC_REASONS key.

    ``None`` (never reaped) and negative codes (killed by signal ``-rc``) each
    get their own code rather than being folded into a generic error, because
    they point at completely different causes.
    """
    if rc is None:
        return "no_exit_status"
    if rc < 0:
        return "killed_by_signal"
    return _EXIT_REASON.get(rc, "rsync_error")


def build_failure(reason_code, exit_code=None, pct=None, bytes_transferred=None,
                  bytes_total=None, duration=None, last_file=None, diagnostics=None,
                  detail=None):
    """Build the canonical sync-failure payload.

    One object serves three consumers so they cannot drift apart: the ``[ERROR]``
    line in the sync log, the message shown on the e-ink and in the web UI, and
    the ``sync_error`` notification delivered over MQTT and webhook.

    ``message`` is assembled from only the facts actually supplied — a failure
    that never started a transfer says nothing about percentages rather than
    reporting a misleading 0%.
    """
    summary = detail or SYNC_REASONS.get(reason_code, "sync failed")
    if reason_code == "killed_by_signal" and exit_code is not None and exit_code < 0:
        sig = -exit_code
        summary = (f"rsync was killed by signal {sig} "
                   f"({_SIGNAL_NAMES.get(sig, 'unknown signal')}) — "
                   f"the out-of-memory killer, or an external kill")

    where = ""
    if pct is not None:
        where = f" at {pct}%"
        if bytes_transferred is not None and bytes_total:
            where += f" ({fmt_bytes(bytes_transferred)} of {fmt_bytes(bytes_total)})"
    when = f" after {fmt_duration(duration)}" if duration else ""
    code = ""
    if exit_code is not None:
        code = f" [exit {exit_code}]" if exit_code >= 0 else f" [signal {-exit_code}]"

    return {
        "reason_code": reason_code,
        "message": f"Sync failed{where}{when}: {summary}{code}",
        "summary": summary,
        "exit_code": exit_code,
        "percent": pct,
        "bytes_transferred": bytes_transferred,
        "bytes_total": bytes_total,
        "duration_seconds": int(duration) if duration else None,
        "last_file": last_file,
        "diagnostics": list(diagnostics or []),
        # True once the sync log already carries this failure, so a caller does
        # not write a second, identical [ERROR] line under the post-mortem.
        "logged": False,
    }


# Bookkeeping the failure dict carries for our own use, which has no meaning to
# an MQTT or webhook subscriber and is stripped before the payload goes out.
_INTERNAL_FAILURE_KEYS = ("logged",)


def notification_payload(failure):
    """The failure as it goes over MQTT and webhook.

    Everything a subscriber can act on — reason_code, exit_code, how far the
    transfer got, the post-mortem findings — minus our internal bookkeeping.
    """
    return {k: v for k, v in (failure or {}).items() if k not in _INTERNAL_FAILURE_KEYS}


def _fail(reason_code, **kw):
    """A run_sync* result dict carrying the structured failure alongside it.

    Callers hand ``result["failure"]`` to notification_payload, so the
    notification and the log line are always the same verdict.
    """
    failure = build_failure(reason_code, **kw)
    return {"success": False, "message": failure["message"],
            "duration": kw.get("duration") or 0, "failure": failure}


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def fmt_bytes(n):
    """Human byte size. Same format as the display daemon's, so one number reads
    identically on the e-ink, in the web UI and in the log."""
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def fmt_duration(seconds):
    """Compact elapsed time: ``45s``, ``12m``, ``2h05m``."""
    seconds = int(seconds or 0)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def redact_cmd(cmd):
    """Render an argv for the log with the sshpass password masked.

    The ``[CMD]`` line goes into a per-run log that the web UI serves at
    /logs/<file>, so writing ``sshpass -p <password>`` verbatim would hand the
    remote's password to anyone who can read a log. The temp key path is left
    alone: it names a 0600 file that is deleted when the sync ends.
    """
    out = list(cmd or [])
    for i, arg in enumerate(out):
        if arg == "-p" and i > 0 and "sshpass" in out[i - 1] and i + 1 < len(out):
            out[i + 1] = "***"
    return " ".join(out)


# ---------------------------------------------------------------------------
# Reading rsync's merged output stream
# ---------------------------------------------------------------------------
# stdout and stderr share one pipe, so three kinds of line arrive interleaved.
# --out-format prefixes every transferred file with this sentinel precisely so a
# file name can never be mistaken for an rsync error message, or the reverse, by
# pattern-matching alone.
NAME_SENTINEL = "@f "
# %i is rsync's itemize string; its second character is the entry's type. It is
# carried so directories and symlinks can be told apart from regular files —
# --out-format reports all three, and rsync creates a directory or a symlink
# instantly, so naming one as "the file rsync is on" would displace the real
# answer at exactly the moment it matters. Verified against rsync 3.4.1:
# ">f+++++++++" for a file, "cd+++++++++"/".d..t......" for a directory,
# "cL+++++++++" for a symlink.
RSYNC_OUT_FORMAT = "@f %i %l %n"
_REGULAR_FILE = "f"


def classify_output_line(line):
    """Split one line of rsync output into ``(kind, value)``.

    - ``("file", {"name", "size"})`` — a transferred regular file, from --out-format
    - ``("skip", None)``            — a directory, symlink or device entry
    - ``("progress", None)``        — an --info=progress2 sample
    - ``("message", line)``         — anything else: errors, warnings, --stats
    - ``("blank", None)``           — nothing worth logging

    The sentinel is tested first. A file may legitimately be named something that
    looks like a progress sample, and losing a real error message to a bad guess
    is worse than logging one redundant line.
    """
    if line is None:
        return "blank", None
    if line.startswith(NAME_SENTINEL):
        rest = line[len(NAME_SENTINEL):]
        itemize, _, tail = rest.partition(" ")
        size_txt, _, name = tail.partition(" ")
        if len(itemize) < 2 or itemize[1] != _REGULAR_FILE:
            return "skip", None
        try:
            size = int(size_txt.replace(",", ""))
        except ValueError:
            size = 0     # %l did not render a number; the name is still usable
        # Falling back through tail then rest keeps a name even if a field is
        # missing, so a malformed line degrades to a worse name, never a crash.
        return "file", {"name": (name or tail or rest).strip(), "size": size}
    if not line.strip():
        return "blank", None
    if _PROGRESS_RE.search(line):
        return "progress", None
    return "message", line


# ---------------------------------------------------------------------------
# Progress bookkeeping
# ---------------------------------------------------------------------------

class RateWindow:
    """Rolling throughput estimator over a fixed time window.

    rsync's own ``x.xMB/s`` is an instantaneous reading: on a many-small-files
    backup it swings between zero and the link speed from one sample to the next,
    so an ETA derived from it is noise. Averaging our own (time, bytes) samples
    over the last ``window_sec`` gives a number that settles.

    Reports ``0.0`` rather than a guess when it holds fewer than two samples or
    the byte count has not moved, and ``eta()`` returns ``None`` in that case: a
    stalled transfer gets "unknown", never a fabricated completion time.
    """

    def __init__(self, window_sec=600):
        self.window_sec = window_sec
        self._samples = []      # [(t, cumulative_bytes)]

    def add(self, t, byte_count):
        self._samples.append((t, byte_count))
        cutoff = t - self.window_sec
        # Keep the oldest sample that is still inside the window, plus the newest.
        while len(self._samples) > 2 and self._samples[1][0] < cutoff:
            self._samples.pop(0)

    def rate(self):
        """Bytes per second across the window, or 0.0 when not measurable."""
        if len(self._samples) < 2:
            return 0.0
        (t0, b0), (t1, b1) = self._samples[0], self._samples[-1]
        span = t1 - t0
        if span <= 0 or b1 <= b0:
            return 0.0
        return (b1 - b0) / span

    def eta(self, remaining_bytes):
        """Seconds remaining, or None when nothing is moving."""
        rate = self.rate()
        if rate <= 0 or not remaining_bytes or remaining_bytes <= 0:
            return None
        return remaining_bytes / rate


class SyncLogWriter:
    """Everything written to the per-run sync log while rsync runs.

    Owning this here rather than in each caller means the auto-sync (display
    daemon) and the manual sync (backup-sync.py) cannot drift into logging
    different things; they now only drive the e-ink and the status file.

    Throttling: a ``[SYNC]`` line goes out on every percentage change, and
    otherwise once per ``progress_interval``. That holds a long transfer to a few
    hundred lines while still proving, every minute, whether bytes are moving —
    the question a run stuck on one percent for half an hour cannot otherwise
    answer.
    """

    def __init__(self, log_file, progress_interval=60.0, file_interval=60.0,
                 window_sec=600):
        self._log = log_file
        self.progress_interval = progress_interval
        self.file_interval = file_interval
        self._rate = RateWindow(window_sec)
        self.last_file = None
        self._last_pct = None
        self._last_emit_t = None
        self._last_emit_bytes = 0
        self._last_file_logged = None
        self._last_file_t = None
        self._last_file_size = 0

    def write(self, line):
        """Write one already-formatted line. Never raises."""
        if not self._log:
            return
        try:
            self._log.write(line.rstrip("\n") + "\n")
        except Exception:
            pass

    def note_file(self, name, size=0):
        """Record the file rsync is currently sending. Cheap by design: called
        for every file, but only sampled into the log by progress()."""
        self.last_file = name
        self._last_file_size = size

    def eta(self, remaining_bytes):
        """Seconds remaining from the windowed rate, or None when unknowable.
        Shared with the UI so the e-ink, the dashboard and the log all quote the
        same estimate."""
        return self._rate.eta(remaining_bytes)

    def progress(self, now, started, pct, bytes_, total, speed):
        """Emit a throttled progress line, and at most as often the file being
        transferred. ``now`` and ``started`` are wall-clock seconds."""
        self._rate.add(now, bytes_)
        changed = pct != self._last_pct
        due = self._last_emit_t is None or (now - self._last_emit_t) >= self.progress_interval
        if not (changed or due):
            return

        parts = [f"[SYNC] {pct}%"]
        parts.append(f"{fmt_bytes(bytes_)} / {fmt_bytes(total)}" if total else fmt_bytes(bytes_))
        if speed:
            parts.append(speed)
        parts.append(f"elapsed {fmt_duration(now - started)}")
        if total:
            eta = self._rate.eta(total - bytes_)
            parts.append(f"ETA {fmt_duration(eta)}" if eta is not None else "ETA unknown")
        if self._last_emit_t is not None:
            span = int(now - self._last_emit_t)
            parts.append(f"+{fmt_bytes(bytes_ - self._last_emit_bytes)}/{span}s")
        self.write(" | ".join(parts))

        self._last_pct = pct
        self._last_emit_t = now
        self._last_emit_bytes = bytes_
        self._maybe_log_file(now)

    def _maybe_log_file(self, now):
        """Sample the current file into the log: only when it changed, and no
        more often than file_interval. rsync names every file it sends, which on
        a first sync is six figures of them — far too many to keep."""
        if not self.last_file or self.last_file == self._last_file_logged:
            return
        if self._last_file_t is not None and (now - self._last_file_t) < self.file_interval:
            return
        size = f" ({fmt_bytes(self._last_file_size)})" if self._last_file_size else ""
        self.write(f"[FILE] {self.last_file}{size}")
        self._last_file_logged = self.last_file
        self._last_file_t = now


# ---------------------------------------------------------------------------
# Post-mortem
# ---------------------------------------------------------------------------
# When rsync dies mid-transfer it often says nothing useful, or nothing at all —
# a link that drops, or a kill from outside, produces an exit code and silence.
# These probes run once, after the fact, to record the state of everything rsync
# depended on while the evidence is still fresh. Each is best-effort and returns
# a short string (or None to say nothing); none may raise, because a failing
# diagnostic must never replace the failure it is diagnosing.

def _probe_oom():
    """Whether the kernel's out-of-memory killer recently took rsync.

    The prime suspect when rsync vanishes without printing an error: the Radxa
    has little RAM, and --no-inc-recursive holds the whole file list in memory.
    """
    try:
        r = subprocess.run(["dmesg", "-T"], capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return "could not read dmesg, so an OOM kill could not be ruled out"
        hits = [ln.strip() for ln in r.stdout.splitlines()[-1000:]
                if "Out of memory" in ln or "Killed process" in ln]
        named = [h for h in hits if "rsync" in h.lower()]
        if named:
            return f"OOM killer terminated rsync: {named[-1][:200]}"
        if hits:
            return f"OOM kills in dmesg but none naming rsync: {hits[-1][:200]}"
        return "no OOM kill of rsync in recent dmesg"
    except Exception as e:
        return f"could not check dmesg for an OOM kill ({e})"


def _probe_remote(host, port, timeout=5.0):
    """Whether the remote answers on its SSH port now.

    Reachable after the fact points at something that happened mid-transfer (the
    link flapped, the server restarted sshd); still unreachable points at the
    network or the server being down for good.
    """
    if not host:
        return None
    t0 = time.time()
    try:
        socket.create_connection((host, int(port or 22)), timeout=timeout).close()
        return (f"remote {host}:{port} answers now "
                f"({int((time.time() - t0) * 1000)} ms), so the link dropped mid-transfer")
    except OSError as e:
        return f"remote {host}:{port} unreachable ({e})"


def _probe_wireguard():
    """Age of the newest WireGuard handshake, when the sync runs over the VPN.

    A handshake older than the sync's own start is strong evidence the tunnel,
    not rsync, is what failed.
    """
    try:
        cfg = _load_config().get("wireguard", {})
        if not cfg.get("enabled"):
            return None
        iface = cfg.get("interface_name", "wg0")
        import wg_manager
        ts = wg_manager.latest_handshake(iface)
        if not ts:
            return f"{iface} is up but has never completed a handshake"
        return f"{iface} last handshake {fmt_duration(time.time() - ts)} ago"
    except Exception as e:
        return f"could not read the WireGuard handshake ({e})"


def _probe_disk(path):
    """Free space on the source filesystem. A full source disk is not what stops
    an upload, but it is what stops the *next* backup, so it is worth recording
    at the same moment."""
    if not path or not hasattr(os, "statvfs"):
        return None
    try:
        st = os.statvfs(path)
        return f"local free space on {path}: {fmt_bytes(st.f_bavail * st.f_frsize)}"
    except Exception:
        return None


def collect_postmortem(context=None, probe_timeout=5.0):
    """Run every probe and return the lines worth logging.

    Ordered by how directly each one explains a dead transfer: what killed the
    process, then whether the far end is there, then the tunnel, then disk.
    """
    context = context or {}
    probes = (
        lambda: _probe_oom(),
        lambda: _probe_remote(context.get("host"), context.get("port"), probe_timeout),
        lambda: _probe_wireguard(),
        lambda: _probe_disk(context.get("backup_dir")),
    )
    out = []
    for probe in probes:
        try:
            line = probe()
        except Exception:
            line = None
        if line:
            out.append(line)
    return out


def parse_progress_line(text):
    """Parse an rsync ``--info=progress2`` chunk.

    Returns ``{"bytes", "pct", "speed", "total"}`` for the LAST progress match in
    ``text``, or ``None`` if there is none. Last, not first: progress2 separates
    its samples with CR rather than LF, so one 1024-byte read holds a whole
    burst of them and only the newest is current — taking the first made the UI
    lag a burst behind and derive its total from a stale bytes/percent pair. A
    chunk cut mid-line can't match (the regex needs the full bytes/percent/speed
    triple), so the tail is simply picked up on the next read. Pure and stateless
    so it can be unit-tested without spawning rsync.
    """
    m = None
    for m in _PROGRESS_RE.finditer(text or ""):
        pass
    if m is None:
        return None
    bytes_transferred = int(m.group(1).replace(",", ""))
    pct = int(m.group(2))
    speed = m.group(3)
    total = int(bytes_transferred * 100 / pct) if pct > 0 else 0
    return {"bytes": bytes_transferred, "pct": pct, "speed": speed, "total": total}


def _excluded_name(name):
    """True when ``name`` matches one of DEST_ONLY_EXCLUDES.

    Only the basename patterns are tested: the single path pattern
    (``.stfolder/**``) is already covered by skipping the ``.stfolder`` directory
    itself, so nothing below it is ever walked.
    """
    return any(fnmatch.fnmatch(name, pat) for pat in DEST_ONLY_EXCLUDES if "/" not in pat)


def local_tree_stats(root):
    """``(total_bytes, file_count)`` for the files rsync will carry in its list.

    The byte total is the denominator ``--info=progress2`` takes its percentage
    against: with ``--no-inc-recursive`` rsync builds the whole list up front and
    counts every regular file in it, including the ones that turn out to be up to
    date (those just complete instantly). Symlinks are not followed, matching
    ``-a``. The count is logged alongside it, because "198 GB in 104,382 files"
    explains a slow transfer that "198 GB" alone does not.

    Returns ``(0, 0)`` when the tree can't be walked at all, which callers read
    as "no exact total, fall back to the estimate". Iterative rather than
    recursive so a pathologically deep backup tree can't blow the stack.
    """
    total = 0
    count = 0
    stack = [root]
    while stack:
        try:
            entries = list(os.scandir(stack.pop()))
        except OSError:
            continue
        for e in entries:
            try:
                if _excluded_name(e.name) or e.is_symlink():
                    continue
                if e.is_dir(follow_symlinks=False):
                    stack.append(e.path)
                elif e.is_file(follow_symlinks=False):
                    total += e.stat(follow_symlinks=False).st_size
                    count += 1
            except OSError:
                continue
    return total, count


def local_tree_size(root):
    """Total size in bytes of the files rsync will carry. See local_tree_stats."""
    return local_tree_stats(root)[0]


def _resolve_min_battery(min_battery):
    """Resolve the power-aware sync threshold (config default 35; 0 disables)."""
    if min_battery is not None:
        return min_battery
    try:
        return _load_config().get("sync", {}).get("min_battery_percent", 35)
    except Exception:
        return 35


def run_sync(passphrase=None, backup_dir=None):
    """
    Run rsync to sync backups to remote server (blocking, no progress).
    Returns dict: {success, message, duration} and, on failure, the same
    structured ``failure`` payload run_sync_with_progress returns.
    """
    cmd, temp_files, err = _prepare_sync(passphrase=passphrase, backup_dir=backup_dir)
    if err:
        return err

    start = time.time()
    try:
        print(f"[SYNC] Running: {' '.join(cmd[:4])}...", flush=True)
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        duration = time.time() - start
        if r.returncode == 0:
            return {"success": True, "message": f"Sync complete ({duration:.0f}s).", "duration": duration}
        stderr = (r.stderr or "").strip()[:200]
        return _fail(reason_for_exit(r.returncode), exit_code=r.returncode, duration=duration,
                     detail=stderr or _rsync_exit_detail(r.returncode))
    except subprocess.TimeoutExpired:
        return _fail("run_timeout", duration=time.time() - start,
                     detail="the sync exceeded its 1 hour limit")
    except FileNotFoundError as e:
        tool = "sshpass" if "sshpass" in str(e) else "/usr/bin/rsync"
        return _fail("tool_missing", detail=f"{tool} is not installed")
    except Exception as e:
        return _fail("internal_error", duration=time.time() - start,
                     detail=f"unexpected error in the sync runner: {e}")
    finally:
        _cleanup_temp(temp_files)


def run_sync_with_progress(passphrase=None, backup_dir=None, on_progress=None, log_file=None,
                           min_battery=None):
    """
    Run rsync with real-time progress reporting.

    on_progress(info: dict) is called as progress updates arrive; ``info`` carries
    pct / bytes / total / speed / eta_seconds plus the scanning and stalled flags.
    It drives the e-ink and the status file only — the sync log is written here,
    so both sync paths log identically.

    log_file: optional writable file object. rsync's output is routed into it by
    kind (see classify_output_line), not teed raw.
    min_battery: power-aware abort threshold (percent). None → config default (35); 0 disables.

    Returns {success, message, duration}, and on failure a ``failure`` key: the
    structured payload (reason_code, exit_code, percent, bytes, duration,
    last_file, diagnostics) that also goes out over MQTT and webhook.
    """
    context = {}
    cmd, temp_files, err = _prepare_sync(passphrase=passphrase, backup_dir=backup_dir,
                                         progress=True, context=context)
    if err:
        return err

    min_battery = _resolve_min_battery(min_battery)

    # Exact progress denominator, measured locally while rsync builds its own
    # file list. progress2 reports only an integer percentage, so a total
    # back-computed from it (bytes * 100 / pct) sawtooths by up to total/pct: it
    # climbs while bytes grow inside one percent bucket, then drops each time the
    # percentage ticks over. Walking the source gives the real number instead.
    # On a thread because rsync's scan phase is at least as slow, so the total is
    # normally ready before the first progress line; it stays 0 if the walk loses
    # that race or fails, and the estimate covers until it lands.
    src_dir = context.get("backup_dir") or backup_dir or _load_backup_dir()
    known_total = {"bytes": 0, "files": 0, "logged": False}

    def _measure_total():
        try:
            known_total["bytes"], known_total["files"] = local_tree_stats(src_dir)
        except Exception:
            known_total["bytes"], known_total["files"] = 0, 0

    threading.Thread(target=_measure_total, daemon=True).start()

    # Two-phase watchdog:
    #   1. Initial scan phase — rsync is building the file list (--no-inc-recursive).
    #      No progress lines yet; the user just needs to know it's still working.
    #      Surface a "Building file list (Xs)" hint to dashboard/e-ink, and kill
    #      only after a generous SCAN_KILL_SEC to cover huge trees.
    #   2. Transfer phase — once we've parsed at least one progress line, switch
    #      to real stall detection: warn quickly, kill after STALL_KILL_SEC.
    SCAN_NOTIFY_SEC = 5      # how soon we tell the UI "we're scanning"
    SCAN_KILL_SEC = 1800     # 30 min — kill if rsync produces NO output at all
    # rsync's progress2 output is bursty on a many-small-files backup over SSH:
    # it can legitimately go silent for minutes between bursts (per-file overhead,
    # delete pass, remote fsync). Keep the thresholds generous so a slow-but-alive
    # transfer isn't flagged "stalled" or falsely aborted.
    STALL_WARN_SEC = 300     # 5 min — only then surface "stalled" on the UI
    STALL_KILL_SEC = 1800    # 30 min — kill only after a long, genuine silence

    BATTERY_CHECK_SEC = 30   # how often to poll the UPS for the abort guard

    start = time.time()
    proc = None
    killed_for_stall = False
    killed_for_scan = False
    killed_for_battery = False
    battery_reason = ""
    try:
        print(f"[SYNC] Running (progress): {' '.join(cmd[:4])}...", flush=True)
        # Header: what this run is actually moving, and where. Without it a log
        # read weeks later can't say which host or path a failure belongs to.
        logw = SyncLogWriter(log_file)
        dest = (f"{context.get('username', '?')}@{context.get('host', '?')}"
                f":{context.get('remote_path', '?')}/")
        logw.write(f"[SYNC] {src_dir} -> {dest}")
        logw.write(f"[CMD] {redact_cmd(cmd)}")
        # Merge stderr into stdout so a single reader sees both progress and errors.
        # Binary mode + raw fd lets us use select() reliably for stall detection.
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                bufsize=0)
        fd = proc.stdout.fileno()

        last_pct = -1
        last_bytes = 0
        last_total = 0
        last_speed = ""
        last_data_time = time.time()
        seen_progress = False       # True once we've parsed a progress line
        stall_warned = False
        scan_notified = False
        scan_start = time.time()
        last_batt_check = time.time()

        # Consume rsync's merged output a line at a time and route each line by
        # kind: progress samples are dropped (that data reaches the log through
        # the throttled [SYNC] line and the UI through on_progress), file names
        # are remembered but only sampled into the log, and everything else —
        # errors, warnings, the --stats block — is written through. Without this
        # split the log would grow by tens of KB per minute and still not say
        # which file rsync was on when it died.
        log_tail = ""

        def _tee(text):
            nonlocal log_tail
            if not text:
                return
            log_tail += text
            parts = re.split(r"[\r\n]+", log_tail)
            log_tail = parts.pop()
            for line in parts:
                kind, value = classify_output_line(line.rstrip())
                if kind == "file":
                    logw.note_file(value["name"], value["size"])
                elif kind == "message":
                    logw.write(value)

        while True:
            # Power-aware abort: if the UPS drops below the threshold (and isn't
            # charging) mid-sync, kill rsync so it doesn't get cut by PiSugar's
            # own auto-shutdown — and so --partial-dir can resume it next time.
            if min_battery and power and time.time() - last_batt_check >= BATTERY_CHECK_SEC:
                last_batt_check = time.time()
                batt_ok, batt_reason = power.sync_allowed(min_battery)
                if not batt_ok:
                    killed_for_battery = True
                    battery_reason = batt_reason
                    logw.write(f"[ABORT] {batt_reason} — killing rsync")
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    try:
                        proc.wait(timeout=5)
                    except Exception:
                        pass
                    break

            if proc.poll() is not None:
                # Drain anything still buffered
                try:
                    rest = proc.stdout.read()
                except Exception:
                    rest = b""
                if rest:
                    _tee(rest.decode("utf-8", errors="replace"))
                break

            r, _, _ = select.select([fd], [], [], 2.0)
            if r:
                try:
                    chunk_bytes = os.read(fd, 1024)
                except OSError:
                    break
                if not chunk_bytes:
                    break
                chunk = chunk_bytes.decode("utf-8", errors="replace")
                last_data_time = time.time()
                if stall_warned:
                    stall_warned = False
                    logw.write(f"[INFO] resumed receiving data from rsync after {int(time.time() - last_data_time)}s")
                _tee(chunk)
                parsed = parse_progress_line(chunk)
                if parsed:
                    bytes_transferred = parsed["bytes"]
                    pct = parsed["pct"]
                    speed = parsed["speed"]
                    total = known_total["bytes"] or parsed["total"]
                    if not seen_progress:
                        seen_progress = True
                        logw.write(f"[INFO] file list complete after {int(time.time() - scan_start)}s, transfer started")
                    # The measured tree lands asynchronously; log it once it does,
                    # so the log states the denominator the percentages are against.
                    if known_total["bytes"] and not known_total["logged"]:
                        known_total["logged"] = True
                        logw.write(f"[INFO] local tree: {fmt_bytes(known_total['bytes'])} "
                                   f"in {known_total['files']:,} files")
                    if pct != last_pct or bytes_transferred != last_bytes:
                        last_pct = pct
                        last_bytes = bytes_transferred
                        last_total = total
                        last_speed = speed
                        now = time.time()
                        # Logging lives here, not in the callers: the auto-sync and
                        # the manual sync used to format their own [SYNC] lines and
                        # could drift apart. They now only drive UI and status.
                        logw.progress(now=now, started=start, pct=pct,
                                      bytes_=bytes_transferred, total=total, speed=speed)
                        if on_progress:
                            eta = logw.eta(total - bytes_transferred) if total else None
                            on_progress({
                                "pct": pct,
                                "elapsed": now - start,
                                "bytes": bytes_transferred,
                                "total": total,
                                "speed": speed,
                                "eta_seconds": int(eta) if eta is not None else None,
                                "stalled": False,
                                "scanning": False,
                            })
            else:
                idle = time.time() - last_data_time
                scan_elapsed = int(time.time() - scan_start)
                if not seen_progress:
                    # ---- Scan phase: rsync is building the file list ----
                    if scan_elapsed >= SCAN_KILL_SEC:
                        killed_for_scan = True
                        logw.write(f"[ABORT] rsync produced no progress for {scan_elapsed}s — killing")
                        try:
                            proc.kill()
                        except Exception:
                            pass
                        try:
                            proc.wait(timeout=5)
                        except Exception:
                            pass
                        break
                    if scan_elapsed >= SCAN_NOTIFY_SEC:
                        if not scan_notified:
                            scan_notified = True
                            logw.write(f"[SCAN] still building file list ({scan_elapsed}s)")
                        if on_progress:
                            on_progress({
                                "pct": 0,
                                "elapsed": time.time() - start,
                                "bytes": 0,
                                "total": 0,
                                "speed": "",
                                "stalled": False,
                                "scanning": True,
                                "scan_seconds": scan_elapsed,
                            })
                else:
                    # ---- Transfer phase: real stall detection ----
                    if idle >= STALL_KILL_SEC:
                        killed_for_stall = True
                        logw.write(f"[STALL] no output for {int(idle)}s — killing rsync "
                                   f"(last file: {logw.last_file or 'unknown'})")
                        try:
                            proc.kill()
                        except Exception:
                            pass
                        try:
                            proc.wait(timeout=5)
                        except Exception:
                            pass
                        break
                    elif idle >= STALL_WARN_SEC:
                        if not stall_warned:
                            stall_warned = True
                            logw.write(f"[STALL] no output for {int(idle)}s at {last_pct}% "
                                       f"(last file: {logw.last_file or 'unknown'})")
                        if on_progress:
                            on_progress({
                                "pct": last_pct if last_pct >= 0 else 0,
                                "elapsed": time.time() - start,
                                "bytes": last_bytes,
                                "total": last_total,
                                "speed": last_speed,
                                "stalled": True,
                                "stalled_seconds": int(idle),
                                "scanning": False,
                            })

        # Flush any trailing buffered line (e.g. a final error without a newline).
        if log_tail.strip():
            kind, value = classify_output_line(log_tail.rstrip())
            if kind == "file":
                logw.note_file(value["name"], value["size"])
            elif kind == "message":
                logw.write(value)

        # The read loop can break on EOF (os.read -> b"") or OSError before the
        # top-of-loop proc.poll() ever observes the child's exit, leaving
        # proc.returncode == None. Reap it here so we report the real exit status
        # instead of a useless "exit None" — and so a clean exit-0 that happened
        # to end via the EOF path isn't misreported as a failure.
        if proc.returncode is None:
            try:
                proc.wait(timeout=10)
            except Exception:
                pass

        duration = time.time() - start
        pct_now = last_pct if last_pct >= 0 else None

        def _failed(reason_code, **kw):
            """Log the failure, run the probes, and return the structured result.

            The probes run here rather than in the callers so every sync path —
            auto-sync, manual sync, and anything added later — gets the same
            post-mortem, and so the notification carries the same diagnostics the
            log does.
            """
            kw.setdefault("pct", pct_now)
            kw.setdefault("bytes_transferred", last_bytes or None)
            kw.setdefault("bytes_total", last_total or None)
            kw.setdefault("duration", duration)
            kw.setdefault("last_file", logw.last_file)
            failure = build_failure(reason_code, **kw)
            failure["logged"] = bool(log_file)
            logw.write(f"[ERROR] {failure['message']}")
            if last_bytes:
                logw.write(f"[POSTMORTEM] transferred {fmt_bytes(last_bytes)}"
                           + (f" of {fmt_bytes(last_total)}" if last_total else "")
                           + f" in {fmt_duration(duration)}")
            if logw.last_file:
                logw.write(f"[POSTMORTEM] last file seen: {logw.last_file}")
            for line in collect_postmortem(context):
                logw.write(f"[POSTMORTEM] {line}")
                failure["diagnostics"].append(line)
            return {"success": False, "message": failure["message"],
                    "duration": duration, "failure": failure}

        if killed_for_battery:
            return _failed("battery_abort",
                           detail=f"{battery_reason} The partial transfer resumes next time")
        if killed_for_scan:
            return _failed("scan_timeout",
                           detail=(f"rsync produced no output for {SCAN_KILL_SEC // 60} min "
                                   f"while building the file list, and was aborted"))
        if killed_for_stall:
            return _failed("stall_timeout",
                           detail=(f"the transfer produced no output for "
                                   f"{STALL_KILL_SEC // 60} min and was aborted"))
        if proc.returncode == 0:
            if on_progress:
                on_progress({
                    "pct": 100,
                    "elapsed": duration,
                    "bytes": last_total or last_bytes,
                    "total": last_total or last_bytes,
                    "speed": last_speed,
                    "eta_seconds": 0,
                    "stalled": False,
                })
            logw.write(f"[OK] transferred {fmt_bytes(last_bytes)} in {fmt_duration(duration)}")
            return {"success": True, "message": f"Sync complete ({duration:.0f}s).",
                    "duration": duration}
        # rsync's own stderr is already in the log above this line; the failure
        # message names the code and its meaning so neither has to be looked up.
        rc = proc.returncode
        return _failed(reason_for_exit(rc), exit_code=rc, detail=_rsync_exit_detail(rc))
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except Exception:
            pass
        return _fail("run_timeout", duration=time.time() - start,
                     detail="the sync exceeded its 1 hour limit")
    except FileNotFoundError as e:
        tool = "sshpass" if "sshpass" in str(e) else "/usr/bin/rsync"
        return _fail("tool_missing", detail=f"{tool} is not installed")
    except Exception as e:
        return _fail("internal_error", duration=time.time() - start,
                     detail=f"unexpected error in the sync runner: {e}")
    finally:
        _cleanup_temp(temp_files)


def test_connection(passphrase=None):
    """
    Test SSH connection to the remote server.
    Returns dict: {success: bool, message: str}
    """
    cfg = sync_crypto.decrypt_sync_config(passphrase=passphrase)
    if not cfg:
        return {"success": False, "message": "Cannot decrypt sync credentials."}

    host = cfg.get("host", "")
    port = cfg.get("port", 22)
    username = cfg.get("username", "")
    auth_method = cfg.get("auth_method", "key")
    ssh_key = cfg.get("ssh_key", "")
    password = cfg.get("password", "")
    expected_fp = cfg.get("host_key_fingerprint", "")

    if not host or not username:
        return {"success": False, "message": "Incomplete configuration (host/user)."}

    temp_files = []
    known_hosts = None
    if expected_fp:
        known_hosts, hk_err = host_key.verify_and_write_known_hosts(host, port, expected_fp)
        if hk_err:
            return {"success": False, "message": hk_err}
        if known_hosts:
            temp_files.append(known_hosts)

    try:
        ssh_base = [
            "ssh", "-p", str(port),
        ] + host_key.strict_host_key_opts(known_hosts) + [
            "-o", "ConnectTimeout=10",
            "-o", "BatchMode=yes",
        ]

        if auth_method == "key" and ssh_key:
            fd, key_file = tempfile.mkstemp(prefix="sync_test_", suffix=".pem")
            with os.fdopen(fd, "w") as f:
                clean_key = ssh_key.replace("\r\n", "\n").replace("\r", "\n")
                f.write(clean_key)
                if not clean_key.endswith("\n"):
                    f.write("\n")
            os.chmod(key_file, 0o600)
            temp_files.append(key_file)
            ssh_base += ["-i", key_file]
            cmd = ssh_base + [f"{username}@{host}", "echo ok"]
        elif auth_method == "password" and password:
            cmd = ["sshpass", "-p", password] + ssh_base + [f"{username}@{host}", "echo ok"]
        else:
            return {"success": False, "message": "No SSH key or password configured."}

        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if r.returncode == 0 and "ok" in r.stdout:
            return {"success": True, "message": "Connection successful."}
        else:
            err = r.stderr.strip()[:200] if r.stderr else f"exit code {r.returncode}"
            return {"success": False, "message": f"Connection failed: {err}"}

    except subprocess.TimeoutExpired:
        return {"success": False, "message": "Connection timed out."}
    except FileNotFoundError as e:
        tool = "sshpass" if "sshpass" in str(e) else "ssh"
        return {"success": False, "message": f"{tool} not found."}
    except Exception as e:
        return {"success": False, "message": f"Error: {e}"}
    finally:
        _cleanup_temp(temp_files)
