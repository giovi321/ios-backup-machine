#!/usr/bin/env python3
"""
sync_manager.py - Remote backup sync via rsync over SSH.

Supports SSH key and password authentication.
Credentials are decrypted from the encrypted sync config store.
"""
import os, sys, re, subprocess, tempfile, time, yaml

import sync_crypto

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

    ssh_opts = f"ssh -p {port} -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15"
    key_file = None

    rsync_flags = ["-az", "--delete", "--rsync-path=/usr/bin/rsync"]
    if progress:
        rsync_flags.append("--info=progress2")
        rsync_flags.append("--no-inc-recursive")

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


def run_sync_with_progress(passphrase=None, backup_dir=None, on_progress=None):
    """
    Run rsync with real-time progress reporting.
    on_progress(percent: int, elapsed: float) is called as progress updates arrive.
    Returns dict: {success: bool, message: str, duration: float}
    """
    cmd, key_file, err = _prepare_sync(passphrase=passphrase, backup_dir=backup_dir, progress=True)
    if err:
        return err

    start = time.time()
    try:
        print(f"[SYNC] Running (progress): {' '.join(cmd[:4])}...", flush=True)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        last_pct = -1
        last_bytes = 0
        last_total = 0
        last_speed = ""
        while True:
            chunk = proc.stdout.read(256)
            if not chunk:
                break
            m = _PROGRESS_RE.search(chunk)
            if m and on_progress:
                bytes_transferred = int(m.group(1).replace(",", ""))
                pct = int(m.group(2))
                speed = m.group(3)
                total = int(bytes_transferred * 100 / pct) if pct > 0 else 0
                if pct != last_pct:
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
                    })

        proc.wait(timeout=3600)
        duration = time.time() - start

        if proc.returncode == 0:
            if on_progress:
                on_progress({
                    "pct": 100,
                    "elapsed": duration,
                    "bytes": last_total or last_bytes,
                    "total": last_total or last_bytes,
                    "speed": last_speed,
                })
            return {"success": True, "message": f"Sync complete ({duration:.0f}s).", "duration": duration}
        else:
            stderr = proc.stderr.read() if proc.stderr else ""
            err_msg = stderr.strip()[:200] if stderr else f"rsync exit code {proc.returncode}"
            return {"success": False, "message": f"rsync failed: {err_msg}", "duration": duration}
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
