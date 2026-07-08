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
- Log retention and handshake parsing (`test_logutil.py`): `logutil.prune_logs` keeping the newest N per kind, dropping files past max age, leaving non-per-run logs alone, and never raising on a missing directory, plus `wg_manager.latest_handshake` parsing the newest WireGuard handshake timestamp

## Hardware-independent by design

The app ships flat to `/root/iosbackupmachine/` on the device and imports its siblings by bare name (for example `import sync_manager`). The tests mirror that layout with a path shim in `tests/conftest.py`, which puts `app/` on `sys.path` so the same bare-name imports resolve. Nothing in the suite touches the e-paper display, the PiSugar UPS, or a connected iPhone, so the tests run unchanged on a developer machine or in CI.

## Run the tests

Install the runtime and test dependencies, then run pytest from the repository root:

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest
```

`requirements-dev.txt` adds only the test dependency (pytest); the runtime dependencies come from `requirements.txt`. See [Contributing](../contributing/) for the full local setup.

## Continuous integration

CI runs on GitHub Actions from `.github/workflows/ci.yml`. On every push and pull request it installs the same dependencies and runs `pytest -q` against a matrix of Python 3.11, 3.12, and 3.13. The matrix does not fail fast, so a failure on one Python version still reports the results for the others.
