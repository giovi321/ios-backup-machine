---
title: "Web UI"
description: The browser interface at port 8080, its first-start wizard, live dashboard, settings pages, authentication, and the login-exempt health endpoint.
---

The web UI is where you configure the appliance and watch it work. Open it at `http://<device-ip>:8080`. On the very first boot it runs a nine-step setup wizard, then it settles into a dashboard with two live cards and a set of settings pages. A login-exempt `GET /api/health` endpoint exposes a JSON snapshot for external monitoring.

## Access

Access the web interface at `http://<device-ip>:8080`. The port is configurable in `config.yaml` under `webui.port`, and `webui.bind_interfaces` selects which network interfaces the UI listens on (options: `all`, `wifi`, `usb_iphone`).

## First-start wizard

On the very first boot, when owner info has not been configured, the web UI shows a guided setup wizard with nine steps:

1. Owner information, displayed on the e-ink screen when idle
2. WiFi (optional), for NTP sync, notifications, and remote access
3. Date & time, set manually or with automatic NTP synchronization
4. Backup directory, where backups are stored
5. Backup encryption, set directly on the iPhone (the password is never stored on this device)
6. Device filter (optional), restrict which iPhones can trigger a backup, auto-detects the connected device
7. Notifications (optional), webhook and MQTT alerts for backup events
8. Display orientation, landscape left or right
9. Web UI password (optional), protect the settings interface

The Flask session `secret_key` is generated automatically on first start and saved to `config.yaml`. No manual configuration is needed.

## Dashboard

The dashboard shows two live status cards and auto-refreshes every 5 seconds:

- Backup Status, with inline Start Backup and Stop Backup buttons. It shows percentage and encryption status while a backup is running, and stays idle while a remote sync is in progress
- Remote Sync Status, with inline Sync Now (or Cancel Sync, when active) and a Configure shortcut when sync is disabled. It shows percent, transferred and total size, current speed, and stall or scanning hints

See [Backups](../backups/) and [Remote sync](../remote-sync/) for what these cards drive.

## Settings pages

- Backup Settings: auto-start toggle, notification on rejected devices
- Device Filter: allow only specific iPhones by UDID (auto-detect connected device or manual entry)
- Encryption: enable or change backup encryption on the connected iPhone (password never stored)
- General: backup directory, display orientation, owner information
- Date & Time: manual date setting, NTP sync configuration
- WiFi: enable or disable and configure one or more networks, each with an optional nickname, plus a Scan & connect button
- Notifications: webhook URLs and MQTT broker settings (separate test buttons for webhook, MQTT, and both)
- WireGuard: upload and encrypt VPN config, start or stop the interface, auto-connect triggers, and a full-tunnel toggle
- Remote Sync: enable, configure SSH credentials (encrypted), test connection, trigger sync, set network restrictions
- Web UI: select which network interfaces the web UI listens on
- Password: protect the web UI with a password (set, change, or remove)
- Logs: browse and view backup log files from the browser, with separate live-tail links for the most recent backup and sync log

## Authentication

By default the web UI has no password. You can set one during the first-start wizard or later on the Password page. Once set, all pages require login. The password is hashed (SHA-256 plus salt) and stored in `config.yaml`. You can change or remove it at any time.

```yaml
auth:
  password_hash: ""   # auto-managed; set via web UI
```

## Health endpoint

`GET /api/health` returns a JSON snapshot for external monitoring such as Uptime Kuma, Home Assistant, or a cron check. It is login-exempt and contains no secrets: no owner info, credentials, or keys.

```json
{
  "status": "ok",
  "warnings": [],
  "version": "4.4.3",
  "time": "2026-05-30T11:13:31",
  "services": { "iosbackupmachine": "active", "webui": "active",
                "pisugar-server": "active", "usbmuxd": "active" },
  "disk":     { "root": {}, "backup": { "free": "...", "percent": 42.0 } },
  "battery":  { "percent": 62.0, "charging": false },
  "network":  { "active_ip": "192.168.1.50", "interface": "wifi",
                "wifi_ssid": "HomeNetwork", "wifi_nickname": "Home",
                "internet": true, "wireguard": {} },
  "backup":   { "state": "complete", "last_backup_time": "..." },
  "sync":     { "state": "sync_complete", "timestamp": "..." }
}
```

The `status` field is a rollup:

- `error`: a failed service, or backup disk at 95 percent or more
- `warning`: low battery, no internet, or the last backup errored
- `ok`: none of the above

:::note
`GET /api/health` is the only endpoint exempt from login, so external monitors can poll it even when a web UI password is set.
:::

## Notifications

Backup and sync events can be sent via webhook (JSON POST) and MQTT. Supported events: `backup_start`, `backup_complete`, `backup_error`, `sync_start`, `sync_complete`, `sync_error`, `device_connected`, `device_disconnected`, `device_rejected`. Configure them on the Notifications page or directly in `config.yaml`.

## Related

- [Backups](../backups/) for the backup flow the dashboard controls
- [Remote sync](../remote-sync/) for the sync card and its network restrictions
- [Security](../../architecture/security/) for password hashing and credential encryption
