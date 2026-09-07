---
title: "Web UI"
description: The browser interface at port 8080, its first-start wizard, live dashboard, settings pages, authentication, and the login-exempt health endpoint.
---

The web UI is where you configure the appliance and watch it work. Open it at `http://<device-ip>:8080`. On the very first boot it runs a nine-step setup wizard, then it settles into a dashboard with three live cards and a set of settings pages. A login-exempt `GET /api/health` endpoint exposes a JSON snapshot for external monitoring.

## Access

Access the web interface at `http://<device-ip>:8080`. The port is configurable in `config.yaml` under `webui.port`, and `webui.bind_interfaces` selects which network interfaces the UI listens on (options: `all`, `wifi`, `usb_iphone`).

## First-start wizard

On the very first boot, when owner info has not been configured, the web UI shows a guided setup wizard. It opens with an unnumbered step asking you to plug the iPhone in and pair it, then nine numbered ones:

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

The dashboard shows three live status cards, each refreshing every 5 seconds, above static System, Storage, and Quick Actions cards and the Reboot / Shut Down buttons:

- Network, showing whether the internet is reachable, which link is carrying it (WiFi with its nickname, or the iPhone's USB hotspot), whether WireGuard is up, and how long the current state has held. The appliance switches links on its own, so how long the current one has lasted sits next to whether it is up. The probe runs on its own schedule and the page reads a cached answer, so polling costs nothing no matter how many tabs are open
- Backup Status, with inline Start Backup and Stop Backup buttons. It shows percentage and encryption status while a backup is running, and stays idle while a remote sync is in progress
- Remote Sync Status, with inline Sync Now (or Cancel Sync, when active) and a Configure shortcut when sync is disabled. It shows percent, transferred and total size, current speed, an ETA, and stall or scanning hints

See [Backups](../backups/) and [Remote sync](../remote-sync/) for what these cards drive.

## Reboot, shutdown, and update while work is running

Reboot, shutdown, and a system update are the three actions that end work in flight. The first two cut the power out from under `idevicebackup2`. The updater stops every service, and the display daemon's `KillMode=mixed` reaps the `idevicebackup2` child along with it. A backup killed that way leaves a half-copied snapshot the next run has to redo, and no `[INTERRUPT]` line in the run log to say why.

All three refuse once while a backup or a sync is running, and offer a button to do it anyway. Taking the override stops the work the sanctioned way first: it drops the stop sentinel the daemon watches and waits up to 10 seconds for `idevicebackup2` to exit, so the run log records why it ended, then it continues.

The refusal is never a hard block. The offer rides on the `?confirm=` query argument of the page you land on, so it disappears as soon as you navigate anywhere else and there is no server-side state to expire or leak between browsers. The busy check itself fails open, because a wedged `pgrep` must not be able to lock you out of powering the device off.

## Pages

The sidebar groups the pages under Backup, Settings, and Tools.

Backup:

- Backup Settings: auto-start toggle, notification on rejected devices. The backup guards and timeouts are config-file only; see [Backups](../backups/#where-these-settings-live)
- Backup List: one row per backup folder on the drive, with the device name, its iOS version, the last backup time, the size (measured in the background), whether the backup completed, and its remote-sync state
- Device Filter: allow only specific iPhones by UDID (auto-detect connected device or manual entry)
- Encryption: enable or change backup encryption on the connected iPhone (password never stored)

Settings:

- General: backup directory, display orientation, owner information, and config export and import
- Date & Time: manual date setting, NTP sync configuration
- WiFi: enable or disable and configure one or more networks, each with an optional nickname, plus a Scan & connect button
- Notifications: webhook URLs and MQTT broker settings (separate test buttons for webhook, MQTT, and both)
- Remote Sync: enable, configure SSH credentials (encrypted), test connection, trigger sync, set network restrictions, and the overall time limit
- WireGuard: upload and encrypt VPN config, start or stop the interface, auto-connect triggers, and a full-tunnel toggle
- Web UI: select which network interfaces the web UI listens on
- Password: protect the web UI with a password (set, change, or remove)

Tools:

- Logs: browse the log files, with separate live-tail links for the most recent backup and sync log, and a viewer for the systemd journal. See [Logs](../logs/)
- Update: check for a new release and install it, with the output following in `iosbackup-update`. See [Installation](../../getting-started/installation/#updating)

## Authentication

By default the web UI has no password. You can set one during the first-start wizard or later on the Password page. Once set, all pages require login. The password is hashed (SHA-256 plus salt) and stored in `config.yaml`. You can change or remove it at any time.

```yaml
auth:
  password_hash: ""   # auto-managed; set via web UI
```

## Config export and import

The General page exports `config.yaml` as a download and imports one back. An import is migrated and default-filled before it is saved, so a file exported from an older release comes back on the current schema.

The import also reports what it had to correct. A wrong-typed value is silently swapped for its default during the merge, and the corrected tree is what gets saved, so without the report you would never learn which of your settings the import dropped. The first 10 corrections appear as banners and the full list goes to `webui.log`.

Uploads are capped at 256 KB. This import is the only upload in the UI and an exported config is a few KB, so the cap is there for the case where the wrong file gets picked in the browser. Without it, a photo or a backup archive would be read whole into RAM and handed to the YAML parser on a board with 512 MB of it.

## When config.yaml is unreadable

A `config.yaml` that cannot be parsed, or that parses to something other than a settings mapping, does not stop the appliance. The loader falls back to the built-in defaults, keeps the original next to it as `config.yaml.bad-<timestamp>`, and the web UI shows a banner saying so, with the specific problems under Details. The saved settings are gone in that case, including the flag that records setup as finished and the password hash, so the first-start wizard re-engages as the recovery path rather than leaving an open UI that pretends setup was done.

A single mistyped key is handled more narrowly: that value is replaced with its default, a warning is recorded, and the rest of the file is kept.

## Health endpoint

`GET /api/health` returns a JSON snapshot for external monitoring such as Uptime Kuma, Home Assistant, or a cron check. It is login-exempt and contains no secrets: no owner info, credentials, or keys.

```json
{
  "status": "ok",
  "warnings": [],
  "version": "4.10.0",
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

The `status` field is a rollup, and `warnings` names each condition that contributed:

- `error`: `iosbackupmachine.service` or `webui.service` failed, or the backup disk is at 95 percent or more
- `warning`: battery below `sync.min_battery_percent` while not charging, no internet, the last backup errored, or no backup has succeeded for `backup.stale_after_sec` (reported as `no backup in N days`)
- `ok`: none of the above

The stale warning is the pull half of the quiet-device alert described in [Backups](../backups/#the-quiet-device-alert). A poller reading this endpoint catches both a device that is up but quiet and a device that has stopped answering, and the second of those is something the device can never report about itself.

:::note
`GET /api/health` is the only endpoint exempt from login, so external monitors can poll it even when a web UI password is set.
:::

## Notifications

Backup and sync events can be sent via webhook (JSON POST) and MQTT. Supported events: `backup_start`, `backup_complete`, `backup_error`, `backup_stale`, `sync_start`, `sync_complete`, `sync_error`, `device_connected`, `device_disconnected`, `device_rejected`. Configure them on the Notifications page or directly in `config.yaml`. An empty event list means every event.

A `backup_error` carries its own `reason_code` drawn from the backup guards and timeouts; see [Backups](../backups/#notifications).

### The `sync_error` payload

A failed sync sends the same verdict its log records, structured so an automation can act on it without matching English:

```json
{
  "event": "sync_error",
  "timestamp": "2026-05-14T22:15:14+0200",
  "source": "iosbackupmachine",
  "error": "Sync failed at 40% (20.0 GB of 50.0 GB) after 1h10m: SSH/connection error (host unreachable, auth, or link dropped) [exit 255]",
  "reason_code": "ssh_connection_failed",
  "summary": "SSH/connection error (host unreachable, auth, or link dropped)",
  "exit_code": 255,
  "percent": 40,
  "bytes_transferred": 21474836480,
  "bytes_total": 53687091200,
  "duration_seconds": 4200,
  "last_file": "00008030-.../Snapshot/9c/9c11ab04",
  "diagnostics": [
    "no OOM kill of rsync in recent dmesg",
    "remote 192.168.1.50:22 unreachable ([Errno 110] Connection timed out)",
    "wg0 last handshake 12m ago"
  ]
}
```

`reason_code` is the field to branch on. It is stable: codes are added, never renamed. The current set:

| Group | Codes |
|---|---|
| Never started | `sync_disabled`, `config_incomplete`, `credentials_unavailable`, `host_key_mismatch`, `network_not_allowed`, `battery_low`, `tool_missing` |
| Aborted by a watchdog | `scan_timeout`, `stall_timeout`, `battery_abort`, `run_timeout` |
| rsync exited non-zero | `ssh_connection_failed`, `rsync_protocol_error`, `socket_io_error`, `file_io_error`, `source_selection_error`, `partial_transfer`, `remote_timeout`, `out_of_memory`, `rsync_usage_error`, `killed_by_signal`, `no_exit_status`, `rsync_error` |
| Anything else | `internal_error` |

`error` repeats `message` so an existing consumer reading `error` keeps working. Fields the failure does not know are `null`: a sync that never started reports no percentage rather than a misleading `0`.

### Delivery

Webhook and MQTT deliveries run on background threads so a slow endpoint never holds up a backup or a sync. Short-lived callers wait for them before exiting, via `notifications.flush()`; that call is also registered with `atexit`, so a delivery cannot be cut short by the process ending.

Delivery is best-effort and is not retried. A notification reporting a network failure has to travel over the network that just failed, so a `sync_error` caused by a dead link may not arrive. The sync log on the device is the authoritative record; see [Logs](../logs/).

### Webhook auth and the iPhone

The optional webhook auth header is encrypted with the iPhone's serial number, so decrypting it needs the phone attached. Most events that carry it do not fire while it is: `device_disconnected` by definition, every `sync_*` after the phone was unplugged, and `backup_complete`, which is sent just after the screen has invited you to unplug.

The resolved header is therefore cached in `/run/iosbackupmachine/webhook_auth.json`, owner-only, and reused when the phone is absent. `/run` is a systemd tmpfs: RAM-only, cleared at every boot and never written to the SD card, so a powered-off device still gives nothing away without the phone. Only the derived header is cached, never the passphrase - the passphrase also unlocks the WireGuard and remote-sync credentials.

The cache is refreshed whenever the phone is seen, primed at the start of every backup, and dropped when notification settings are saved. Set `IOSBACKUP_WEBHOOK_AUTH_TTL` to a number of seconds to expire it sooner; the default of `0` means it lasts until the next reboot.

If auth is enabled and no header can be obtained, the webhook is skipped rather than sent unauthenticated, and the reason is written to the run's log.

## Related

- [Backups](../backups/) for the backup flow the dashboard controls
- [Remote sync](../remote-sync/) for the sync card and its network restrictions
- [Security](../../architecture/security/) for password hashing and credential encryption
