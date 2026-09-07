---
title: "Backups"
description: How automatic backups start, the auto-start and device-filter controls, encryption, the guards that stop a bad run, and backup notifications.
---

Plug in your iPhone and the backup starts automatically. This page covers the auto-start toggle, the device filter that restricts which iPhones can trigger a backup, how encryption is set, the guards that refuse or stop a run that cannot finish safely, the alert for a device that has quietly stopped backing up, and the notification events fired around a backup.

## Automatic start

When an iPhone is plugged in, the system runs an encrypted `idevicebackup2` backup to local storage. The display prompts you to unlock the phone if needed, shows encryption status, and shows progress percentage, then confirms success with a timestamp. The first backup takes a long time depending on device storage; later backups are incremental and much faster.

### Auto-start toggle

Auto-start is on by default (`backup.auto_start: true`). It controls whether plugging in an iPhone starts a backup on its own. With it off, plugging in a phone does not start a backup, but you can still start one manually with the web UI Start Backup button or a double-tap of the PiSugar button (see [Display and controls](../display-and-controls/)).

You can toggle it under Backup Settings in the [Web UI](../web-ui/).

## Device filter

The device filter restricts which iPhones can trigger a backup:

- Enable the filter in Device Filter settings
- Add devices by connecting an iPhone and clicking "Add connected device", or enter a UDID manually
- When a non-allowed device is plugged in, the backup is blocked and a notification is sent (configurable via `backup.notify_on_rejected`)

When the filter is disabled (the default), any iPhone triggers a backup.

## Backup encryption

Backups use the iPhone's own encryption credentials. You set the password during the first-start wizard, or later from the Encryption page, with your iPhone connected and unlocked. The password is sent directly to the iPhone and is never stored on this device.

:::caution
Because the password is never stored on the device, write it down. You need it to restore a backup. If no iPhone is connected during setup, skip the encryption step and return to the Encryption page later when the phone is plugged in.
:::

If a first backup is interrupted, encryption (if enabled) stays active on the iPhone, and the next attempt proceeds normally with no data lost.

For how credentials and encryption fit the wider threat model, see [Security](../../architecture/security/).

## Power loss

The PiSugar 3 UPS keeps the board running through a mains cut, so losing power does not corrupt a backup or the filesystem. A soft power-off from the PiSugar button runs `shutdown.sh`, which stops the display daemon first so it can paint the owner screen and sleep the panel, and only then shuts the system down.

At 30% battery PiSugar cuts power to the board on its own (`auto_shutdown_level` in its own config). That one is not a graceful shutdown, which is why the battery guards below refuse or stop a backup before it gets there.

## Guards before a backup starts

Three checks run before `idevicebackup2` is launched. Any one of them refuses the run, puts the reason on the display, and sends a `backup_error` notification, since nothing else reports a refusal to anyone who is not standing in front of the panel.

- Backup drive: the marker file (`marker_file`, `.foldermarker` by default) must exist inside `backup_dir`, and a probe write must succeed. A drive that came back read-only fails here instead of crashing `idevicebackup2` part-way through
- Free space: at least 200 MB on the root filesystem, and at least 500 MB on the backup drive, or `backup.min_free_mb` when that is higher (512 MB with the shipped default)
- Battery: at least `backup.min_battery_percent` (default 35) unless the device is charging

The battery floor sits above PiSugar's own 30% auto-shutdown, which is the point of the number. A backup started at 33% is a backup cut mid-write, with nothing in any log to say why and a half-written folder on the drive. The check fails open in every direction the probe itself can fail: an unreadable UPS, no power module at all, or a floor of `0` all let the run proceed.

## Guards during a backup

Each of those conditions can stop being true while a run is going. One health probe re-checks them every 10 seconds, on the thread already reading `idevicebackup2`'s output, and stops the run when one trips.

| Reason code | What tripped |
|---|---|
| `mount_lost` | The backup drive disappeared: the marker file vanished, or the mountpoint's device id changed |
| `disk_full` | Free space on the backup drive fell below `backup.min_free_mb`, or the root filesystem fell below 200 MB |
| `battery_abort` | The battery read below `backup.min_battery_percent` on two checks a minute apart, while not charging |

The reason code appears on the display, in the run log as an `[INTERRUPT]` line, and as `reason_code` in the `backup_error` notification.

`mount_lost` is the one abort with no grace period: `idevicebackup2` is killed outright, because every further second is written into the now-empty mountpoint on the root filesystem. The other two send `SIGTERM` first, so the file in flight is finished and the partial backup stays resumable.

The mount check waits for two consecutive misses of the marker file before it fires, because a single I/O error on a drive under load reads exactly like an unmounted drive, and killing a 40 GB run over one is worse. A changed device id fires at once: that is positive evidence the drive is gone, not an absence of evidence.

## Hang and runaway timeouts

Two limits bound a run that is producing no outcome at all:

- `backup.hang_timeout_sec` (default 600): `idevicebackup2` silent for this long counts as hung and is terminated, reported as `silent`
- `backup.max_duration_sec` (default 14400, four hours): the total cap for one run, reported as `overtime`

Separately, the daemon's main loop feeds a heartbeat watchdog. If no beat lands in time, the process logs `[FATAL] main loop stalled` and exits non-zero, and `Restart=on-failure` on `iosbackupmachine.service` brings back a working daemon.

## The quiet-device alert

Every other notification is edge-triggered: something happened, so something is sent. Nothing reports the absence of events, so a device that quietly stops backing up would tell nobody until a restore is needed. The phone stops being plugged in, auto-start gets switched off, the device filter starts rejecting, and the appliance sits there looking healthy.

`backup_stale` covers that case. When no backup has succeeded for `backup.stale_after_sec` (default 604800, seven days), one notification goes out. One per quiet stretch, not one per check: the alert is keyed to the timestamp it is reporting on, so a reboot, a restart, or a system update cannot re-fire an alert that already went out, and the next successful backup moves the key forward and re-arms it. There is no repeat interval and no snooze to get wrong.

Set `backup.notify_stale: false` to switch it off, or `backup.stale_after_sec: 0`.

The same verdict shows up in two more places. The e-ink info screen appends `(stale)` to its `Backup:` line, and `GET /api/health` reports a `no backup in N days` warning. The health endpoint matters here because a poller also notices the device being unreachable, which a push notification from the device itself can never report.

:::note
The first time this version runs on a device that already has backups, the record is seeded from the newest backup folder rather than reporting "never backed up". That delays a first alert by at most one threshold and corrects itself at the next completed backup.
:::

## Where these settings live

Auto-start and the rejected-device notification are the only two with web UI controls, on the Backup Settings page. Everything else on this page is edited in `config.yaml` under `backup:`.

The timeouts and the two guard floors are read when the display daemon starts, so restart `iosbackupmachine.service` after changing them. `notify_stale` and `stale_after_sec` are re-read on every check and take effect without a restart.

## Notifications

Backup-related events can be sent by webhook (JSON POST) and/or MQTT:

- `backup_start`
- `backup_complete`
- `backup_error`
- `backup_stale` (see [The quiet-device alert](#the-quiet-device-alert) above)
- `device_connected`
- `device_disconnected`
- `device_rejected`

Configure targets and which events to send in the web UI or directly in `config.yaml`. Remote sync fires `sync_start`, `sync_complete`, and `sync_error` through the same two channels and the same event list; see [Web UI](../web-ui/#notifications).

A `backup_error` carries a `reason_code` naming what stopped the run: `mount_lost` or `disk_full` from either the pre-backup gate or the in-run probe, `battery_low` when the pre-backup gate refused on battery, `battery_abort` when the in-run probe stopped a running backup on battery, and `silent` or `overtime` from the timeouts above. An `idevicebackup2` failure carries the numeric `code` from `error_codes` instead.

A `backup_complete` that ends a quiet stretch carries `was_stale: true` and `stale_days`, so an automation can report the recovery from an event it already subscribes to.

## Related

- [First backup](../../getting-started/first-backup/) for the initial setup walkthrough
- [Web UI](../web-ui/) for the settings pages
- [Security](../../architecture/security/) for encryption and credential handling
