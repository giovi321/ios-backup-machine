#!/usr/bin/env python3
"""
notifications.py - Send notifications via webhook and/or MQTT.

Reads notification config from the main config.yaml.
"""
import atexit, os, json, time, threading, yaml

CONFIG_PATH = os.getenv("IOSBACKUP_CONFIG", "/root/iosbackupmachine/config.yaml")

# In-flight deliveries. Both transports are dispatched on daemon threads so a
# wedged broker can never hold the process open, but Python terminates daemon
# threads at interpreter exit — and backup-sync.py calls sys.exit(0) immediately
# after reporting its result. Every notification a manual sync produced was
# therefore killed mid-POST and silently lost. Deliveries are tracked here so
# flush() can wait for them, and flush() is registered with atexit (which runs
# before daemon threads are killed) so a caller that forgets is still covered.
_pending = []
_pending_lock = threading.Lock()

# Comfortably longer than the webhook's own 10s socket timeout, so a delivery
# that is merely slow is waited out rather than truncated a second time.
FLUSH_TIMEOUT = 12.0


def _spawn(target, args):
    """Start a tracked delivery thread."""
    t = threading.Thread(target=target, args=args, daemon=True)
    with _pending_lock:
        _pending[:] = [x for x in _pending if x.is_alive()]
        _pending.append(t)
    t.start()
    return t


def flush(timeout=FLUSH_TIMEOUT):
    """Block until in-flight notifications finish, or ``timeout`` elapses.

    Returns True when everything drained. Call it before a short-lived script
    exits; long-running daemons do not need it, since their threads outlive the
    call that started them.
    """
    deadline = time.time() + timeout
    with _pending_lock:
        threads = list(_pending)
    for t in threads:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        t.join(remaining)
    drained = not any(t.is_alive() for t in threads)
    with _pending_lock:
        _pending[:] = [x for x in _pending if x.is_alive()]
    return drained


atexit.register(flush)

# ---------------------------------------------------------------------------
# Webhook auth cache
# ---------------------------------------------------------------------------
# The webhook auth header is decrypted with the iPhone's serial number, but most
# of the events that use it fire when the phone is gone: device_disconnected by
# definition, every sync_* after the phone was unplugged, and backup_complete —
# which is sent only after the e-ink has already said "Backup completed" and
# invited the user to unplug. Without a cache the header is unavailable exactly
# when it is needed, and an authenticated endpoint answers 403.
#
# /run, not /tmp and not RUNTIME_DIR: /run is a systemd tmpfs, RAM-only, wiped on
# every boot and never written to the SD card. /var/log is zram but armbian-ramlog
# syncs it to disk periodically (see logutil.py), which would put the secret on
# the card — the one thing this must not do. So the property that matters is kept:
# a powered-off device hands over nothing without the phone.
#
# Only the derived header is cached, never the passphrase. The passphrase is the
# phone's serial, which also unlocks the WireGuard and remote-sync credentials;
# caching it would widen one webhook secret into all of them.
AUTH_CACHE_DIR = os.getenv("IOSBACKUP_RUNTIME_SECRET_DIR", "/run/iosbackupmachine")
AUTH_CACHE_FILE = os.path.join(AUTH_CACHE_DIR, "webhook_auth.json")
# Seconds the cached header stays usable. 0 means "until the next reboot", which
# /run already enforces. Refreshed whenever the phone is seen.
AUTH_CACHE_TTL = int(os.getenv("IOSBACKUP_WEBHOOK_AUTH_TTL", "0"))

_log_hook = None


def set_logger(fn):
    """Route notification diagnostics into the caller's per-run log.

    Delivery problems used to exist only as a print() to the journal, so a 403 or
    an unavailable auth header was invisible in the log the web UI serves.
    """
    global _log_hook
    _log_hook = fn


def _log(msg):
    print(f"[NOTIFY] {msg}")
    if _log_hook:
        try:
            _log_hook(f"[NOTIFY] {msg}")
        except Exception:
            pass


def _write_auth_cache(headers):
    """Store the resolved header in RAM-backed runtime state, root-only."""
    try:
        os.makedirs(AUTH_CACHE_DIR, mode=0o700, exist_ok=True)
        os.chmod(AUTH_CACHE_DIR, 0o700)
        tmp = AUTH_CACHE_FILE + f".tmp.{os.getpid()}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"headers": headers, "at": time.time()}, f)
        os.replace(tmp, AUTH_CACHE_FILE)
        return True
    except Exception as e:
        _log(f"could not cache the webhook auth header: {e}")
        return False


def _read_auth_cache():
    """Return the cached header, or None when absent, expired or unreadable."""
    try:
        with open(AUTH_CACHE_FILE, "r") as f:
            data = json.load(f) or {}
    except Exception:
        return None
    headers = data.get("headers")
    if not headers:
        return None
    if AUTH_CACHE_TTL > 0 and (time.time() - float(data.get("at", 0))) > AUTH_CACHE_TTL:
        clear_auth_cache()
        return None
    return headers


def clear_auth_cache():
    """Drop the cached header. Called when notification settings change, so a
    rotated secret cannot keep being sent from cache."""
    try:
        os.remove(AUTH_CACHE_FILE)
        return True
    except FileNotFoundError:
        return True
    except Exception as e:
        _log(f"could not clear the cached webhook auth header: {e}")
        return False


def prime_auth(config=None):
    """Resolve and cache the auth header while the phone is attached.

    Called when a device is present, so the header is already in hand by the time
    backup_complete and the sync events need it. No-op when auth is not enabled.
    """
    ncfg = (config or _load_notify_config())
    wh = ncfg.get("webhook", {}) if isinstance(ncfg, dict) else {}
    if not (wh.get("enabled") and wh.get("auth_enabled")):
        return False
    headers = _resolve_auth_now(wh)
    if headers:
        _write_auth_cache(headers)
        return True
    return False


def _load_notify_config():
    try:
        with open(CONFIG_PATH, "r") as f:
            cfg = yaml.safe_load(f) or {}
    except Exception:
        cfg = {}
    return cfg.get("notifications", {})

def _send_webhook(url, payload, extra_headers=None):
    """Send a JSON POST to the webhook URL using urllib (no extra deps).

    Returns (status, error): `status` is the HTTP code — including 4xx/5xx — so a
    rejected request surfaces the real code (e.g. 401) instead of None. `error`
    is a human-readable message, or None on success.
    """
    import urllib.request, urllib.error
    try:
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if extra_headers:
            headers.update(extra_headers)
        req = urllib.request.Request(url, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, None
    except urllib.error.HTTPError as e:
        # Endpoint replied with a non-2xx — surface the real status.
        _log(f"webhook rejected: HTTP {e.code} {e.reason}")
        return e.code, f"HTTP {e.code} {e.reason}"
    except Exception as e:
        _log(f"webhook delivery failed: {e}")
        return None, str(e)


def _resolve_auth_now(wh):
    """Decrypt the auth header from the credential store, or None.

    Needs the iPhone attached in UDID passphrase mode, since the passphrase is
    the phone's serial number.
    """
    try:
        import notify_crypto
        creds = notify_crypto.decrypt_notify_config()
        if creds and creds.get("value"):
            name = wh.get("auth_header") or creds.get("header") or "Authorization"
            return {name: creds["value"]}
    except Exception as e:
        _log(f"webhook auth could not be decrypted: {e}")
    return None


def webhook_auth_headers(wh):
    """Resolve the webhook auth header.

    Three distinct outcomes, which the caller must tell apart:

    - ``{}``      no auth configured; send the request plain
    - ``{...}``   the header, freshly decrypted or from the RAM cache
    - ``None``    auth is configured but unobtainable

    ``None`` used to be reported as ``{}``, so the request went out
    unauthenticated and an authenticated endpoint answered 403 — silently, since
    no notification carries a delivery receipt. Sending blind is never right:
    the caller skips instead and says why.
    """
    if not wh.get("auth_enabled"):
        return {}

    headers = _resolve_auth_now(wh)
    if headers:
        _write_auth_cache(headers)      # refresh while the phone is here
        return headers

    cached = _read_auth_cache()
    if cached:
        return cached

    _log("webhook auth header unavailable (iPhone not attached and nothing "
         "cached) — the webhook was NOT sent")
    return None

def _send_mqtt(broker, port, username, password, topic, payload):
    """Send an MQTT message. Requires paho-mqtt."""
    try:
        import paho.mqtt.publish as publish
        auth = None
        if username:
            auth = {"username": username, "password": password or ""}
        publish.single(
            topic, payload=json.dumps(payload), hostname=broker,
            port=port, auth=auth, qos=1, retain=False
        )
        return True
    except ImportError:
        _log("paho-mqtt not installed; MQTT notifications disabled")
        return False
    except Exception as e:
        _log(f"MQTT delivery failed: {e}")
        return False

def send_notification(event, data=None):
    """
    Send notification for the given event.
    event: str like 'backup_start', 'backup_complete', 'backup_error',
           'device_connected', 'device_disconnected'
    data: optional dict with extra info
    """
    ncfg = _load_notify_config()
    payload = {
        "event": event,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source": "iosbackupmachine",
    }
    if data:
        payload.update(data)

    # Webhook
    wh = ncfg.get("webhook", {})
    if wh.get("enabled") and wh.get("url"):
        events = wh.get("events", [])
        if event in events or not events:
            extra = webhook_auth_headers(wh)
            if extra is None:
                # Auth wanted but unobtainable. Sending anyway guarantees a 403
                # and looks, from every side, exactly like nothing was sent.
                _log(f"skipped webhook for '{event}': no auth header available")
            else:
                _spawn(_send_webhook, (wh["url"], payload, extra))

    # MQTT
    mq = ncfg.get("mqtt", {})
    if mq.get("enabled") and mq.get("broker"):
        events = mq.get("events", [])
        if event in events or not events:
            topic = f"{mq.get('topic_prefix', 'iosbackupmachine')}/{event}"
            _spawn(_send_mqtt, (mq["broker"], mq.get("port", 1883),
                                mq.get("username", ""), mq.get("password", ""),
                                topic, payload))
