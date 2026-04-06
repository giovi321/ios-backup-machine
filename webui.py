#!/usr/bin/env python3
"""
webui.py - Web UI for iOS Backup Machine settings.

Provides a web interface to configure:
- General settings (date, backup dir, owner info)
- WiFi
- NTP
- Notifications (webhook, MQTT)
- WireGuard client
- Web UI interface binding
All configuration is saved directly to config.yaml.
"""
import os, sys, time, subprocess, json, secrets, yaml, copy, hashlib, glob, plistlib
from functools import wraps

from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, jsonify, send_from_directory, session
)

import netutil
import wg_crypto
import wg_manager
import sync_crypto
import sync_manager

VERSION = "2.0"

CONFIG_PATH = os.getenv("IOSBACKUP_CONFIG", "/root/iosbackupmachine/config.yaml")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webui_static")
TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webui_templates")

app = Flask(
    __name__,
    template_folder=TEMPLATE_DIR,
    static_folder=STATIC_DIR,
    static_url_path="/static"
)

@app.context_processor
def inject_version():
    return {"app_version": VERSION}

LOG_DIR = "/var/log/iosbackupmachine"

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def load_config():
    with open(CONFIG_PATH, "r") as f:
        cfg = yaml.safe_load(f) or {}
    _apply_defaults(cfg)
    return cfg

def _apply_defaults(cfg):
    cfg.setdefault("backup_dir", "/media/iosbackup/")
    cfg.setdefault("marker_file", ".foldermarker")
    cfg.setdefault("disk_device", "/dev/mmcblk1")
    cfg.setdefault("orientation", "landscape_right")
    cfg.setdefault("font_path", "/root/iosbackupmachine/UbuntuMono-Regular.ttf")
    cfg.setdefault("owner_lines", ["Name", "telephone", "email", "message"])
    cfg.setdefault("error_codes", {})
    cfg.setdefault("env", {})
    cfg.setdefault("auth", {"password_hash": ""})
    cfg.setdefault("backup", {"auto_start": True, "notify_on_rejected": True})
    cfg.setdefault("backup_encryption", {"encryption_confirmed": False})
    cfg.setdefault("device_filter", {"enabled": False, "allowed_devices": []})
    cfg.setdefault("wifi", {"enabled": False, "ssid": "", "password": ""})
    cfg.setdefault("ntp", {"enabled": True, "servers": ["pool.ntp.org", "time.google.com"]})
    cfg.setdefault("webui", {"enabled": True, "port": 8080, "bind_interfaces": ["all"], "secret_key": "change-me"})
    cfg.setdefault("notifications", {
        "webhook": {"enabled": False, "url": "", "events": ["backup_complete", "backup_error"]},
        "mqtt": {"enabled": False, "broker": "", "port": 1883, "username": "", "password": "", "topic_prefix": "iosbackupmachine", "events": ["backup_complete", "backup_error"]},
    })
    cfg.setdefault("wireguard", {"enabled": False, "interface_name": "wg0"})
    cfg.setdefault("sync", {"enabled": False, "auto_sync": False})
    cfg.setdefault("setup_completed", False)

# ---------------------------------------------------------------------------
# Authentication helpers
# ---------------------------------------------------------------------------

def _hash_password(password):
    """Hash a password using SHA-256 + salt (no bcrypt dependency needed)."""
    salt = secrets.token_hex(16)
    h = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
    return f"{salt}${h}"

def _verify_password(password, stored_hash):
    """Verify password against stored salt$hash."""
    if not stored_hash or "$" not in stored_hash:
        return False
    salt, h = stored_hash.split("$", 1)
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest() == h

def _auth_enabled():
    cfg = load_config()
    return bool(cfg.get("auth", {}).get("password_hash", ""))

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if _setup_needed():
            return redirect(url_for("setup"))
        if _auth_enabled() and not session.get("authenticated"):
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return decorated

def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

# ---------------------------------------------------------------------------
# First-start / secret-key helpers
# ---------------------------------------------------------------------------

_PLACEHOLDER_KEYS = {"", "change-me", "change-me-to-a-random-string"}

def _ensure_secret_key():
    """
    If the secret_key in config.yaml is a placeholder (or empty),
    generate a cryptographically random one and persist it.
    Returns the (possibly new) secret key.
    """
    cfg = load_config()
    webui = cfg.get("webui", {})
    current = webui.get("secret_key", "")
    if current in _PLACEHOLDER_KEYS:
        new_key = secrets.token_hex(32)
        webui["secret_key"] = new_key
        cfg["webui"] = webui
        save_config(cfg)
        print(f"[WEBUI] Auto-generated new secret key.")
        return new_key
    return current

def _setup_needed():
    """Return True if the guided first-start wizard should be shown."""
    cfg = load_config()
    return not cfg.get("setup_completed", False)

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/setup", methods=["GET", "POST"])
def setup():
    """Guided first-start wizard."""
    cfg = load_config()
    if not _setup_needed():
        return redirect(url_for("index"))

    # Detect connected iPhone for encryption & device filter steps
    connected_udid = wg_crypto.get_iphone_udid()
    connected_name = ""
    if connected_udid:
        try:
            r = subprocess.run(["ideviceinfo", "-k", "DeviceName"],
                               capture_output=True, text=True, timeout=5)
            connected_name = r.stdout.strip() if r.returncode == 0 else ""
        except Exception:
            pass

    current_time = time.strftime("%Y-%m-%dT%H:%M")

    if request.method == "POST":
        # --- Step 1: Owner info ---
        owner = []
        for i in range(4):
            owner.append(request.form.get(f"owner_line_{i}", ""))
        cfg["owner_lines"] = owner

        # --- Step 2: WiFi ---
        wifi = cfg.get("wifi", {})
        wifi["enabled"] = request.form.get("wifi_enabled") == "on"
        wifi["ssid"] = request.form.get("wifi_ssid", "")
        wifi["password"] = request.form.get("wifi_password", "")
        cfg["wifi"] = wifi
        if wifi["enabled"] and wifi["ssid"]:
            _apply_wifi(wifi["ssid"], wifi["password"])

        # --- Step 3: Date/Time & NTP ---
        new_dt = request.form.get("setup_datetime", "").strip()
        if new_dt and new_dt != current_time:
            try:
                subprocess.run(["date", "-s", new_dt.replace("T", " ")],
                               capture_output=True, text=True, timeout=5)
                # Update RTC
                try:
                    import socket
                    s = socket.create_connection(("127.0.0.1", 8423), timeout=5)
                    s.sendall(b"rtc_pi2rtc\n")
                    time.sleep(0.5)
                    s.close()
                except Exception:
                    pass
                flash("Date/time updated.", "success")
            except Exception as e:
                flash(f"Failed to set date: {e}", "warning")

        ntp = cfg.get("ntp", {})
        ntp["enabled"] = request.form.get("ntp_enabled") == "on"
        servers = request.form.get("ntp_servers", "").strip().splitlines()
        ntp["servers"] = [s.strip() for s in servers if s.strip()]
        cfg["ntp"] = ntp

        # --- Step 4: Backup directory ---
        bd = request.form.get("backup_dir", "").strip()
        if bd:
            cfg["backup_dir"] = bd

        # --- Step 5: Backup encryption ---
        enc_pw = request.form.get("encryption_password", "").strip()
        if enc_pw:
            if connected_udid:
                try:
                    r = subprocess.run(
                        ["idevicebackup2", "encryption", "on", enc_pw,
                         cfg.get("backup_dir", "/media/iosbackup/")],
                        capture_output=True, text=True, timeout=60
                    )
                    out = r.stdout + r.stderr
                    if r.returncode == 0 or "enabled" in out.lower():
                        enc = cfg.get("backup_encryption", {})
                        enc["encryption_confirmed"] = True
                        cfg["backup_encryption"] = enc
                        flash("Backup encryption enabled on your iPhone.", "success")
                    else:
                        flash(f"Could not enable encryption: {out[:150]}. "
                              "Go to Encryption settings later to retry.", "warning")
                except subprocess.TimeoutExpired:
                    flash("Encryption command timed out. Make sure iPhone is unlocked. "
                          "You can retry from the Encryption page.", "warning")
                except Exception as e:
                    flash(f"Encryption error: {e}. You can retry from the Encryption page.", "warning")
            else:
                flash("No iPhone connected - encryption password was NOT stored. "
                      "Connect your iPhone and visit the Encryption page to enable it.", "warning")

        # --- Step 6: Device filter ---
        df = cfg.get("device_filter", {})
        df["enabled"] = request.form.get("filter_enabled") == "on"
        if connected_udid and request.form.get("add_connected_device") == "on":
            allowed = df.get("allowed_devices", [])
            existing_udids = [d.get("udid", "") for d in allowed]
            if connected_udid not in existing_udids:
                name = connected_name or "iPhone"
                allowed.append({"udid": connected_udid, "name": name})
                df["allowed_devices"] = allowed
                flash(f"Added device: {name} ({connected_udid})", "success")
        cfg["device_filter"] = df

        # --- Step 7: Notifications ---
        notif = cfg.get("notifications", {})
        # Webhook
        wh = notif.get("webhook", {})
        wh["enabled"] = request.form.get("webhook_enabled") == "on"
        wh["url"] = request.form.get("webhook_url", "")
        wh_events = request.form.getlist("webhook_events")
        if wh_events:
            wh["events"] = wh_events
        notif["webhook"] = wh
        # MQTT
        mq = notif.get("mqtt", {})
        mq["enabled"] = request.form.get("mqtt_enabled") == "on"
        mq["broker"] = request.form.get("mqtt_broker", "")
        try:
            mq["port"] = int(request.form.get("mqtt_port", "1883"))
        except ValueError:
            mq["port"] = 1883
        mq["username"] = request.form.get("mqtt_username", "")
        mq["password"] = request.form.get("mqtt_password", "")
        mq["topic_prefix"] = request.form.get("mqtt_topic_prefix", "iosbackupmachine")
        mq_events = request.form.getlist("mqtt_events")
        if mq_events:
            mq["events"] = mq_events
        notif["mqtt"] = mq
        cfg["notifications"] = notif

        # --- Step 8: Orientation ---
        orient = request.form.get("orientation", "")
        if orient:
            cfg["orientation"] = orient

        # --- Step 9: Web UI password ---
        pw = request.form.get("password", "").strip()
        if pw:
            confirm = request.form.get("confirm_password", "")
            if pw != confirm:
                flash("Passwords do not match.", "error")
                return render_template("setup.html", cfg=cfg,
                                       connected_udid=connected_udid,
                                       connected_name=connected_name,
                                       current_time=current_time)
            if len(pw) < 4:
                flash("Password must be at least 4 characters.", "error")
                return render_template("setup.html", cfg=cfg,
                                       connected_udid=connected_udid,
                                       connected_name=connected_name,
                                       current_time=current_time)
            auth = cfg.get("auth", {})
            auth["password_hash"] = _hash_password(pw)
            cfg["auth"] = auth
            session["authenticated"] = True

        cfg["setup_completed"] = True
        save_config(cfg)
        flash("Setup complete! Your iOS Backup Machine is ready.", "success")
        return redirect(url_for("index"))

    return render_template("setup.html", cfg=cfg,
                           connected_udid=connected_udid,
                           connected_name=connected_name,
                           current_time=current_time)

@app.route("/login", methods=["GET", "POST"])
def login():
    if _setup_needed():
        return redirect(url_for("setup"))
    if not _auth_enabled():
        return redirect(url_for("index"))
    if request.method == "POST":
        pw = request.form.get("password", "")
        cfg = load_config()
        stored = cfg.get("auth", {}).get("password_hash", "")
        if _verify_password(pw, stored):
            session["authenticated"] = True
            next_url = request.args.get("next", url_for("index"))
            return redirect(next_url)
        flash("Incorrect password.", "error")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.pop("authenticated", None)
    flash("Logged out.", "success")
    return redirect(url_for("login"))

@app.route("/")
@login_required
def index():
    cfg = load_config()
    ip, iface_type = netutil.get_active_ip()
    wg_status = wg_manager.get_wireguard_status(cfg.get("wireguard", {}).get("interface_name", "wg0"))
    return render_template("index.html", cfg=cfg, ip=ip, iface_type=iface_type, wg_status=wg_status)

@app.route("/favicon.ico")
def favicon():
    return send_from_directory(STATIC_DIR, "favicon.ico", mimetype="image/x-icon")

# --- General Settings ---
@app.route("/settings/general", methods=["GET", "POST"])
@login_required
def settings_general():
    cfg = load_config()
    if request.method == "POST":
        cfg["backup_dir"] = request.form.get("backup_dir", cfg["backup_dir"])
        cfg["marker_file"] = request.form.get("marker_file", cfg["marker_file"])
        cfg["disk_device"] = request.form.get("disk_device", cfg["disk_device"])
        cfg["orientation"] = request.form.get("orientation", cfg["orientation"])
        cfg["font_path"] = request.form.get("font_path", cfg["font_path"])
        # Owner lines
        owner = []
        for i in range(4):
            owner.append(request.form.get(f"owner_line_{i}", ""))
        cfg["owner_lines"] = owner
        save_config(cfg)
        flash("General settings saved.", "success")
        return redirect(url_for("settings_general"))
    return render_template("settings_general.html", cfg=cfg)

# --- Date/Time ---
@app.route("/settings/datetime", methods=["GET", "POST"])
@login_required
def settings_datetime():
    cfg = load_config()
    current_time = time.strftime("%Y-%m-%dT%H:%M")
    if request.method == "POST":
        action = request.form.get("action", "")
        if action == "set_date":
            new_dt = request.form.get("datetime", "")
            if new_dt:
                try:
                    subprocess.run(["date", "-s", new_dt.replace("T", " ")],
                                   capture_output=True, text=True, timeout=5)
                    # Update RTC
                    try:
                        import socket
                        s = socket.create_connection(("127.0.0.1", 8423), timeout=5)
                        s.sendall(b"rtc_pi2rtc\n")
                        time.sleep(0.5)
                        s.close()
                    except Exception:
                        pass
                    flash("Date/time updated.", "success")
                except Exception as e:
                    flash(f"Failed to set date: {e}", "error")
        elif action == "ntp_sync":
            try:
                r = subprocess.run(
                    [sys.executable, os.path.join(os.path.dirname(__file__), "ntp-sync.py")],
                    capture_output=True, text=True, timeout=30,
                    env={**os.environ, "IOSBACKUP_CONFIG": CONFIG_PATH}
                )
                if r.returncode == 0:
                    flash("NTP sync completed.", "success")
                else:
                    flash(f"NTP sync failed: {r.stderr[:200]}", "error")
            except Exception as e:
                flash(f"NTP sync error: {e}", "error")
        elif action == "save_ntp":
            ntp = cfg.get("ntp", {})
            ntp["enabled"] = request.form.get("ntp_enabled") == "on"
            servers = request.form.get("ntp_servers", "").strip().splitlines()
            ntp["servers"] = [s.strip() for s in servers if s.strip()]
            cfg["ntp"] = ntp
            save_config(cfg)
            flash("NTP settings saved.", "success")
        return redirect(url_for("settings_datetime"))
    return render_template("settings_datetime.html", cfg=cfg, current_time=current_time)

# --- WiFi ---
@app.route("/settings/wifi", methods=["GET", "POST"])
@login_required
def settings_wifi():
    cfg = load_config()
    if request.method == "POST":
        wifi = cfg.get("wifi", {})
        wifi["enabled"] = request.form.get("wifi_enabled") == "on"
        wifi["ssid"] = request.form.get("ssid", "")
        wifi["password"] = request.form.get("password", "")
        cfg["wifi"] = wifi
        save_config(cfg)
        # Apply WiFi settings
        if wifi["enabled"] and wifi["ssid"]:
            _apply_wifi(wifi["ssid"], wifi["password"])
        flash("WiFi settings saved.", "success")
        return redirect(url_for("settings_wifi"))
    wifi_ip = netutil.get_wifi_ip()
    return render_template("settings_wifi.html", cfg=cfg, wifi_ip=wifi_ip)

def _apply_wifi(ssid, password):
    """Apply WiFi settings using nmcli."""
    try:
        # Remove old connection if exists
        subprocess.run(["nmcli", "con", "delete", "iosbackup-wifi"],
                       capture_output=True, timeout=10)
        cmd = ["nmcli", "dev", "wifi", "connect", ssid]
        if password:
            cmd += ["password", password]
        cmd += ["name", "iosbackup-wifi"]
        subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except Exception as e:
        print(f"[WEBUI] WiFi apply error: {e}")

# --- Notifications ---
@app.route("/settings/notifications", methods=["GET", "POST"])
@login_required
def settings_notifications():
    cfg = load_config()
    if request.method == "POST":
        notif = cfg.get("notifications", {})
        # Webhook
        wh = notif.get("webhook", {})
        wh["enabled"] = request.form.get("webhook_enabled") == "on"
        wh["url"] = request.form.get("webhook_url", "")
        wh_events = request.form.getlist("webhook_events")
        wh["events"] = wh_events
        notif["webhook"] = wh
        # MQTT
        mq = notif.get("mqtt", {})
        mq["enabled"] = request.form.get("mqtt_enabled") == "on"
        mq["broker"] = request.form.get("mqtt_broker", "")
        try:
            mq["port"] = int(request.form.get("mqtt_port", "1883"))
        except ValueError:
            mq["port"] = 1883
        mq["username"] = request.form.get("mqtt_username", "")
        mq["password"] = request.form.get("mqtt_password", "")
        mq["topic_prefix"] = request.form.get("mqtt_topic_prefix", "iosbackupmachine")
        mq_events = request.form.getlist("mqtt_events")
        mq["events"] = mq_events
        notif["mqtt"] = mq
        cfg["notifications"] = notif
        save_config(cfg)
        flash("Notification settings saved.", "success")
        return redirect(url_for("settings_notifications"))
    return render_template("settings_notifications.html", cfg=cfg)

@app.route("/settings/notifications/test", methods=["POST"])
@login_required
def test_notification():
    """Send a test notification."""
    from notifications import send_notification
    send_notification("test", {"message": "Test notification from iOS Backup Machine"})
    flash("Test notification sent.", "success")
    return redirect(url_for("settings_notifications"))

# --- WireGuard ---
@app.route("/settings/wireguard", methods=["GET", "POST"])
@login_required
def settings_wireguard():
    cfg = load_config()
    wg = cfg.get("wireguard", {})
    iface = wg.get("interface_name", "wg0")
    wg_status = wg_manager.get_wireguard_status(iface)
    udid = wg_crypto.get_iphone_udid()
    has_enc_file = os.path.exists(wg_crypto.ENC_FILE)

    if request.method == "POST":
        action = request.form.get("action", "")
        if action == "save_settings":
            wg["enabled"] = request.form.get("wg_enabled") == "on"
            wg["interface_name"] = request.form.get("interface_name", "wg0")
            cfg["wireguard"] = wg
            save_config(cfg)
            flash("WireGuard settings saved.", "success")
        elif action == "upload_config":
            wg_conf = request.form.get("wg_config", "")
            if wg_conf.strip():
                if not udid:
                    flash("No iPhone connected. Connect iPhone to encrypt config.", "error")
                else:
                    if wg_crypto.encrypt_wg_config({"wg_conf": wg_conf}, udid=udid):
                        flash("WireGuard config encrypted and saved.", "success")
                    else:
                        flash("Failed to encrypt WireGuard config.", "error")
            else:
                flash("Empty WireGuard config.", "error")
        elif action == "start":
            if wg_manager.start_wireguard(iface, udid=udid):
                flash(f"WireGuard interface {iface} started.", "success")
            else:
                flash("Failed to start WireGuard.", "error")
        elif action == "stop":
            if wg_manager.stop_wireguard(iface):
                flash(f"WireGuard interface {iface} stopped.", "success")
            else:
                flash("Failed to stop WireGuard.", "error")
        elif action == "backup_key":
            key = wg_crypto.backup_encryption_key(udid=udid)
            if key:
                flash(f"Key backed up. Key: {key}", "success")
            else:
                flash("No iPhone connected. Cannot backup key.", "error")
        return redirect(url_for("settings_wireguard"))
    return render_template("settings_wireguard.html",
                           cfg=cfg, wg_status=wg_status, udid=udid, has_enc_file=has_enc_file)

# --- Remote Sync ---
@app.route("/settings/sync", methods=["GET", "POST"])
@login_required
def settings_sync():
    cfg = load_config()
    sync = cfg.get("sync", {})
    udid = wg_crypto.get_iphone_udid()
    has_enc_file = os.path.exists(sync_crypto.ENC_FILE)

    if request.method == "POST":
        action = request.form.get("action", "")
        if action == "save_settings":
            sync["enabled"] = request.form.get("sync_enabled") == "on"
            sync["auto_sync"] = request.form.get("auto_sync") == "on"
            cfg["sync"] = sync
            save_config(cfg)
            flash("Sync settings saved.", "success")
        elif action == "upload_credentials":
            if not udid:
                flash("No iPhone connected. Connect iPhone to encrypt credentials.", "error")
            else:
                host = request.form.get("host", "").strip()
                port = request.form.get("port", "22").strip()
                username = request.form.get("username", "").strip()
                auth_method = request.form.get("auth_method", "key")
                ssh_key = request.form.get("ssh_key", "")
                password = request.form.get("password", "")
                remote_path = request.form.get("remote_path", "").strip()
                if not host or not username or not remote_path:
                    flash("Host, username, and remote path are required.", "error")
                elif auth_method == "key" and not ssh_key.strip():
                    flash("SSH private key is required for key authentication.", "error")
                elif auth_method == "password" and not password:
                    flash("Password is required for password authentication.", "error")
                else:
                    try:
                        port_int = int(port)
                    except ValueError:
                        port_int = 22
                    cred = {
                        "host": host,
                        "port": port_int,
                        "username": username,
                        "auth_method": auth_method,
                        "ssh_key": ssh_key if auth_method == "key" else "",
                        "password": password if auth_method == "password" else "",
                        "remote_path": remote_path,
                    }
                    if sync_crypto.encrypt_sync_config(cred, udid=udid):
                        flash("Sync credentials encrypted and saved.", "success")
                    else:
                        flash("Failed to encrypt sync credentials.", "error")
        elif action == "test_connection":
            result = sync_manager.test_connection(udid=udid)
            if result["success"]:
                flash(result["message"], "success")
            else:
                flash(result["message"], "error")
        elif action == "run_sync":
            result = sync_manager.run_sync(udid=udid)
            if result["success"]:
                flash(result["message"], "success")
            else:
                flash(result["message"], "error")
        elif action == "backup_key":
            key = sync_crypto.backup_encryption_key(udid=udid)
            if key:
                flash(f"Key backed up. Key: {key}", "success")
            else:
                flash("No iPhone connected. Cannot backup key.", "error")
        return redirect(url_for("settings_sync"))
    return render_template("settings_sync.html",
                           cfg=cfg, udid=udid, has_enc_file=has_enc_file)

# --- Web UI Interface Binding ---
@app.route("/settings/webui", methods=["GET", "POST"])
@login_required
def settings_webui():
    cfg = load_config()
    if request.method == "POST":
        webui = cfg.get("webui", {})
        try:
            webui["port"] = int(request.form.get("port", "8080"))
        except ValueError:
            webui["port"] = 8080
        bind = request.form.getlist("bind_interfaces")
        webui["bind_interfaces"] = bind if bind else ["all"]
        webui["secret_key"] = request.form.get("secret_key", webui.get("secret_key", ""))
        cfg["webui"] = webui
        save_config(cfg)
        flash("Web UI settings saved. Restart the service to apply binding changes.", "success")
        return redirect(url_for("settings_webui"))
    interfaces = netutil.get_all_interfaces()
    return render_template("settings_webui.html", cfg=cfg, interfaces=interfaces)

# --- Password Management ---
@app.route("/settings/password", methods=["GET", "POST"])
@login_required
def settings_password():
    cfg = load_config()
    has_password = bool(cfg.get("auth", {}).get("password_hash", ""))
    if request.method == "POST":
        action = request.form.get("action", "")
        if action == "set_password":
            current = request.form.get("current_password", "")
            new_pw = request.form.get("new_password", "")
            confirm = request.form.get("confirm_password", "")
            stored = cfg.get("auth", {}).get("password_hash", "")
            # If password is already set, verify current
            if stored and not _verify_password(current, stored):
                flash("Current password is incorrect.", "error")
                return redirect(url_for("settings_password"))
            if not new_pw:
                flash("New password cannot be empty.", "error")
                return redirect(url_for("settings_password"))
            if new_pw != confirm:
                flash("New passwords do not match.", "error")
                return redirect(url_for("settings_password"))
            if len(new_pw) < 4:
                flash("Password must be at least 4 characters.", "error")
                return redirect(url_for("settings_password"))
            auth = cfg.get("auth", {})
            auth["password_hash"] = _hash_password(new_pw)
            cfg["auth"] = auth
            save_config(cfg)
            session["authenticated"] = True
            flash("Password updated successfully.", "success")
        elif action == "remove_password":
            current = request.form.get("current_password", "")
            stored = cfg.get("auth", {}).get("password_hash", "")
            if stored and not _verify_password(current, stored):
                flash("Current password is incorrect.", "error")
                return redirect(url_for("settings_password"))
            auth = cfg.get("auth", {})
            auth["password_hash"] = ""
            cfg["auth"] = auth
            save_config(cfg)
            flash("Password removed. Web UI is now open.", "success")
        return redirect(url_for("settings_password"))
    return render_template("settings_password.html", cfg=cfg, has_password=has_password)

# --- Device Filter ---
@app.route("/settings/devices", methods=["GET", "POST"])
@login_required
def settings_devices():
    cfg = load_config()
    df = cfg.get("device_filter", {})
    # Detect currently connected device
    connected_udid = wg_crypto.get_iphone_udid()
    connected_name = ""
    if connected_udid:
        try:
            r = subprocess.run(["ideviceinfo", "-k", "DeviceName"],
                               capture_output=True, text=True, timeout=5)
            connected_name = r.stdout.strip() if r.returncode == 0 else ""
        except Exception:
            pass

    if request.method == "POST":
        action = request.form.get("action", "")
        if action == "save_filter":
            df["enabled"] = request.form.get("filter_enabled") == "on"
            cfg["device_filter"] = df
            save_config(cfg)
            flash("Device filter settings saved.", "success")
        elif action == "add_connected":
            if not connected_udid:
                flash("No iPhone connected.", "error")
            else:
                allowed = df.get("allowed_devices", [])
                existing_udids = [d.get("udid", "") for d in allowed]
                if connected_udid in existing_udids:
                    flash(f"Device {connected_udid} is already in the list.", "warning")
                else:
                    name = connected_name or request.form.get("device_name", "Unknown")
                    allowed.append({"udid": connected_udid, "name": name})
                    df["allowed_devices"] = allowed
                    cfg["device_filter"] = df
                    save_config(cfg)
                    flash(f"Added device: {name} ({connected_udid})", "success")
        elif action == "add_manual":
            udid = request.form.get("manual_udid", "").strip()
            name = request.form.get("manual_name", "").strip() or "Manual entry"
            if not udid:
                flash("UDID cannot be empty.", "error")
            else:
                allowed = df.get("allowed_devices", [])
                existing_udids = [d.get("udid", "") for d in allowed]
                if udid in existing_udids:
                    flash(f"Device {udid} is already in the list.", "warning")
                else:
                    allowed.append({"udid": udid, "name": name})
                    df["allowed_devices"] = allowed
                    cfg["device_filter"] = df
                    save_config(cfg)
                    flash(f"Added device: {name} ({udid})", "success")
        elif action == "remove_device":
            rm_udid = request.form.get("remove_udid", "")
            allowed = df.get("allowed_devices", [])
            df["allowed_devices"] = [d for d in allowed if d.get("udid") != rm_udid]
            cfg["device_filter"] = df
            save_config(cfg)
            flash(f"Device {rm_udid} removed.", "success")
        return redirect(url_for("settings_devices"))
    return render_template("settings_devices.html", cfg=cfg, df=df,
                           connected_udid=connected_udid, connected_name=connected_name)

# --- Backup Settings ---
@app.route("/settings/backup", methods=["GET", "POST"])
@login_required
def settings_backup():
    cfg = load_config()
    if request.method == "POST":
        bk = cfg.get("backup", {})
        bk["auto_start"] = request.form.get("auto_start") == "on"
        bk["notify_on_rejected"] = request.form.get("notify_on_rejected") == "on"
        cfg["backup"] = bk
        save_config(cfg)
        flash("Backup settings saved.", "success")
        return redirect(url_for("settings_backup"))
    return render_template("settings_backup.html", cfg=cfg)

# --- Backup Encryption ---
@app.route("/settings/encryption", methods=["GET", "POST"])
@login_required
def settings_encryption():
    cfg = load_config()
    enc = cfg.get("backup_encryption", {})
    connected_udid = wg_crypto.get_iphone_udid()

    # Check current encryption status on device
    enc_status = None
    if connected_udid:
        try:
            r = subprocess.run(
                ["idevicebackup2", "-i", "encryption", cfg.get("backup_dir", "/media/iosbackup/")],
                capture_output=True, text=True, timeout=10
            )
            out = r.stdout + r.stderr
            if "Backup encryption is currently enabled" in out or "will be encrypted" in out.lower():
                enc_status = "enabled"
            elif "Backup encryption is currently disabled" in out or "not encrypted" in out.lower():
                enc_status = "disabled"
        except Exception:
            pass

    if request.method == "POST":
        action = request.form.get("action", "")
        if action == "enable_encryption":
            pw = request.form.get("enable_password", "").strip()
            if not pw:
                flash("Please provide an encryption password.", "error")
            elif not connected_udid:
                flash("No iPhone connected. Connect your iPhone first.", "error")
            else:
                try:
                    r = subprocess.run(
                        ["idevicebackup2", "encryption", "on", pw, cfg.get("backup_dir", "/media/iosbackup/")],
                        capture_output=True, text=True, timeout=60
                    )
                    out = r.stdout + r.stderr
                    if r.returncode == 0 or "enabled" in out.lower():
                        enc["encryption_confirmed"] = True
                        cfg["backup_encryption"] = enc
                        save_config(cfg)
                        flash("Backup encryption enabled on device. The password was NOT stored - remember it for restores.", "success")
                    else:
                        flash(f"Failed to enable encryption: {out[:200]}", "error")
                except subprocess.TimeoutExpired:
                    flash("Command timed out. Make sure the iPhone is unlocked and trusted.", "error")
                except Exception as e:
                    flash(f"Error enabling encryption: {e}", "error")
        elif action == "change_password":
            old_pw = request.form.get("old_password", "").strip()
            new_pw = request.form.get("new_password", "").strip()
            if not old_pw or not new_pw:
                flash("Both old and new passwords are required.", "error")
            elif not connected_udid:
                flash("No iPhone connected. Connect your iPhone first.", "error")
            else:
                try:
                    r = subprocess.run(
                        ["idevicebackup2", "changepw", old_pw, new_pw, cfg.get("backup_dir", "/media/iosbackup/")],
                        capture_output=True, text=True, timeout=60
                    )
                    out = r.stdout + r.stderr
                    if r.returncode == 0:
                        flash("Encryption password changed on device. The new password was NOT stored - remember it for restores.", "success")
                    else:
                        flash(f"Failed to change password: {out[:200]}", "error")
                except Exception as e:
                    flash(f"Error changing password: {e}", "error")
        return redirect(url_for("settings_encryption"))
    return render_template("settings_encryption.html",
                           cfg=cfg, enc=enc, connected_udid=connected_udid, enc_status=enc_status)

# --- Backup List ---

def _dir_size(path):
    """Return total size in bytes of all files under *path*."""
    total = 0
    try:
        for dirpath, _dirnames, filenames in os.walk(path):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                try:
                    total += os.path.getsize(fp)
                except OSError:
                    pass
    except OSError:
        pass
    return total

def _human_size(nbytes):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(nbytes) < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} PB"

def _parse_info_plist(plist_path):
    """Parse an iOS backup Info.plist and return a dict of useful fields."""
    info = {}
    try:
        with open(plist_path, "rb") as fp:
            pl = plistlib.load(fp)
        info["device_name"] = pl.get("Device Name", "")
        info["display_name"] = pl.get("Display Name", "")
        info["product_type"] = pl.get("Product Type", "")
        info["product_version"] = pl.get("Product Version", "")
        info["serial_number"] = pl.get("Serial Number", "")
        info["udid"] = pl.get("Target Identifier", "") or pl.get("Unique Identifier", "")
        last_backup = pl.get("Last Backup Date")
        if last_backup:
            info["last_backup"] = last_backup.strftime("%Y-%m-%d %H:%M:%S")
            info["last_backup_ts"] = last_backup.timestamp()
        else:
            info["last_backup"] = ""
            info["last_backup_ts"] = 0
    except Exception:
        pass
    return info

@app.route("/backups")
@login_required
def backups():
    cfg = load_config()
    backup_dir = cfg.get("backup_dir", "/media/iosbackup/")
    backup_list = []
    if os.path.isdir(backup_dir):
        for entry in os.listdir(backup_dir):
            entry_path = os.path.join(backup_dir, entry)
            if not os.path.isdir(entry_path):
                continue
            info_plist = os.path.join(entry_path, "Info.plist")
            if not os.path.exists(info_plist):
                continue
            info = _parse_info_plist(info_plist)
            # Folder modification time as fallback date
            try:
                stat = os.stat(entry_path)
                folder_mtime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime))
                folder_mtime_ts = stat.st_mtime
            except OSError:
                folder_mtime = ""
                folder_mtime_ts = 0
            size_bytes = _dir_size(entry_path)
            # Check for Manifest.plist (indicates a completed backup)
            has_manifest = os.path.exists(os.path.join(entry_path, "Manifest.plist"))
            # Check for Status.plist snapshot state
            status_plist = os.path.join(entry_path, "Status.plist")
            snapshot_state = ""
            if os.path.exists(status_plist):
                try:
                    with open(status_plist, "rb") as fp:
                        st = plistlib.load(fp)
                    snapshot_state = st.get("SnapshotState", "")
                except Exception:
                    pass
            backup_list.append({
                "folder": entry,
                "device_name": info.get("display_name") or info.get("device_name") or entry,
                "product_type": info.get("product_type", ""),
                "ios_version": info.get("product_version", ""),
                "serial": info.get("serial_number", ""),
                "udid": info.get("udid", entry),
                "last_backup": info.get("last_backup") or folder_mtime,
                "sort_ts": info.get("last_backup_ts") or folder_mtime_ts,
                "size": _human_size(size_bytes),
                "size_bytes": size_bytes,
                "status": "complete" if (has_manifest and snapshot_state != "uploading") else "incomplete",
                "snapshot_state": snapshot_state,
            })
    # Sort newest first
    backup_list.sort(key=lambda b: b["sort_ts"], reverse=True)
    return render_template("backups.html", cfg=cfg, backups=backup_list, backup_dir=backup_dir)

# --- Log Viewer ---
@app.route("/logs")
@login_required
def logs():
    log_files = []
    if os.path.isdir(LOG_DIR):
        for f in sorted(glob.glob(os.path.join(LOG_DIR, "*.log")), reverse=True):
            stat = os.stat(f)
            log_files.append({
                "name": os.path.basename(f),
                "size": stat.st_size,
                "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
            })
    return render_template("logs.html", log_files=log_files)

@app.route("/logs/<filename>")
@login_required
def view_log(filename):
    # Sanitize filename
    safe = os.path.basename(filename)
    path = os.path.join(LOG_DIR, safe)
    content = ""
    if os.path.isfile(path):
        try:
            with open(path, "r", errors="replace") as f:
                content = f.read()
        except Exception as e:
            content = f"Error reading log: {e}"
    else:
        content = "Log file not found."
    return render_template("log_view.html", filename=safe, content=content)

@app.route("/logs/<filename>/delete", methods=["POST"])
@login_required
def delete_log(filename):
    safe = os.path.basename(filename)
    path = os.path.join(LOG_DIR, safe)
    if os.path.isfile(path):
        try:
            os.remove(path)
            flash(f"Deleted {safe}.", "success")
        except Exception as e:
            flash(f"Failed to delete {safe}: {e}", "error")
    else:
        flash(f"Log file not found: {safe}", "error")
    return redirect(url_for("logs"))

@app.route("/logs/purge", methods=["POST"])
@login_required
def purge_logs():
    count = 0
    if os.path.isdir(LOG_DIR):
        for f in glob.glob(os.path.join(LOG_DIR, "*.log")):
            try:
                os.remove(f)
                count += 1
            except Exception:
                pass
    flash(f"Purged {count} log file(s).", "success")
    return redirect(url_for("logs"))

# --- API endpoints ---
@app.route("/api/status")
@login_required
def api_status():
    cfg = load_config()
    ip, iface_type = netutil.get_active_ip()
    wg = cfg.get("wireguard", {})
    return jsonify({
        "ip": ip,
        "interface": iface_type,
        "wifi_ip": netutil.get_wifi_ip(),
        "usb_iphone_ip": netutil.get_usb_iphone_ip(),
        "wireguard": wg_manager.get_wireguard_status(wg.get("interface_name", "wg0")),
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
    })

@app.route("/api/encryption-key")
@login_required
def api_encryption_key():
    """API endpoint to retrieve the encryption key (requires iPhone connected)."""
    udid = wg_crypto.get_iphone_udid()
    if not udid:
        return jsonify({"error": "No iPhone connected"}), 400
    key = wg_crypto.derive_key(udid).hex()
    return jsonify({"udid": udid, "key": key})

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def create_app():
    app.secret_key = _ensure_secret_key()
    return app

def main():
    # Auto-generate secret key if placeholder
    app.secret_key = _ensure_secret_key()

    cfg = load_config()
    webui_cfg = cfg.get("webui", {})
    port = webui_cfg.get("port", 8080)
    bind = netutil.get_bind_address(webui_cfg.get("bind_interfaces", ["all"]))

    print(f"[WEBUI] Starting on {bind}:{port}")
    app.run(host=bind, port=port, debug=False)

if __name__ == "__main__":
    main()
