---
title: "Display and controls"
description: The e-ink screens the daemon renders, the status icon row, and the PiSugar button gestures.
---

The 2.13" e-ink display (Waveshare 2.13" e-Paper HAT V4, 250x122) is driven by a single always-on daemon that renders every screen from a shared status file, and the PiSugar button gives you three actions without a browser: system info, start a backup, and start a remote sync. This page lists the screens, the status icons in the bottom-left of every live screen, and what each button gesture does.

## Who owns the display

The always-on `iosbackupmachine.service` daemon is the only process that opens the e-paper panel. It holds the panel for the whole uptime and draws every screen from state. Everything else (backup, remote sync, web UI, PiSugar button) only writes state and never touches the display. Each screen-type transition does one full refresh; animated progress uses partial refresh.

:::note
Because one process owns the panel, screens never fight over the SPI bus. The daemon samples icon state in the background, so the status row never blocks or overlaps the screen text.
:::

## Screens

The daemon renders these screens from state:

- Boot / idle: on boot it shows the project icon, the "iOS Backup Machine" title, and owner info. When idle it shows the last backup result, timestamp, disk usage, and owner info
- Backup progress: prompts to unlock the phone if needed, shows encryption status and progress percentage, then a success confirmation with timestamp at the end
- Sync progress: transferred / total size, current speed, and a progress bar (see [Remote sync](../remote-sync/))
- System info: shown for 30 seconds after a single button tap (see below), then returns to the boot screen
- Unplug / interrupted: if you unplug the iPhone mid-backup the process stops safely and the screen shows the interruption timestamp
- Power-off owner screen: owner info only. The daemon paints it on shutdown and sleeps the panel, so the image persists on e-paper after power-off or power loss

Errors appear directly on the display.

## Status icons

Every live screen (boot/idle, backup, sync, info, interrupted, complete) draws a row of status icons in the bottom-left corner. From the set: power (always on), VPN, internet, WiFi, and iPhone. The VPN, internet, and WiFi icons are crossed out with a "/" when that connection is inactive.

The power-off owner screen omits the icon row.

### The three-state iPhone icon

The iPhone icon has three states, so the trust state (which udid-mode decryption depends on) is visible at a glance:

- Crossed out: no iPhone seen on USB
- Closed padlock: iPhone plugged in but not readable yet, because Trust has not been granted or the phone is locked
- Checkmark: iPhone plugged in and trusted, lockdown is readable, so the WireGuard config can be decrypted

## PiSugar button controls

The daemon's button listener handles the PiSugar power button. Three gestures:

- Single tap: shows the system-info screen for 30 seconds, then returns to the boot screen. It lists date/time, active network (WiFi / iPhone hotspot / Ethernet), IP, VPN state, last backup, last sync, SoC temperature, and disk free %
- Double tap: starts an iPhone backup, the same action as the web UI Start Backup. It needs an allowed iPhone connected and works even when auto-start is off (see [Backups](../backups/))
- Long press: triggers a remote sync to the configured server over rsync-over-SSH. The display then shows transferred / total size, current speed, and a progress bar (see [Remote sync](../remote-sync/))

:::tip
The double-tap backup and long-press sync work without opening the web UI, so the device is fully usable headless.
:::

For how the single-owner display model fits the rest of the system, see the [Architecture overview](../../architecture/overview/).
