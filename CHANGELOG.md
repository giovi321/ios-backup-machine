# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses a
single version constant in `app/webui.py`.

## [4.10.3] - 2026-09-07

### Fixed

- A flaky test broke CI. `test_the_quiesce_wait_is_bounded` counted every value
  passed to `time.sleep` while the reboot route ran, but monkeypatching
  `webui.time.sleep` replaces it for the whole process, so the background
  connectivity monitor's 20 s tick was counted as a quiesce poll whenever an
  earlier test had started that thread. It passed on Windows, where the tests
  that start it are skipped, and failed on Linux. The assertion now counts only
  sleeps of `_QUIESCE_POLL_SEC`, which is a named constant for exactly that
  reason. No production behaviour changed.

## [4.10.2] - 2026-09-07

### Fixed

- The journal viewer's problems-only filter matched the two daemons' bracket
  tags only. Flask leaves its `default_handler` on `app.logger`, so every
  `app.logger.warning` and `.error` also reaches `webui.service`'s journal
  formatted `[ts] WARNING in webui: ...`, which none of those tags match: the
  filter answered "nothing matched" on a journal full of web UI errors.
- That filter also dropped the flood-truncation notice, so a filtered view of a
  truncated window read as the whole story.
- `prune_logs` promised the freshest log survives all three of its rules, and
  the size sweep spared it, but the age rule did not. An appliance idle past
  `IOSBACKUP_LOG_MAX_AGE_DAYS` could delete the log the daemon still held open,
  freeing nothing and making the live run vanish from the Logs page.
- Cancelling a sync to reboot, shut down or update wrote no closing line to the
  persistent sync log, so the confirm banner's promise that "the run log records
  why it ended" held for a backup and not for a sync.
- A clean stop that failed or ran out of budget still reported success. The
  caller now says the log may end without a reason instead of leaving the
  failure in `webui.log` alone.
- The journal page re-read the boot count from journald on every render, and its
  live tail re-renders every 10 s, so a tab left open forked `journalctl` twice
  per refresh for an answer that does not change.

### Added

- Tests for `Panel`, the only writer to the e-ink panel and the one class the
  suite never constructed. Covers the errno-16 retry that the blank-display bug
  disabled, and the driver API resolution whose silent failure mode is a correct
  picture that flashes on every tick.

## [4.10.1] - 2026-09-07

### Fixed

- `sync.allowed_ssid` was missing from the config schema, so it was never
  default-filled and never type-checked: the settings page wrote it and
  `sync_manager` read it, but a hand-edited non-string reached the SSID
  comparison instead of being reset with a warning like every other setting.
  Found while auditing the documentation against the code.

### Changed

- Documentation brought back in line with the code across all 18 pages, the
  README, `SECURITY.md`, `CONTRIBUTING.md` and `config.yaml.example`. Several
  statements were wrong rather than merely out of date, the backup battery
  threshold and the manual-install paths among them.

## [4.10.0] - 2026-09-07

### Fixed

- The overall sync time limit was a hard-coded 3600 s that nothing in the web UI
  could change, so a first sync of a large backup set aborted mid-transfer and
  reported a failure. A 130 GB set stopped at 20% after an hour. There is now no
  default limit at all: the scan and stall watchdogs already abort a sync that
  has stopped moving, so a cap only bounds one that is still working. Existing
  configs holding the old 3600 are cleared on upgrade; any other value was
  chosen deliberately and is left alone.

### Added

- A **Time limit** field in Remote Sync settings, in minutes, empty for no
  limit. Anything unparseable reads as no limit rather than silently
  reinstating a cap.

### Changed

- Config schema version 4, for the sync-limit default.

## [4.9.0] - 2026-09-07

### Fixed

- A fatal error in the display daemon exited with code 0, so systemd's
  `Restart=on-failure` never restarted it - one unexpected exception left the
  device dead until the next power cycle. Fatal exits now use a non-zero code
  and the unit restarts the daemon.
- The daemon no longer dies or hangs on startup faults: an unwritable log
  directory, a missing or stuck e-ink display, or a broken config all degrade
  instead of crashing - the daemon runs headless (backups, sync and
  notifications keep working) and logs one journal warning. The e-ink BUSY wait
  now has a 10-second ceiling instead of blocking forever, and after 30
  consecutive draw failures the panel is re-initialised once. Headless is never
  final: while the display is unavailable the daemon retries it on a 30 s to
  15 min backoff, so a panel that was only slow to appear at boot recovers on
  its own instead of staying dark until a restart.
- A hung backup is no longer invisible: `idevicebackup2` is terminated after
  600 s without output (`backup.hang_timeout_sec`) and capped at 4 h total
  (`backup.max_duration_sec`), `idevice_id`/`idevicepair`/`df`/`pgrep` probes
  have timeouts, and a watchdog thread restarts the daemon if the main loop
  stalls. An unplugged phone is now detected within 30 s even if both udev and
  `idevicebackup2` misbehave.
- `systemctl stop iosbackupmachine` no longer orphans a running
  `idevicebackup2` (`KillMode=mixed`), and the daemon terminates a running
  backup child before exiting.
- A corrupt or truncated `config.yaml` no longer bricks the web UI and every
  route in it: the bad file is saved as `config.yaml.bad-*`, the device runs on
  defaults with a banner in the web UI, and the first-start wizard re-engages
  (previously a missing config silently disabled the web UI password). Config
  values with the wrong type are replaced by defaults with a warning instead of
  crashing pages. Concurrent saves no longer race, and the directory is fsynced
  after the atomic rename.
- The web UI gained a global error handler (friendly page instead of a bare
  500), every settings save is guarded, and syncs started from the web UI now
  survive a web UI restart (launched via `systemd-run`, like the updater).
  Purging logs no longer deletes the web UI's own open log file.
- Logging can no longer crash what it logs: a full or read-only disk makes the
  log writer degrade to the journal instead of raising into callers - including
  the daemon's own fatal-error handler. Per-run logs now have a 100 MB per-kind
  size cap in addition to the count/age pruning.
- `send_notification` and the credential decrypter no longer raise on malformed
  config or corrupt `.enc` files; they degrade to "no credentials/disabled".
- Sync launcher guards now fail closed (a failed probe skips the sync instead
  of allowing overlapping rsync/backup runs), skipped syncs write a
  `sync_skipped` status instead of leaving stale state, and a failed sync exits
  non-zero. Syncs now have an overall time cap (`sync.max_seconds`, default
  1 h) in addition to the stall watchdog.
- WireGuard: a `wg` command error is no longer indistinguishable from "no
  handshake yet", so the reconciler stops cycling the tunnel every minute when
  the probe itself fails; full-tunnel enforcement failures are reported instead
  of swallowed. WiFi: a failed netplan rollback is now reported ("WiFi may be
  down") instead of ignored, and clearing all networks reports real failures.
- A failed or interrupted upgrade no longer leaves the appliance dead: when the
  installer aborts after stopping services, it restores the previous install
  from its backup and restarts the web UI and daemon. The version file is only
  written when the health checks pass. The storage marker file is only created
  when the backup drive is actually mounted, so backups can no longer silently
  land on the root filesystem.
- Backups now refuse to start when disk space is critically low (previously a
  warning that was ignored) or when the backup storage is read-only, with a
  clear display/status message instead of a mid-backup failure.
- `usbmux-refresh.sh` checks and retries the usbmuxd restart; the WireGuard
  auto-connect script is serialized with `flock` so the boot service and the
  NetworkManager dispatcher can no longer race; rtc-sync output now reaches the
  web UI logs page.

- Reboot, shutdown and a system update no longer destroy work in flight. They
  were the only actions with no mutual exclusion at all, while starting a sync
  during a backup was already refused. Each now refuses once, names what is
  running and how to stop it, and offers an explicit override that stops the work
  through the sanctioned path first, so the run log still records why it ended.
  The check fails open, so a device that cannot answer "is a backup running" can
  always still be powered off.
- The log viewer could take the web UI down. It read the whole file and rendered
  it in full, and nothing capped a single run log, so the page opened when
  something had gone wrong was at its most dangerous when it was most needed. It
  now shows a bounded head and tail and says so, with a streamed download for the
  whole file, and the writer caps a single run, marks where it cut and appends the
  closing lines when the run ends. The daemon's own backup log is capped too - it
  is one file for the whole daemon lifetime, not one per backup.
- A backup had no battery protection, though a sync had it at both ends. A run
  starting at 33% was powered off mid-write by PiSugar's own 30% auto-shutdown.
  It now refuses below `backup.min_battery_percent` (default 35) and aborts if
  the battery falls during the run.
- The backup drive disappearing mid-run left `idevicebackup2` filling the rootfs
  through the empty mountpoint. The marker that distinguishes a mounted drive
  from an empty directory was only checked before the run; it is now checked
  every 10 s, alongside the device id of the mountpoint, and a lost drive is
  killed without the usual SIGTERM grace.
- Free space was measured once, against a 500 MB floor, for a job that writes
  tens of gigabytes - so the run died on ENOSPC deep in. It is now checked during
  the run against `backup.min_free_mb`, and the start floor rises with it so a
  run can never pass the gate and then abort on its own first poll.
- The completion check only tested that `Manifest.plist` parsed, which an
  interrupted run passes because the previous run's copy is still there. It now
  tests signals that actually prove a run finished, degrading to the old answer
  on a layout it does not recognise: calling a good backup bad would be worse.
- Uploads had no size limit, and a config import silently reset wrong-typed
  values. Uploads are now capped and the import says what it corrected.

### Added

- `ntp-sync.timer`: retries clock sync 2 min after boot and every 15 min after
  that (once synchronized, each run is one cheap early-exit probe) - a boot
  without internet no longer leaves the clock wrong forever (which broke
  WireGuard handshakes).
- A journal viewer in the web UI. Much of the daemons' diagnostics go to the
  systemd journal rather than to a log file - panel failures, watchdog trips, a
  stalled main loop - so that whole class of degradation was invisible from the
  device's own interface. The Logs page now links to a viewer over the units that
  matter, honest about how far back the journal goes on an appliance whose
  `/var/log` is a RAM disk.
- A `backup_stale` notification. Every other event is edge-triggered, so an
  appliance that quietly stopped backing up told nobody until a restore was
  needed. A durable last-success record now drives one alert per quiet episode
  (`backup.stale_after_sec`, default 7 days, `backup.notify_stale` to disable),
  with the state also on the panel and the health endpoint.
- New `backup:` settings, all file-only for now: `min_battery_percent`,
  `min_free_mb`, `notify_stale`, `stale_after_sec`.

### Changed

- Config schema version 3. The migration adds `backup_stale` only to
  notification event lists that already asked for `backup_error`: a list
  narrowed to successes is a deliberate choice, and adding the event to the
  defaults alone would have reached fresh installs only, never the devices that
  would benefit.

## [4.8.1] - 2026-09-02

### Fixed

- After a failed backup, "Start Backup" in the web UI did nothing while the
  error screen was up, and the request then fired later when the iPhone was
  next plugged in - looking like an unwanted automatic backup. One cause, two
  symptoms: the error screen's wait polled only for the cable being pulled, so
  the daemon was deaf to everything else for as long as the phone stayed in.
  The web UI meanwhile reported the request accepted, and the sentinel that
  carries it stays valid for 15 seconds, so a replug inside that window ran the
  backup the user had asked for a minute earlier.

  The wait now releases on the same three conditions as the post-backup hold -
  the phone leaving, a fresh manual request, or shutdown - and does so by
  calling the same `uipolicy.should_release_hold` rather than keeping its own
  copy of the rule, which is how the two drifted apart in the first place.

## [4.8.0] - 2026-09-02

### Added

- Sync failures now carry a structured, stable payload instead of a sentence.
  Every way a sync can fail maps to one of a fixed set of `reason_code` values
  (`ssh_connection_failed`, `stall_timeout`, `out_of_memory`, `battery_abort`
  and so on), and the failure object carries the exit code, how far the transfer
  got in bytes and percent, its duration, the last file rsync was on, and the
  post-mortem findings. The same object is the `[ERROR]` line in the log, the
  message on the e-ink and the dashboard, and the `sync_error` notification body
  delivered over MQTT and webhook - so the three can no longer disagree. The
  payload keeps `error` alongside `message` so an existing consumer still works.
- A failed sync now runs a post-mortem and records it in the log and in the
  notification: whether the kernel OOM killer took rsync, whether the remote
  answers on its SSH port now, how long ago WireGuard last handshook, and free
  space on the source. When rsync dies mid-transfer it often prints nothing at
  all, and these are the facts that are gone by the time anyone looks. Each
  probe is best-effort: one that cannot run says so, and the rest still report.
- Sync progress lines now carry bytes transferred against the total, speed,
  elapsed time, an ETA, and the byte delta since the previous line. The delta is
  what distinguishes a transfer parked on one percentage but still moving from
  one that is genuinely stuck. The ETA comes from a rolling ten-minute average
  of the transfer's own samples rather than rsync's instantaneous rate, and
  reads `ETA unknown` rather than guessing when nothing is moving. It is also
  shown on the dashboard's Remote Sync card.
- Sync logs now name the file rsync is on (`--out-format`), sampled at most once
  a minute, so a failure can be pinned to a file rather than to a bare
  percentage. rsync reports every file it sends, which on a first sync is six
  figures of them, so the log keeps a sample rather than the list. Directories
  and symlinks are filtered out by rsync's own itemize flag: rsync reports those
  through the same channel and creates them instantly, so one would otherwise
  displace the real file at the moment it matters. `--stats` appends the real
  totals when a run ends.
- A Network card on the dashboard: internet reachable or not, which link is
  carrying it (WiFi with its nickname, or the iPhone's USB hotspot), WireGuard
  up or down, and how long the current state has held. The appliance switches
  links on its own, so how long the current one has lasted is worth showing next
  to whether it is up. The probe runs on its own schedule and the page reads a
  cached answer, so polling it every 5 seconds costs nothing; `/api/health` reads
  the same cache and falls back to a direct probe when it is stale.
- `update.log` and the sync output redirected into `autostart.log` are now
  timestamped line by line, through a `logutil.py --stamp` filter that reuses the
  format the per-run logs already use. They were the last two logs whose lines
  could not be lined up against the others.

### Changed

- The sync log is now written entirely by `sync_manager`; the auto-sync (display
  daemon) and the manual sync (`backup-sync.py`) no longer format their own
  progress lines and can no longer drift apart. They drive the e-ink and the
  status file only.
- Sync progress is logged once a minute rather than every 30 seconds. The lines
  carry considerably more, and an eight-hour transfer now leaves a log in the
  low hundreds of lines instead of around a thousand near-identical ones.

### Fixed

- Webhook notifications were sent unauthenticated whenever the iPhone was not
  attached, and an authenticated endpoint answered 403 with no trace anywhere.
  The auth header is decrypted with the iPhone's serial number, but most of the
  events that carry it fire when the phone is gone: `device_disconnected` by
  definition, every `sync_*` after the phone was unplugged, and `backup_complete`
  - which is sent only after the e-ink has already displayed "Backup completed"
  and invited the user to unplug. `webhook_auth_headers` reported that failure as
  `{}`, indistinguishable from "no auth configured", so the request went out
  regardless.

  The resolved header is now cached in `/run/iosbackupmachine/webhook_auth.json`
  (0600 in a 0700 directory) and reused when the phone is absent. `/run` is a
  systemd tmpfs: RAM-only, wiped at every boot, never written to the SD card - so
  a powered-off device still yields nothing without the phone. Only the derived
  header is cached, never the passphrase, which is the phone's serial and also
  unlocks the WireGuard and remote-sync credentials. The cache is refreshed
  whenever the phone is seen, primed at the start of every backup, dropped when
  notification settings are saved, and can be given a lifetime with
  `IOSBACKUP_WEBHOOK_AUTH_TTL` (default 0, meaning until the next reboot).

  When auth is wanted and genuinely unobtainable the webhook is now skipped
  rather than sent blind, and the reason is written to the per-run log the web UI
  serves - on the Test Webhook button too, which calls the sender directly and
  needed the same check. Delivery failures - a 403, a refused connection, a
  missing paho-mqtt - now land in that log as well, instead of only in the
  journal.

- Notifications sent by a manual sync were silently discarded. `send_notification`
  dispatches webhook and MQTT deliveries on daemon threads and returns at once,
  and `backup-sync.py` calls `sys.exit(0)` immediately after reporting its
  result. Python terminates daemon threads at interpreter exit, so the POST was
  killed mid-request, every time, with no error recorded anywhere. Deliveries are
  now tracked and `notifications.flush()` waits for them; it is also registered
  with `atexit`, which runs before daemon threads are killed, so a caller that
  forgets to flush is still covered. Affected every sync started from the web UI,
  a long press or a double tap, and the low-battery refusal. The auto-sync after
  a backup was not affected, because the display daemon keeps running.

### Security

- The `[CMD]` line in a sync log published the SSH password in clear for
  password-authenticated syncs: the full argv was written verbatim, and for that
  auth method the argv is `sshpass -p <password> ...`. The log is served by the
  web UI at `/logs/<file>`, so anyone who could read a log could read the
  password. The password is now masked. Key-authenticated setups were never
  affected. Rotate the remote's password if you used password auth and any sync
  log may have been seen.

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
