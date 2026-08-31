# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses a
single version constant in `app/webui.py`.

## [4.6.0] - 2026-08-31

### Fixed

- An update started from the web UI killed itself partway through and took the
  device off the VPN. The updater ran as a plain child of `webui.service`;
  `install.sh` restarts that unit midway, and since the unit does not set
  `KillMode=process`, systemd tore down its whole control group - the updater
  with it. `start_new_session` had put the updater in its own session but not
  its own cgroup, so it was no protection. The kill landed after the installer
  had stopped every service and before it restarted any, leaving the display
  daemon down and, with it, the WireGuard reconciler that keeps the VPN up. The
  updater now runs in its own transient systemd unit (`systemd-run
  --unit=iosbackup-update --collect`), outside the web UI's cgroup, so
  restarting the web UI cannot touch it.
- A web-triggered update never rebooted, even when the release required one.
  Both `update.sh` and `install.sh` asked with `read -rp`, which reads EOF
  without a tty and takes the "no" branch - so "Re-install anyway?" silently
  cancelled a re-install and "Reboot now?" silently skipped the reboot. Both
  prompts now honour `IOSBACKUP_NONINTERACTIVE=1`.

### Changed

- An update started from the web UI now always reboots the device when it
  finishes (`IOSBACKUP_AUTO_REBOOT=1`), rather than only when the release bumps
  `REBOOT_EPOCH`. The installer stops every service up front and restarts only
  some of them, so a reboot is what guarantees the display daemon, the
  WireGuard reconciler and usbmuxd all come back. Updates run over SSH are
  unchanged and still prompt.

### Added

- An `Updating - device will reboot` screen on the e-ink for the duration of a
  web-triggered update. The web UI drops a sentinel before it launches the
  updater; the display daemon shows the screen while it exists and paints it as
  its final frame on the way down, so the e-ink holds it with the daemon
  stopped and through the reboot. Without this the daemon's shutdown handler
  painted the power-off owner screen, which reads as "the device is off" for
  the whole update. The sentinel is retired once it predates the current boot
  (armbian-ramlog restores zram `/var/log` across a reboot, so it cannot be
  assumed to vanish with it), and in any case after 30 minutes, so an update
  that dies without rebooting cannot strand the panel.

## [4.5.1] - 2026-08-21

### Fixed

- The sync screen showed a total size that kept moving up and down for the
  whole transfer. rsync's `--info=progress2` reports only an integer
  percentage, and the total was back-computed from it as `bytes * 100 / pct`,
  so the figure climbed while the transferred bytes grew inside one percent
  bucket and dropped again each time the percentage ticked over - an error of
  up to `total/pct`, so roughly a factor of two at 1% and still a few percent
  at half way. The real total is now measured by walking the backup directory
  on a background thread while rsync builds its own file list, so it is
  normally exact from the first progress update and never moves afterwards.
  The old estimate is kept only as a fallback for the case where the walk
  fails or has not finished yet.
- Progress on the e-ink and the dashboard lagged behind the transfer. rsync
  separates its progress samples with CR, not LF, so a single read of the pipe
  holds a burst of ten or more of them; the parser took the first and dropped
  the rest, then took the first of the next burst. It now takes the newest
  sample in each burst, so the bytes, percentage and speed on screen are
  current.

## [4.5.0] - 2026-08-21

### Added

- Optional SSH host key verification for the sync server. Remote Sync settings
  gains a `Verify the server's SSH host key` switch and a SHA256 fingerprint
  field; when set, the device pins that key and refuses to sync if the server
  presents a different one. Verification runs before any data moves, on both
  Test Connection and every sync, and fails closed - a mismatch or an
  unreadable key aborts rather than falling back to trusting the server.
  `Fetch from server` reads the offered fingerprints so they can be compared
  against a value obtained out-of-band. Leaving the switch off keeps the
  previous `accept-new` behaviour unchanged.

### Fixed

- The e-ink could sit on a stale `Sync failed` (or other result) screen through
  an entire following sync or backup. After a backup the daemon holds the result
  screen until the iPhone is unplugged, but that wait watched only the cable, so
  it never re-read the status file - a sync started from the web UI while the
  phone was still plugged in, or a fresh backup request, stayed invisible for its
  whole run. The wait now also releases the screen for a live sync or an explicit
  start request. An idle plugged-in phone still holds the result, so the same
  device is not immediately backed up again.
- The single-tap system-info screen did nothing in the cases it was most wanted.
  A tap was discarded outright while a backup or sync was running - and the
  request flag was consumed anyway, so the tap vanished - and even when it was
  allowed, the next sync tick repainted over it about half a second later. A tap
  now always shows the info screen for 30s whatever the device is doing, and the
  screen that was up beforehand comes back when the window closes, along with
  anything the daemon drew meanwhile (so a backup result raised during those 30s
  is not lost). The rule is enforced once, in the display's single drawing owner,
  instead of at each call site.

## [4.4.4] - 2026-07-14

### Fixed

- A completed sync could be reported as failed with `rsync failed (exit None)`.
  The read loop broke on the output pipe's EOF before it ever reaped rsync, so
  the exit code was still unset when it was checked. rsync is now reaped after
  the loop, so the real exit status is used and a clean exit-0 is no longer
  misreported. This was a race, so it surfaced intermittently.

### Changed

- Sync logs are now wall-clock timestamped (`[YYYY-MM-DD HH:MM:SS]` per line),
  matching the continuous logs, instead of carrying only rsync's elapsed-seconds
  counter. Applies to both auto-sync and manual sync.
- A failed sync names the reason: the log and status now read e.g. `rsync failed
  (exit 255: SSH/connection error ...)` or `killed by signal 9`, instead of a
  bare exit number.

## [4.4.3] - 2026-07-08

### Fixed

- usbmux hot-plug now works for every plug and lock order. The daemon tracks a
  sysfs signature of the connected Apple device, so a fresh plug or a
  re-enumeration (from unlocking a phone that was in USB Restricted Mode)
  restarts usbmuxd immediately, and a device that stays invisible is retried
  with exponential backoff (8s to 120s) instead of on a tight loop.

### Changed

- The single-tap info screen shows the last sync time in the same format as the
  last backup time (`HH:MM / DD Mon YYYY`).

## [4.4.2] - 2026-07-08

### Fixed

- Hot-plugging an iPhone after boot is recognised without a udev round-trip. The
  usbmuxd re-scan moved into the always-on daemon, which restarts usbmuxd when
  the kernel sees an Apple device that usbmux does not.
- The single-tap PiSugar button shows the system-info screen again. The button
  now uses the same flag-file mechanism as double and long tap (the pisugar
  socket had no working single-tap query).

## [4.4.1] - 2026-07-07

### Added

- A udev rule and one-shot service that re-scan usbmuxd when an iPhone is
  plugged in, working around the libusb hot-plug gap on this image.

## [4.4.0] - 2026-07-07

### Added

- Three-state iPhone status icon: absent, plugged-but-untrusted (padlock), and
  trusted (checkmark), so the trust state that udid-mode decryption depends on is
  visible at a glance.

## [4.3.1] - 2026-07-07

### Fixed

- Status icons paint the true state on the first sample, so they are correct
  after a daemon restart instead of showing stale defaults.

## [4.3.0] - 2026-07-07

### Changed

- Logs persist on the rootfs under `/var/lib/iosbackupmachine/` so they survive
  reboots and power loss, with app-managed retention (newest 50 backup and 50
  sync logs, and anything older than 90 days pruned). Volatile runtime state
  stays on the zram `/var/log` to avoid SD-card wear.

### Fixed

- The WireGuard auto-connect reconciler verifies an actual handshake instead of
  just the interface existing, and tears down and reconnects a tunnel that comes
  up but never handshakes.

## [4.2.0] - 2026-06-08

### Changed

- WiFi backend rewritten on netplan and wpa_supplicant with multi-network
  roaming, status icons on every screen, and a full-tunnel VPN mode that keeps
  local SSH and web UI access.

[4.4.3]: https://github.com/giovi321/ios-backup-machine/compare/v4.2.0...v4.4.3
[4.2.0]: https://github.com/giovi321/ios-backup-machine/releases/tag/v4.2.0
