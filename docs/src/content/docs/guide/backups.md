---
title: "Backups"
description: How automatic backups start, the auto-start and device-filter controls, encryption, battery protection, and backup notifications.
---

Plug in your iPhone and the backup starts automatically. This page covers the auto-start toggle, the device filter that restricts which iPhones can trigger a backup, how encryption is set, the UPS battery cut-off that stops a backup cleanly, and the notification events fired around a backup.

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

## Battery protection

The PiSugar 3 UPS guards against corruption on power loss. A running backup stops cleanly if the battery drops below 30%, and a power loss or UPS switch-off triggers a graceful shutdown.

:::note
The 30% backup cut-off sits above PiSugar's own auto-shutdown, so a backup ends on its own terms before the device powers off.
:::

## Notifications

Backup-related events can be sent by webhook (JSON POST) and/or MQTT:

- `backup_start`
- `backup_complete`
- `backup_error`
- `device_connected`
- `device_disconnected`
- `device_rejected`

Configure targets and which events to send in the web UI or directly in `config.yaml`.

## Related

- [First backup](../../getting-started/first-backup/) for the initial setup walkthrough
- [Web UI](../web-ui/) for the settings pages
- [Security](../../architecture/security/) for encryption and credential handling
