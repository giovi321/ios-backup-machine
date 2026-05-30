#!/usr/bin/env python3
"""
sync_manager.py - Remote backup sync via rsync over SSH.

Supports SSH key and password authentication.
Credentials are decrypted from the encrypted sync config store.
"""
import os, sys, re, select, subprocess, tempfile, time, yaml

import sync_crypto

try:
    import power
except ImportError:
    power = None

CONFIG_PATH = os.getenv("IOSBACKUP_CONFIG", "/root/iosbackupmachine/config.yaml")


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
                r = subprocess.run(["iwgetid", "-r"], capture_output=True, text=True, timeout=5)
                current_ssid = r.stdout.strip()
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


def _prepare_sync(passphrase=None, backup_dir=None, progress=False):
    """Shared setup for run_sync and run_sync_with_progress.
    Returns (cmd, key_file, error_dict) — error_dict is set on failure."""
    net_ok, net_reason = _check_network_allowed()
    if not net_ok:
        return None, None, {"success": False, "message": net_reason, "duration": 0}

    cfg = sync_crypto.decrypt_sync_config(passphrase=passphrase)
    if not cfg:
        return None, None, {"success": False, "message": "Cannot decrypt sync credentials.", "duration": 0}

    host = cfg.get("host", "")
    port = cfg.get("port", 22)
    username = cfg.get("username", "")
    auth_method = cfg.get("auth_method", "key")
    ssh_key = cfg.get("ssh_key", "")
    password = cfg.get("password", "")
    remote_path = cfg.get("remote_path", "")

    if not host or not username or not remote_path:
        return None, None, {"success": False, "message": "Incomplete sync configuration (host/user/path).", "duration": 0}

    if backup_dir is None:
        backup_dir = _load_backup_dir()
    if not backup_dir.endswith("/"):
        backup_dir += "/"

    # ServerAliveInterval/CountMax detects dead connections in ~90s instead of
    # waiting for the TCP-level keepalive (default 2h).
    ssh_opts = (f"ssh -p {port} -o StrictHostKeyChecking=accept-new "
                f"-o ConnectTimeout=15 -o ServerAliveInterval=30 -o ServerAliveCountMax=3")
    key_file = None

    # -a (archive) but NOT -z: iOS backups are encrypted / already-compressed, so
    # gzip gains ~nothing and just burns the Radxa's weak CPU (and can bottleneck
    # the transfer). --partial + --partial-dir keep incomplete files in a stable
    # dir on the remote so a reboot mid-sync resumes; rsync excludes it from --delete.
    rsync_flags = ["-a", "--delete", "--partial", "--partial-dir=.rsync-partial",
                   "--rsync-path=/usr/bin/rsync"]
    if progress:
        # --outbuf=L line-buffers rsync's output. Without it, rsync block-buffers
        # progress2 to the pipe and emits it in bursts with long gaps, which
        # trips the stall detector even though the transfer is alive.
        rsync_flags += ["--info=progress2", "--no-inc-recursive", "--outbuf=L"]

    if auth_method == "key" and ssh_key:
        fd, key_file = tempfile.mkstemp(prefix="sync_key_", suffix=".pem")
        with os.fdopen(fd, "w") as f:
            clean_key = ssh_key.replace("\r\n", "\n").replace("\r", "\n")
            f.write(clean_key)
            if not clean_key.endswith("\n"):
                f.write("\n")
        os.chmod(key_file, 0o600)
        ssh_opts += f" -i {key_file}"
        cmd = ["/usr/bin/rsync"] + rsync_flags + ["-e", ssh_opts, backup_dir, f"{username}@{host}:{remote_path}/"]
    elif auth_method == "password" and password:
        cmd = ["sshpass", "-p", password, "/usr/bin/rsync"] + rsync_flags + ["-e", ssh_opts, backup_dir, f"{username}@{host}:{remote_path}/"]
    else:
        return None, None, {"success": False, "message": "No SSH key or password configured.", "duration": 0}

    return cmd, key_file, None


def _cleanup_key(key_file):
    if key_file and os.path.exists(key_file):
        try:
            os.remove(key_file)
        except Exception:
            pass


_PROGRESS_RE = re.compile(r"([\d,]+)\s+(\d+)%\s+([\d.]+[kKMGT]?B/s)")


def parse_progress_line(text):
    """Parse an rsync ``--info=progress2`` chunk.

    Returns ``{"bytes", "pct", "speed", "total"}`` for the first progress match
    in ``text``, or ``None`` if there is none. Pure and stateless so it can be
    unit-tested without spawning rsync.
    """
    m = _PROGRESS_RE.search(text or "")
    if not m:
        return None
    bytes_transferred = int(m.group(1).replace(",", ""))
    pct = int(m.group(2))
    speed = m.group(3)
    total = int(bytes_transferred * 100 / pct) if pct > 0 else 0
    return {"bytes": bytes_transferred, "pct": pct, "speed": speed, "total": total}


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
    Returns dict: {success: bool, message: str, duration: float}
    """
    cmd, key_file, err = _prepare_sync(passphrase=passphrase, backup_dir=backup_dir)
    if err:
        return err

    start = time.time()
    try:
        print(f"[SYNC] Running: {' '.join(cmd[:4])}...", flush=True)
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        duration = time.time() - start
        if r.returncode == 0:
            return {"success": True, "message": f"Sync complete ({duration:.0f}s).", "duration": duration}
        else:
            err_msg = r.stderr.strip()[:200] if r.stderr else f"rsync exit code {r.returncode}"
            return {"success": False, "message": f"rsync failed: {err_msg}", "duration": duration}
    except subprocess.TimeoutExpired:
        return {"success": False, "message": "Sync timed out (1h limit).", "duration": time.time() - start}
    except FileNotFoundError as e:
        tool = "sshpass" if "sshpass" in str(e) else "/usr/bin/rsync"
        return {"success": False, "message": f"{tool} not found. Install it.", "duration": 0}
    except Exception as e:
        return {"success": False, "message": f"Sync error: {e}", "duration": time.time() - start}
    finally:
        _cleanup_key(key_file)


def run_sync_with_progress(passphrase=None, backup_dir=None, on_progress=None, log_file=None,
                           min_battery=None):
    """
    Run rsync with real-time progress reporting.
    on_progress(info: dict) is called as progress updates arrive.
    log_file: optional writable file object — raw rsync output (stdout+stderr) is teed to it.
    min_battery: power-aware abort threshold (percent). None → config default (35); 0 disables.
    Returns dict: {success: bool, message: str, duration: float}
    """
    cmd, key_file, err = _prepare_sync(passphrase=passphrase, backup_dir=backup_dir, progress=True)
    if err:
        return err

    min_battery = _resolve_min_battery(min_battery)

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
        if log_file:
            try:
                log_file.write(f"[CMD] {' '.join(cmd)}\n")
            except Exception:
                pass
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

        # Tee rsync output to the log a line at a time, dropping the progress-bar
        # updates (the "1,234  45%  1.2MB/s" spam) — that data is already surfaced
        # via on_progress. Keeps file names and errors so the log stays useful
        # without growing by tens of KB per minute.
        log_tail = ""

        def _tee(text):
            nonlocal log_tail
            if not log_file or not text:
                return
            log_tail += text
            parts = re.split(r"[\r\n]+", log_tail)
            log_tail = parts.pop()
            for line in parts:
                line = line.rstrip()
                if line and not _PROGRESS_RE.search(line):
                    try:
                        log_file.write(line + "\n")
                    except Exception:
                        pass

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
                    if log_file:
                        log_file.write(f"[ABORT] {batt_reason} — killing rsync\n")
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
                if rest and log_file:
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
                    if log_file:
                        log_file.write("[INFO] resumed receiving data from rsync\n")
                _tee(chunk)
                parsed = parse_progress_line(chunk)
                if parsed and on_progress:
                    bytes_transferred = parsed["bytes"]
                    pct = parsed["pct"]
                    speed = parsed["speed"]
                    total = parsed["total"]
                    if not seen_progress:
                        seen_progress = True
                        if log_file:
                            log_file.write(f"[INFO] file list complete after {int(time.time() - scan_start)}s, transfer started\n")
                    if pct != last_pct or bytes_transferred != last_bytes:
                        last_pct = pct
                        last_bytes = bytes_transferred
                        last_total = total
                        last_speed = speed
                        on_progress({
                            "pct": pct,
                            "elapsed": time.time() - start,
                            "bytes": bytes_transferred,
                            "total": total,
                            "speed": speed,
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
                        if log_file:
                            log_file.write(f"[ABORT] rsync produced no progress for {scan_elapsed}s — killing\n")
                        try:
                            proc.kill()
                        except Exception:
                            pass
                        try:
                            proc.wait(timeout=5)
                        except Exception:
                            pass
                        break
                    if scan_elapsed >= SCAN_NOTIFY_SEC and on_progress:
                        if not scan_notified:
                            scan_notified = True
                            if log_file:
                                log_file.write(f"[SCAN] still building file list ({scan_elapsed}s)\n")
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
                        if log_file:
                            log_file.write(f"[STALL] no output for {int(idle)}s — killing rsync\n")
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
                            if log_file:
                                log_file.write(f"[STALL] no output for {int(idle)}s\n")
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
        if log_file and log_tail.strip() and not _PROGRESS_RE.search(log_tail):
            try:
                log_file.write(log_tail.rstrip() + "\n")
            except Exception:
                pass

        duration = time.time() - start

        if killed_for_battery:
            return {"success": False,
                    "message": f"Sync aborted: {battery_reason} Will resume next time.",
                    "duration": duration}
        if killed_for_scan:
            mins = SCAN_KILL_SEC // 60
            return {"success": False,
                    "message": f"No progress {mins} min, aborted.",
                    "duration": duration}
        if killed_for_stall:
            mins = STALL_KILL_SEC // 60
            return {"success": False,
                    "message": f"Sync stalled {mins} min, aborted.",
                    "duration": duration}
        if proc.returncode == 0:
            if on_progress:
                on_progress({
                    "pct": 100,
                    "elapsed": duration,
                    "bytes": last_total or last_bytes,
                    "total": last_total or last_bytes,
                    "speed": last_speed,
                    "stalled": False,
                })
            return {"success": True, "message": f"Sync complete ({duration:.0f}s).", "duration": duration}
        else:
            # stderr was merged into stdout and written to log_file already
            return {"success": False, "message": f"rsync failed (exit {proc.returncode}). See sync log.", "duration": duration}
    except subprocess.TimeoutExpired:
        proc.kill()
        return {"success": False, "message": "Sync timed out (1h limit).", "duration": time.time() - start}
    except FileNotFoundError as e:
        tool = "sshpass" if "sshpass" in str(e) else "/usr/bin/rsync"
        return {"success": False, "message": f"{tool} not found. Install it.", "duration": 0}
    except Exception as e:
        return {"success": False, "message": f"Sync error: {e}", "duration": time.time() - start}
    finally:
        _cleanup_key(key_file)


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

    if not host or not username:
        return {"success": False, "message": "Incomplete configuration (host/user)."}

    key_file = None
    try:
        ssh_base = [
            "ssh", "-p", str(port),
            "-o", "StrictHostKeyChecking=accept-new",
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
        _cleanup_key(key_file)
