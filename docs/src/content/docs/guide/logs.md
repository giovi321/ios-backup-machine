---
title: "Logs"
description: Where logs live, why they sit on the rootfs instead of the zram RAM disk, per-run log files, retention, and browsing them from the web UI.
---

Logs live on the rootfs under `/var/lib/iosbackupmachine/` so they survive reboots and power loss. Each backup and each sync writes its own timestamped file, the app prunes old ones itself, and the web UI Logs page can browse and live-tail the most recent backup and sync. Volatile runtime state stays in RAM to avoid wearing the SD card.

## Where logs live

Logs are stored on the rootfs under `/var/lib/iosbackupmachine/`, so they survive reboots and power loss. They are deliberately kept off `/var/log`, which on this Armbian image is a zram RAM disk (`armbian-ramlog`) that loses anything not yet synced to disk when the device is cut abruptly. That abrupt cut is the exact failure mode of a power-loss shutdown.

Volatile runtime state stays on `/var/log/iosbackupmachine/`: `backup_status.json`, `start_requested`, and `stop_requested`. This state is rewritten constantly and regenerated every run, so keeping it in RAM avoids SD-card wear.

:::note
Persistent logs go on the rootfs (`/var/lib/iosbackupmachine/`); constantly-rewritten runtime IPC files stay on the RAM-backed `/var/log/iosbackupmachine/`.
:::

## Per-run log files

Each run creates a timestamped file:

```text
backup-YYYYMMDD-HHMMSS.log
sync-YYYYMMDD-HHMMSS.log
```

## Retention

Retention is managed by the app, not logrotate. The newest 50 backup logs and the newest 50 sync logs are kept, and anything older than 90 days is pruned. Override the defaults with two environment variables:

- `IOSBACKUP_LOG_KEEP`: how many of each log type to keep
- `IOSBACKUP_LOG_MAX_AGE_DAYS`: maximum age in days before pruning

The continuous append logs (`ntp-sync.log`, `autostart.log`, `update.log`) are size-capped by logrotate. `webui.log` self-rotates.

## Browsing logs

The web UI Logs page can browse backup log files directly from the browser. It has separate live-tail links for the most recent backup log and the most recent sync log.

## Related

- [Web UI](../web-ui/) for the Logs page and the rest of the browser interface
