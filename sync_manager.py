#!/usr/bin/env python3
"""
sync_manager.py - Remote backup sync via rsync over SSH.

Supports SSH key and password authentication.
Credentials are decrypted from the encrypted sync config store.
"""
import os, sys, subprocess, tempfile, time, yaml

import sync_crypto

CONFIG_PATH = os.getenv("IOSBACKUP_CONFIG", "/root/config.yaml")


def _load_backup_dir():
    try:
        with open(CONFIG_PATH, "r") as f:
            cfg = yaml.safe_load(f) or {}
        return cfg.get("backup_dir", "/media/iosbackup/")
    except Exception:
        return "/media/iosbackup/"


def run_sync(udid=None, backup_dir=None):
    """
    Run rsync to sync backups to remote server.
    Returns dict: {success: bool, message: str, duration: float}
    """
    cfg = sync_crypto.decrypt_sync_config(udid=udid)
    if not cfg:
        return {"success": False, "message": "Cannot decrypt sync credentials. Is iPhone connected?", "duration": 0}

    host = cfg.get("host", "")
    port = cfg.get("port", 22)
    username = cfg.get("username", "")
    auth_method = cfg.get("auth_method", "key")
    ssh_key = cfg.get("ssh_key", "")
    password = cfg.get("password", "")
    remote_path = cfg.get("remote_path", "")

    if not host or not username or not remote_path:
        return {"success": False, "message": "Incomplete sync configuration (host/user/path).", "duration": 0}

    if backup_dir is None:
        backup_dir = _load_backup_dir()

    # Ensure trailing slash for rsync
    if not backup_dir.endswith("/"):
        backup_dir += "/"

    ssh_opts = f"ssh -p {port} -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15"
    key_file = None
    start = time.time()

    try:
        if auth_method == "key" and ssh_key:
            # Write temp key file
            fd, key_file = tempfile.mkstemp(prefix="sync_key_", suffix=".pem")
            with os.fdopen(fd, "w") as f:
                f.write(ssh_key)
                if not ssh_key.endswith("\n"):
                    f.write("\n")
            os.chmod(key_file, 0o600)

            ssh_opts += f" -i {key_file}"
            cmd = [
                "rsync", "-az", "--delete",
                "-e", ssh_opts,
                backup_dir,
                f"{username}@{host}:{remote_path}/"
            ]
        elif auth_method == "password" and password:
            cmd = [
                "sshpass", "-p", password,
                "rsync", "-az", "--delete",
                "-e", ssh_opts,
                backup_dir,
                f"{username}@{host}:{remote_path}/"
            ]
        else:
            return {"success": False, "message": "No SSH key or password configured.", "duration": 0}

        print(f"[SYNC] Running: rsync to {username}@{host}:{remote_path}/", flush=True)
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        duration = time.time() - start

        if r.returncode == 0:
            return {"success": True, "message": f"Sync complete ({duration:.0f}s).", "duration": duration}
        else:
            err = r.stderr.strip()[:200] if r.stderr else f"rsync exit code {r.returncode}"
            return {"success": False, "message": f"rsync failed: {err}", "duration": duration}

    except subprocess.TimeoutExpired:
        return {"success": False, "message": "Sync timed out (1h limit).", "duration": time.time() - start}
    except FileNotFoundError as e:
        tool = "sshpass" if "sshpass" in str(e) else "rsync"
        return {"success": False, "message": f"{tool} not found. Install it.", "duration": 0}
    except Exception as e:
        return {"success": False, "message": f"Sync error: {e}", "duration": time.time() - start}
    finally:
        if key_file and os.path.exists(key_file):
            try:
                os.remove(key_file)
            except Exception:
                pass


def test_connection(udid=None):
    """
    Test SSH connection to the remote server.
    Returns dict: {success: bool, message: str}
    """
    cfg = sync_crypto.decrypt_sync_config(udid=udid)
    if not cfg:
        return {"success": False, "message": "Cannot decrypt sync credentials. Is iPhone connected?"}

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
                f.write(ssh_key)
                if not ssh_key.endswith("\n"):
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
        if key_file and os.path.exists(key_file):
            try:
                os.remove(key_file)
            except Exception:
                pass
