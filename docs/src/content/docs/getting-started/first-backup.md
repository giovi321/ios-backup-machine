---
title: "First backup"
description: Complete the first-start wizard, set backup encryption on the iPhone, run the first backup, and restore when needed.
---

After installation, open the web UI at `http://<device-ip>:8080` and complete the first-start wizard, then set backup encryption on the iPhone and run the first backup. The first backup is a full, slow run; every later backup is incremental and much faster.

## First-start wizard

On the very first boot (when owner info has not been configured), the web UI automatically shows a guided setup wizard. It walks through nine steps:

1. Owner information, displayed on the e-ink screen when idle
2. WiFi (optional), to connect to a wireless network for NTP sync, notifications, and remote access
3. Date and time, set manually or with automatic NTP synchronization
4. Backup directory, where backups are stored
5. Backup encryption, set directly on your iPhone (the password is never stored on this device)
6. Device filter (optional), to restrict which iPhones can trigger a backup; it auto-detects the connected device
7. Notifications (optional), webhook and MQTT alerts for backup events
8. Display orientation, landscape left or right
9. Web UI password (optional), to protect the settings interface

The Flask session `secret_key` is generated automatically on first start and saved to `config.yaml`, so no manual configuration is needed.

## Backup encryption

Set backup encryption during the wizard, or later from the Encryption page in the sidebar. Enter a password with your iPhone connected and unlocked. The password is sent directly to the iPhone.

:::caution
The encryption password is never stored on this device. Write it down. Without it you cannot restore an encrypted backup.
:::

If no iPhone is connected during setup, skip the encryption step and visit the Encryption page later when your iPhone is plugged in.

## Run the first backup

1. Plug in your iPhone, unlock it, and tap Trust when prompted
2. The first backup runs; this takes a long time depending on device storage
3. All subsequent backups are incremental and much faster

If the first backup is interrupted, encryption (if enabled) remains active on the iPhone. The next backup attempt proceeds normally and no data is lost.

## Restoring a backup

To restore a backup, plug your iOS device into a computer, plug the microSD card into the same computer, and run:

```bash
idevicebackup2 restore --password <your backup password> /media/sdcard/iosbackup/
```

## Related pages

- [Backups](../../guide/backups/)
- [Web UI](../../guide/web-ui/)
