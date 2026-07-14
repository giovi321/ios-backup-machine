# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses a
single version constant in `app/webui.py`.

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
