---
title: "Security"
description: What the appliance encrypts, how the web UI authenticates, and the trust boundary it is designed for.
---

The appliance is built for a trusted network and keeps the iOS backup payload and every stored credential encrypted. It is not hardened for direct exposure to the public internet, so run it on your LAN or reach it over the VPN.

## What is encrypted

Backup payload. The backup itself is encrypted by iOS with a password you set on the iPhone. That password is sent to the phone during setup and is never stored on the appliance, so write it down. Without it a restore is not possible.

Credentials. WireGuard, remote sync, and webhook auth credentials are encrypted with AES-256-GCM, using a key derived through PBKDF2-HMAC-SHA256 (100,000 iterations). All three share one credential store, so all three answer to the same passphrase mode. The encrypted files in the install directory are:

| File | Holds |
|------|-------|
| `wireguard.enc` | The uploaded WireGuard config |
| `sync.enc` | The sync host, port, user, SSH key or password, remote path, and the pinned host key fingerprint |
| `notify.enc` | The webhook auth header name and its value |

The resolved webhook auth header is additionally cached in RAM at `/run/iosbackupmachine/webhook_auth.json`, owner-only, because most of the events that need it fire after the iPhone has been unplugged. `/run` is a systemd tmpfs: cleared at every boot and never written to the SD card, so a powered-off device still gives nothing away without the phone. Only the derived header is cached, never the passphrase, since the passphrase also unlocks the other two files. See [Web UI](../../guide/web-ui/#webhook-auth-and-the-iphone).

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

- Backups never leave the device unless you enable remote sync, and remote sync goes over SSH (optionally inside the VPN full tunnel), with optional host key pinning (below)
- The health endpoint at `GET /api/health` is login-exempt on purpose, but it contains no secrets: no owner info, credentials, or keys
- Config is written atomically (temp file, `fsync`, rename), so a power loss during a save cannot truncate `config.yaml`

## SSH host key verification

By default the sync trusts the first host key it sees (`StrictHostKeyChecking=accept-new`). That protects every later sync, but not the first one, and it accepts a new key silently if the server is ever replaced.

Turning on host key verification in Remote Sync settings pins a specific key instead. You store the server's SHA256 fingerprint; before any data moves, the device reads the keys the server offers, fingerprints each one, and only the key that matches is written to a throwaway `known_hosts` that ssh then checks strictly against. Pinning the key rather than only comparing the fingerprint means a substitution part-way through the connection is rejected by ssh itself.

It fails closed. A mismatch, an unreadable host key, or a corrupt stored fingerprint aborts the sync with a message naming the expected and offered fingerprints, rather than falling back to trusting the server. If you rebuild the server or rotate its host key, update the stored fingerprint; until you do, syncs stay blocked. The fingerprint is kept in the encrypted `sync.enc` alongside the host and credentials, though it is not itself a secret.

The **Fetch from server** button reads the offered fingerprints over the network, which is a convenience for filling the field, not proof of anything: confirm the value on the server itself with `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` before saving.

## Config integrity

`config.yaml` holds settings, the hashed web UI password, and the auto-generated Flask `secret_key`. It is written atomically (temp file, `fsync`, rename, then an `fsync` of the directory so the rename itself survives a power loss) and migrated automatically: on update, a single versioned migration fills in new defaults without overwriting your values. The `config_version` field is managed for you and should not be edited by hand. The current schema version is 4.

Reading it fails safe. Corrupt YAML, a truncated file that parses to a scalar, or a valid document that is not a settings mapping all yield the built-in defaults instead of an exception, and the unreadable original is preserved next to it as `config.yaml.bad-<timestamp>`. The web UI then shows a banner naming the specific problems.

That fallback has a consequence worth stating plainly: when the file is discarded, the saved web UI password hash goes with it, so the appliance is briefly unauthenticated. The first-start wizard re-engages in that state, which is the recovery path, but if the device is reachable from anywhere you do not fully trust, treat a config-reset banner as something to act on rather than dismiss.

A single mistyped key is handled without discarding anything: the value is replaced by its default, a warning is recorded, and the rest of the file is kept.

Config upload through the web UI is capped at 256 KB, and an imported file is migrated and default-filled before it is saved. See [Web UI](../../guide/web-ui/#config-export-and-import).
