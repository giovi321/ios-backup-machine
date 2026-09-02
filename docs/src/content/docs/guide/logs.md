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

Every line in every log carries a wall-clock `[YYYY-MM-DD HH:MM:SS]` stamp, so a sync failure can be lined up against what the updater, the NTP sync or the button handler was doing at that second. `update.log` and the output redirected into `autostart.log` are raw shell output, so they are piped through `logutil.py --stamp` to get the same format.

## Reading a sync log

A sync log opens with what the run is moving and where, then carries a progress line at every percentage change and otherwise once a minute:

```text
[2026-05-14 15:42:12] [SYNC] /media/iosbackup/ -> backup@192.168.1.50:/srv/ios-backups/
[2026-05-14 15:42:12] [CMD] /usr/bin/rsync -a --delete --partial ...
[2026-05-14 15:42:23] [INFO] local tree: 50.0 GB in 41,207 files
[2026-05-14 15:42:23] [INFO] file list complete after 11s, transfer started
[2026-05-14 22:15:14] [SYNC] 40% | 20.0 GB / 50.0 GB | 3.1MB/s | elapsed 1h10m | ETA 1h45m | +186.0 MB/60s
[2026-05-14 22:15:14] [FILE] 00008030-.../Snapshot/4f/4fa8c2e1 (1.2 GB)
```

The trailing `+186.0 MB/60s` is the delta since the previous line: it is what tells you whether a transfer parked on one percentage is still moving bytes or genuinely stuck. `ETA` comes from a rolling ten-minute average of the transfer's own byte samples, not from rsync's instantaneous rate, and reads `ETA unknown` rather than guessing when nothing is moving.

`[FILE]` names the file rsync is on, sampled at most once a minute. rsync reports every file it sends, which on a first sync is six figures of them, so the log keeps a sample rather than the list. `--stats` appends the real totals when the run ends.

### When a sync fails

A failure writes one `[ERROR]` line naming the cause, followed by `[POSTMORTEM]` lines recording the state of everything the transfer depended on, while the evidence is still fresh:

```text
[ERROR] Sync failed at 40% (20.0 GB of 50.0 GB) after 1h10m: SSH/connection error [exit 255]
[POSTMORTEM] transferred 20.0 GB of 50.0 GB in 1h10m
[POSTMORTEM] last file seen: 00008030-.../Snapshot/9c/9c11ab04
[POSTMORTEM] no OOM kill of rsync in recent dmesg
[POSTMORTEM] remote 192.168.1.50:22 unreachable ([Errno 110] Connection timed out)
[POSTMORTEM] wg0 last handshake 12m ago
[POSTMORTEM] local free space on /media/iosbackup: 41.2 GB
```

The probes are best-effort and never replace the failure they are diagnosing: one that cannot run says so and the rest still report.

The same verdict is delivered as a `sync_error` notification. See [Web UI](../web-ui/#notifications) for the payload.

## Retention

Retention is managed by the app, not logrotate. The newest 50 backup logs and the newest 50 sync logs are kept, and anything older than 90 days is pruned. Override the defaults with two environment variables:

- `IOSBACKUP_LOG_KEEP`: how many of each log type to keep
- `IOSBACKUP_LOG_MAX_AGE_DAYS`: maximum age in days before pruning

The continuous append logs (`ntp-sync.log`, `autostart.log`, `update.log`) are size-capped by logrotate. `webui.log` self-rotates.

## Browsing logs

The web UI Logs page can browse backup log files directly from the browser. It has separate live-tail links for the most recent backup log and the most recent sync log.

## Related

- [Web UI](../web-ui/) for the Logs page and the rest of the browser interface
