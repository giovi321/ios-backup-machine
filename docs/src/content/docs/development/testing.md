---
title: "Testing"
description: What the pytest suite covers, why it needs no e-paper hardware, how to run it, and the CI setup.
---

The project has a pytest suite covering the parts of the app that parse, encrypt, migrate, and decide, and also the display daemon, the web UI, and the e-paper HAL themselves, with no e-paper panel, PiSugar UPS, or iPhone involved anywhere. The tests import the flat app modules through a path shim, so they run on any machine. This page lists what the suite covers, how to run it, and how continuous integration runs it on every push and pull request.

## What the suite covers

The tests live under `tests/`, one file per area:

- rsync progress parsing (`test_sync_progress.py`): `sync_manager.parse_progress_line` reading rsync `--info=progress2` output into bytes, percentage, speed, and computed total, including the no-match and zero-percent cases
- Remote-sync credential crypto (`test_sync_crypto.py`): encrypt and decrypt round-trips for `sync_crypto`, wrong-passphrase and missing-file returning `None`
- WireGuard credential crypto (`test_wg_crypto.py`): `wg_crypto` AES-GCM round-trip plus the XOR fallback when `cryptography` is unavailable, deterministic 32-byte key derivation, and passphrase resolution across explicit, UDID, and custom modes
- Webhook auth credential crypto (`test_notify_crypto.py`): `notify_crypto` round-trip, the webhook auth header assembly, and the `_send_webhook` (status, error) contract
- Config schema and migration (`test_config_schema.py`): defaults filling, existing values winning while sibling defaults still fill, input not mutated, atomic save/load round-trip under concurrency, a wrong-typed or null value falling back to its default with a warning while a corrupt or non-mapping file reads as degraded and is backed up, and each migration in turn: the WiFi networks seeded from the legacy single `ssid`/`password`, the `backup_stale` event reaching devices that already had notifications configured without touching a deliberately narrowed list, and the old hard-coded one-hour sync cap being cleared while a deliberately chosen one survives
- WiFi netplan generator (`test_wifi_manager.py`): `wifi_manager.build_netplan` producing valid netplan YAML, skipping blank SSIDs, quoting special characters, and setting the high WiFi route metric so the iPhone hotspot is preferred
- Power-aware battery logic (`test_power.py`): PiSugar reply parsing and `power.sync_allowed`, covering fail-open on an unreadable UPS, charging bypassing the threshold, and low battery refusing
- SSH host key pinning (`test_host_key.py`): `host_key` fingerprint normalization across the forms a user might paste (canonical, bare base64, padded, mixed case, a whole `ssh-keygen -lf` line), rejection of MD5 and malformed values, `ssh-keygen -lf` output parsing, match selection among several offered keys, the pinned `known_hosts` being owner-only and holding only the matching key, and the fail-closed mismatch / no-key / missing-tool paths
- Host key pinning wired into sync (`test_sync_host_key.py`): `_prepare_sync` and `test_connection` leaving the ssh options untouched when no fingerprint is configured, pinning `StrictHostKeyChecking=yes` against the verified file when one is, aborting before rsync or ssh runs on a mismatch, and cleaning up both temp files afterwards
- Screen-handover policy (`test_uipolicy.py`): `uipolicy.should_release_hold` deciding when the post-backup result screen may be given up, and the `InfoWindow` lifetime, single-shot restore, re-tap extension, cancellation, and resume merge
- Display wiring (`test_display_wiring.py`): the daemon's actual info-screen paths: updates held back and queued while a tap is showing, the queued screen landing on resume, a backup cancelling the window and its queue, and a tap during a sync showing info then handing the sync screen back. It imports `iosbackupmachine.py` for real, stubbing `waveshare_epd` (which ships with the panel) and `python-periphery` only when the real package cannot import, and skipping entirely if the import still fails
- Log retention, capping and line stamping (`test_logutil.py`): `logutil.prune_logs` keeping the newest N per kind, dropping files past max age, leaving non-per-run logs alone, enforcing the aggregate per-kind size cap oldest-first while never deleting the log being written, and never raising on a missing directory; the per-file cap suppressing output, announcing itself, keeping the run's last lines for the close, counting bytes already on disk on a reopen, and never letting a write raise even when the disk is full or read-only; `wg_manager.latest_handshake` parsing the newest WireGuard handshake timestamp; and `logutil.stamp_stream` prefixing shell-produced log lines with the same stamp the per-run logs use, stripping ANSI colour and leaving blank lines blank
- Sync log content and the failure payload (`test_sync_logging.py`): the three-way split of rsync's merged output into file names, progress samples and messages; the rolling rate window returning no ETA rather than a fabricated one when nothing is moving; the throttled progress writer's line content, its percentage-change and interval triggers, and the once-a-minute file sample; the `[CMD]` line masking an sshpass password; every failure reason code producing a message; and the post-mortem probes composing, skipping what has nothing to say, and surviving one that raises
- Daemon hardening (`test_daemon_hardening.py`): the largest file in the suite, covering the loop watchdog's stall verdict and exit code, the fatal-error exit paths, the drop to a headless panel after a streak of draw failures and the backoff that climbs back out of it, the pre-backup mount, free-space and battery gates, the in-run health probe's mount / disk / battery verdicts and their debounce rules, the backup completion check, and the quiet-device staleness verdict and its clock guards. Like `test_display_wiring.py` it imports the real daemon against a throwaway config and runtime directory
- Web UI fail-safe behavior (`test_webui_failsafe.py`): the global error handler rendering a 500 page instead of a Werkzeug traceback, a degraded config showing the banner and re-engaging the setup wizard, the upload size cap, the import reporting what it reset, the bounded log viewer and its streamed download, the journal viewer, the refuse-then-override flow for reboot, shutdown and update (including a broken busy probe never trapping the owner and the quiesce wait staying bounded), the health endpoint's stale-backup warning, the sync time limit round-tripping through the form in minutes, and web-launched syncs detached from `webui.service`'s cgroup so a restart cannot kill the rsync
- Sync launcher guards (`test_backup_sync_guards.py`): `backup-sync.py`'s mutex guards failing closed, so a probe that cannot run (a missing `pgrep`, an unparseable status file) reads as "assume busy, skip" rather than silently disabling the guard; every probe passing a timeout so it can actually reach a verdict; and no guard writing the status file, which used to clobber a running backup's own state
- System update launch (`test_system_update.py`): the updater running in its own transient systemd unit rather than as a child of `webui.service`, the non-interactive and auto-reboot flags reaching it, its output landing in `update.log` stamped line by line, the exit status surviving the stamper pipeline, paths with spaces quoted for the inner shell, the fallback to a bare `bash` where `systemd-run` is absent, and the updating sentinel the display daemon watches
- Webhook auth cache (`test_notify_auth_cache.py`): a resolved header being cached and answering once the phone is gone, the three-way distinction between no auth configured, a header, and auth configured but unobtainable, the cache file and directory being owner-only under `/run`, only the header being cached and never the passphrase, TTL expiry, priming while the phone is attached, and the webhook being skipped rather than sent unauthenticated
- Notification delivery (`test_notify_delivery.py`): `flush()` waiting for in-flight deliveries and timing out rather than hanging, the `atexit` registration saving a caller that forgets to flush, the pending list staying bounded, and a non-mapping notifications, webhook or mqtt section reading as disabled instead of raising
- e-paper HAL (`test_epdconfig.py`): `epdconfig` against a fake gpiochip, covering a re-init while the lines are still held (the driver calls `module_init()` from both `init()` and `init_fast()`), every line being released when an init fails part-way, an optional power pin that may be absent, and the stuck-BUSY timeout firing only on a real stall
- Network probes (`test_netutil.py`): every `netutil` probe failure still returning its safe default (`{}`, `None`, `False`) and now logging the reason instead of failing silently
- WireGuard status (`test_wg_manager.py`): `latest_handshake` distinguishing "never handshaked" (zero) from "could not be read" (`None`) across a command error, a missing `wg`, and a timeout, plus `stop_wireguard` warning when the full-tunnel rules cannot be cleared

## Hardware-independent by design

The app ships flat to `/root/iosbackupmachine/` on the device and imports its siblings by bare name (for example `import sync_manager`). The tests mirror that layout with a path shim in `tests/conftest.py`, which puts `app/` on `sys.path` so the same bare-name imports resolve. Nothing in the suite touches the e-paper display, the PiSugar UPS, or a connected iPhone, so the tests run unchanged on a developer machine or in CI.

Most modules are import-safe by design so they can be tested this way: logic that would otherwise sit inline in the display daemon lives in `config_schema.py`, `logutil.py`, `power.py`, `host_key.py`, `netutil.py`, and `uipolicy.py`, which depend only on the standard library.

Three files import a real entry point rather than a helper. `test_display_wiring.py` covers the screen-handover wiring and `test_daemon_hardening.py` covers the watchdogs and guards; both import `iosbackupmachine.py`, stubbing `waveshare_epd` and `python-periphery` only when the real packages cannot import on this machine, and skipping the whole module if the import still fails. `test_webui_failsafe.py` imports `webui.py` against a throwaway config and skips when Flask is absent. `test_epdconfig.py` exercises the e-paper HAL itself, against a fake gpiochip rather than the panel.

## Run the tests

Install the runtime and test dependencies, then run pytest from the repository root:

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest
```

`requirements-dev.txt` adds only the test dependency (pytest); the runtime dependencies come from `requirements.txt`. See [Contributing](../contributing/) for the full local setup.

## Continuous integration

CI runs on GitHub Actions from `.github/workflows/ci.yml`. On every push and pull request it installs the same dependencies and runs `pytest -q` against a matrix of Python 3.11, 3.12, and 3.13. The matrix does not fail fast, so a failure on one Python version still reports the results for the others.
