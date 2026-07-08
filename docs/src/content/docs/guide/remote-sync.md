---
title: "Remote sync"
description: Copy backups to a remote server over rsync-over-SSH, with progress, stall detection, and power-aware, resumable transfers.
---

Remote sync ships your backups to a remote server over rsync-over-SSH, run manually or automatically after each backup. This page covers authentication, how a sync is triggered, the network and battery conditions that gate it, the progress and error reporting on both the e-ink and the dashboard, and how a transfer resumes after a reboot.

The receiving server needs `rsync` installed.

:::note
Install rsync on the remote with `sudo apt install rsync`. Without it the sync cannot complete.
:::

## Transport and authentication

Sync uses rsync over SSH and supports both SSH key and password authentication. Configure the server and credentials in the web UI under Remote Sync; credentials are stored encrypted.

## Triggering a sync

- Manual: long-press the PiSugar button, or click Sync Now on the web UI dashboard or the Remote Sync settings page
- Auto-sync: optionally trigger a sync after each successful backup

## Network restrictions

You can limit when a sync is allowed to run:

- WiFi only
- A specific SSID
- iPhone USB tethering

## Connection errors

Before transferring, a pre-flight check reports the actual cause of a failure on both the e-ink and the dashboard, instead of a raw rsync exit code. The messages:

- No network connection
- VPN not connected
- No internet connection
- Sync server unreachable

If your sync depends on the tunnel, see [WireGuard VPN](../wireguard-vpn/); for the network options, see [Networking](../networking/).

## Progress display

The e-paper screen and the web dashboard show transferred / total size, current speed, percentage, and a progress bar. Sizes auto-scale across KB, MB, GB, and TB.

During the initial file-list scan (rsync `--no-inc-recursive`) you see "Building file list (Xs)" instead of fake progress, because rsync has not yet computed the total.

## Stall detection

If rsync produces no output for 2 minutes, the dashboard shows a yellow "Stalled" badge and the e-ink switches to "Sync STALLED". After 15 minutes without progress the sync is auto-aborted with a `sync_error`.

## Cancelling

While a sync is in progress, a Cancel Sync button appears on the dashboard and the Remote Sync settings page. It kills `rsync` and `backup-sync.py` immediately and reports "Cancelled by user.". The end state (complete, failed, or cancelled) stays on the e-ink until another event, such as a new sync, a backup start, or a service restart.

## Keepalive

SSH keepalive is set to `ServerAliveInterval=30` with `CountMax=3`, so a dead TCP connection is detected in about 90 seconds.

## Resumable across reboots

rsync runs with `--partial --partial-dir=.rsync-partial`, so a reboot or power loss mid-sync resumes from where it stopped instead of restarting from zero. Incomplete files live in `.rsync-partial/` on the remote.

## Power-aware behavior

A sync will not start, and an in-progress sync auto-aborts, when the battery is below `sync.min_battery_percent` (default 35%) and the device is not charging. This keeps a long transfer from being cut mid-way by PiSugar's 30% auto-shutdown. The aborted transfer resumes on the next run.

:::tip
The threshold is tunable in `config.yaml`. Battery is read fail-open: if the UPS cannot be reached, the sync proceeds.
:::
