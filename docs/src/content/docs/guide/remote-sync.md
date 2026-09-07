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

## Host key verification

By default the device trusts the first host key it sees (`StrictHostKeyChecking=accept-new`), which
protects later syncs but not the first one. Tick **Verify the server's SSH host key** on the Remote
Sync settings page to pin the server's key instead: the sync then refuses to run unless the server
presents exactly the key you configured.

Get the fingerprint from the server itself:

```sh
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
```

Paste the `SHA256:...` value into **Host Key Fingerprint**. **Fetch from server** fills it in for you,
but it reads the key over the same network you are trying to protect, so treat it as a convenience and
confirm the value out-of-band before saving.

Verification runs before any data is transferred, on both Test Connection and every sync. A mismatch
fails the sync with `Host key mismatch - refusing to connect`, naming the expected and offered
fingerprints. If you legitimately rebuilt the server or rotated its host key, update the stored
fingerprint; until then, syncs stay blocked.

Untick the checkbox to go back to accept-new.

## Triggering a sync

- Manual: long-press the PiSugar button, or click Sync Now on the web UI dashboard or the Remote Sync settings page
- Auto-sync: optionally trigger a sync after each successful backup

## Network restrictions

You can limit when a sync is allowed to run, with `sync.allowed_network`:

- `any`: no restriction, and the default
- `wifi`: only while a WiFi address is up
- `wifi_ssid`: only on the SSID named in `sync.allowed_ssid`. Picking Specific WiFi SSID in the web UI reveals the field that writes it, and leaving the field empty falls back to "any WiFi"
- `usb`: only while the iPhone USB tether is up

A refused sync reports `network_not_allowed` and names the restriction it hit. The check fails open if `netutil` cannot be imported.

## Connection errors

Before transferring, a pre-flight check reports the actual cause of a failure on both the e-ink and the dashboard, instead of a raw rsync exit code. The messages:

- No network connection
- VPN not connected
- No internet connection
- Sync server unreachable

If your sync depends on the tunnel, see [WireGuard VPN](../wireguard-vpn/); for the network options, see [Networking](../networking/).

## Progress display

The e-paper screen and the web dashboard show transferred / total size, current speed, percentage, and a progress bar. Sizes auto-scale across KB, MB, GB, and TB.

During the initial file-list scan (rsync `--no-inc-recursive`) you see "Building file list (Xs)" instead of fake progress, because rsync has not yet computed the total. The total size is measured by walking the backup directory while that scan runs, so it is a real figure rather than one inferred from rsync's rounded percentage, and it does not drift during the transfer.

## Stall detection

Once the transfer is running, rsync going quiet for 5 minutes puts a yellow "Stalled" badge on the dashboard and switches the e-ink to "Sync STALLED". After 30 minutes without output the sync is auto-aborted with a `sync_error` reading "Sync stalled 30 min, aborted.". The thresholds are deliberately generous: on a backup of many small files over SSH, rsync can legitimately go quiet for minutes at a time between bursts.

The file-list scan has its own limit, because it produces no progress output at all: if rsync writes nothing for 30 minutes during that phase, the sync is aborted with "No progress 30 min, aborted.".

## Overall time limit

Off by default. Remote Sync settings has a **Time limit** field in minutes; leave it empty for no limit.

There is deliberately no default. The stall watchdogs above already abort a sync that has stopped moving, so a limit here only bounds a transfer that is still working - and a first sync of a large backup set legitimately runs for many hours. A limit that fires mid-transfer reports `run_timeout` and reads like a failure, even though nothing failed. The partial transfer resumes on the next run either way, because `--partial-dir` is always in use.

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
