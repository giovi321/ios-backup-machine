#!/usr/bin/env python3
"""
notifications.py — Send notifications via webhook and/or MQTT.

Reads notification config from the main config.yaml.
"""
import os, json, time, threading, yaml

CONFIG_PATH = os.getenv("IOSBACKUP_CONFIG", "/root/config.yaml")

def _load_notify_config():
    try:
        with open(CONFIG_PATH, "r") as f:
            cfg = yaml.safe_load(f) or {}
    except Exception:
        cfg = {}
    return cfg.get("notifications", {})

def _send_webhook(url, payload):
    """Send a JSON POST to the webhook URL using urllib (no extra deps)."""
    import urllib.request
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status
    except Exception as e:
        print(f"[NOTIFY] Webhook error: {e}")
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
            threading.Thread(
                target=_send_webhook, args=(wh["url"], payload), daemon=True
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
