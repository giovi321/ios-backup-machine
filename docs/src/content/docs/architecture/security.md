---
title: "Security"
description: What the appliance encrypts, how the web UI authenticates, and the trust boundary it is designed for.
---

The appliance is built for a trusted network and keeps three things encrypted: the iOS backup payload, the WireGuard config, and the remote sync credentials. It is not hardened for direct exposure to the public internet, so run it on your LAN or reach it over the VPN.

## What is encrypted

Backup payload. The backup itself is encrypted by iOS with a password you set on the iPhone. That password is sent to the phone during setup and is never stored on the appliance, so write it down. Without it a restore is not possible.

Credentials. WireGuard and remote sync credentials are encrypted with AES-256-GCM, using a key derived through PBKDF2 (100,000 iterations). The encrypted files are `wireguard.enc` and `sync.enc` in the install directory.

## Passphrase modes

The credential passphrase has two modes, chosen in the WireGuard settings:

| Mode | Passphrase | Auto-start | Protects against |
|------|------------|------------|------------------|
| iPhone UDID (default) | The iPhone's unique device ID | Yes, when the iPhone is connected | Theft of the device without the iPhone |
| Custom password | A password you choose | No, entered manually | Guessing, up to the password strength |

In UDID mode the config is decrypted with the connected iPhone's serial, which is why the VPN can only come up while the phone is readable. See [Device connectivity](../device-connectivity/). You can decrypt from the CLI with `python3 wg_crypto.py decrypt`, which uses the UDID when available and otherwise prompts.

## Web UI authentication

The web UI password is optional. You can set one during the first-start wizard or later from the Password page. Once set, every page requires login, and the password is stored as a salted SHA-256 hash in `config.yaml`. You can change or remove it at any time.

Because the login is basic and has no rate limiting or lockout, treat it as a convenience for a private network rather than a barrier against the internet. Bind the web UI to the interfaces you trust (the Web UI settings page controls this) and reach it over the VPN when you are away.

## Trust boundary

The appliance is designed to run offline or on a network you control:

- Backups never leave the device unless you enable remote sync, and remote sync goes over SSH (optionally inside the VPN full tunnel)
- The health endpoint at `GET /api/health` is login-exempt on purpose, but it contains no secrets: no owner info, credentials, or keys
- Config is written atomically (temp file, `fsync`, rename), so a power loss during a save cannot truncate `config.yaml`

## Config integrity

`config.yaml` holds settings, the hashed web UI password, and the auto-generated Flask `secret_key`. It is written atomically and migrated automatically: on update, a single versioned migration fills in new defaults without overwriting your values. The `config_version` field is managed for you and should not be edited by hand.
