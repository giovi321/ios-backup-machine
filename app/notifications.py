#!/usr/bin/env python3
"""
notifications.py - Send notifications via webhook and/or MQTT.

Reads notification config from the main config.yaml.
"""
import os, json, time, threading, yaml

CONFIG_PATH = os.getenv("IOSBACKUP_CONFIG", "/root/iosbackupmachine/config.yaml")

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
        print(f"[NOTIFY] Webhook HTTP {e.code} {e.reason}")
        return e.code, f"HTTP {e.code} {e.reason}"
    except Exception as e:
        print(f"[NOTIFY] Webhook error: {e}")
        return None, str(e)


def webhook_auth_headers(wh):
    """Return the configured webhook auth header as {name: value}, decrypted from
    the credential store, or {} if not configured / not decryptable (e.g. UDID
    mode with no iPhone connected — the request then goes out unauthenticated)."""
    if not wh.get("auth_enabled"):
        return {}
    try:
        import notify_crypto
        creds = notify_crypto.decrypt_notify_config()
        if creds and creds.get("value"):
            name = wh.get("auth_header") or creds.get("header") or "Authorization"
            return {name: creds["value"]}
    except Exception as e:
        print(f"[NOTIFY] webhook auth unavailable: {e}")
    return {}

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
        print("[NOTIFY] paho-mqtt not installed. MQTT notifications disabled.")
        return False
    except Exception as e:
        print(f"[NOTIFY] MQTT error: {e}")
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
            threading.Thread(
                target=_send_webhook, args=(wh["url"], payload, extra), daemon=True
            ).start()

    # MQTT
    mq = ncfg.get("mqtt", {})
    if mq.get("enabled") and mq.get("broker"):
        events = mq.get("events", [])
        if event in events or not events:
            topic = f"{mq.get('topic_prefix', 'iosbackupmachine')}/{event}"
            threading.Thread(
                target=_send_mqtt,
                args=(mq["broker"], mq.get("port", 1883),
                      mq.get("username", ""), mq.get("password", ""),
                      topic, payload),
                daemon=True
            ).start()
