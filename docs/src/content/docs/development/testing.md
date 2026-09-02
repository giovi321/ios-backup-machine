---
title: "Testing"
description: What the pytest suite covers, why it needs no e-paper hardware, how to run it, and the CI setup.
---

The project has a pytest suite that covers the hardware-independent core: the parts of the app that parse, encrypt, migrate, and decide, with no e-paper panel, PiSugar UPS, or iPhone involved. The tests import the flat app modules through a path shim, so they run on any machine. This page lists what the suite covers, how to run it, and how continuous integration runs it on every push and pull request.

## What the suite covers

The tests live under `tests/`, one file per area:

- rsync progress parsing (`test_sync_progress.py`): `sync_manager.parse_progress_line` reading rsync `--info=progress2` output into bytes, percentage, speed, and computed total, including the no-match and zero-percent cases
- Remote-sync credential crypto (`test_sync_crypto.py`): encrypt and decrypt round-trips for `sync_crypto`, wrong-passphrase and missing-file returning `None`
- WireGuard credential crypto (`test_wg_crypto.py`): `wg_crypto` AES-GCM round-trip plus the XOR fallback when `cryptography` is unavailable, deterministic 32-byte key derivation, and passphrase resolution across explicit, UDID, and custom modes
- Webhook auth credential crypto (`test_notify_crypto.py`): `notify_crypto` round-trip, the webhook auth header assembly, and the `_send_webhook` (status, error) contract
- Config schema and migration (`test_config_schema.py`): defaults filling, existing values winning while sibling defaults still fill, input not mutated, atomic save/load round-trip, and the WiFi-networks migration that seeds `networks` from the legacy single `ssid`/`password` fields
- WiFi netplan generator (`test_wifi_manager.py`): `wifi_manager.build_netplan` producing valid netplan YAML, skipping blank SSIDs, quoting special characters, and setting the high WiFi route metric so the iPhone hotspot is preferred
- Power-aware battery logic (`test_power.py`): PiSugar reply parsing and `power.sync_allowed`, covering fail-open on an unreadable UPS, charging bypassing the threshold, and low battery refusing
- SSH host key pinning (`test_host_key.py`): `host_key` fingerprint normalization across the forms a user might paste (canonical, bare base64, padded, mixed case, a whole `ssh-keygen -lf` line), rejection of MD5 and malformed values, `ssh-keygen -lf` output parsing, match selection among several offered keys, the pinned `known_hosts` being owner-only and holding only the matching key, and the fail-closed mismatch / no-key / missing-tool paths
- Host key pinning wired into sync (`test_sync_host_key.py`): `_prepare_sync` and `test_connection` leaving the ssh options untouched when no fingerprint is configured, pinning `StrictHostKeyChecking=yes` against the verified file when one is, aborting before rsync or ssh runs on a mismatch, and cleaning up both temp files afterwards
- Screen-handover policy (`test_uipolicy.py`): `uipolicy.should_release_hold` deciding when the post-backup result screen may be given up, and the `InfoWindow` lifetime, single-shot restore, re-tap extension, cancellation, and resume merge
- Display wiring (`test_display_wiring.py`): the daemon's actual info-screen paths — updates held back and queued while a tap is showing, the queued screen landing on resume, a backup cancelling the window and its queue, and a tap during a sync showing info then handing the sync screen back. This is the one module that imports `iosbackupmachine.py`; it stubs `waveshare_epd` (ships with the panel) and `python-periphery` only when the real package cannot import, and skips entirely if the import still fails
- Log retention, handshake parsing and line stamping (`test_logutil.py`): `logutil.prune_logs` keeping the newest N per kind, dropping files past max age, leaving non-per-run logs alone, and never raising on a missing directory; `wg_manager.latest_handshake` parsing the newest WireGuard handshake timestamp; and `logutil.stamp_stream` prefixing shell-produced log lines with the same stamp the per-run logs use, stripping ANSI colour and leaving blank lines blank
- Sync log content and the failure payload (`test_sync_logging.py`): the three-way split of rsync's merged output into file names, progress samples and messages; the rolling rate window returning no ETA rather than a fabricated one when nothing is moving; the throttled progress writer's line content, its percentage-change and interval triggers, and the once-a-minute file sample; the `[CMD]` line masking an sshpass password; every failure reason code producing a message; and the post-mortem probes composing, skipping what has nothing to say, and surviving one that raises

## Hardware-independent by design

The app ships flat to `/root/iosbackupmachine/` on the device and imports its siblings by bare name (for example `import sync_manager`). The tests mirror that layout with a path shim in `tests/conftest.py`, which puts `app/` on `sys.path` so the same bare-name imports resolve. Nothing in the suite touches the e-paper display, the PiSugar UPS, or a connected iPhone, so the tests run unchanged on a developer machine or in CI.

Most modules are import-safe by design so they can be tested this way: logic that would otherwise sit inline in the display daemon lives in `config_schema.py`, `logutil.py`, `power.py`, `host_key.py`, and `uipolicy.py`, which depend only on the standard library. `test_display_wiring.py` is the exception — it imports the daemon itself to cover the screen-handover wiring, substituting the two dependencies that may not import off-device.

## Run the tests

Install the runtime and test dependencies, then run pytest from the repository root:

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest
```

`requirements-dev.txt` adds only the test dependency (pytest); the runtime dependencies come from `requirements.txt`. See [Contributing](../contributing/) for the full local setup.

## Continuous integration

CI runs on GitHub Actions from `.github/workflows/ci.yml`. On every push and pull request it installs the same dependencies and runs `pytest -q` against a matrix of Python 3.11, 3.12, and 3.13. The matrix does not fail fast, so a failure on one Python version still reports the results for the others.
