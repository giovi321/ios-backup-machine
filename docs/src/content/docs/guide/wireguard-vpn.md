---
title: "WireGuard VPN"
description: The built-in WireGuard client, its encrypted config, handshake-verified auto-connect, and the optional full tunnel that keeps local access.
---

The iOS Backup Machine ships a built-in WireGuard client. You upload a config through the web UI, it is stored encrypted, and a background reconciler brings the tunnel up automatically when a source you selected appears. The reconciler verifies a real WireGuard handshake, not just that the interface exists, and an optional full-tunnel mode routes all traffic through the VPN while keeping SSH and the web UI reachable from the LAN.

## Encrypted config

The config is uploaded through the web UI and stored encrypted at `/root/iosbackupmachine/wireguard.enc`. It is decrypted with the connected iPhone's serial or a custom passphrase. See [Security](../../architecture/security/) for how credential encryption works and how the two passphrase modes differ.

```yaml
wireguard:
  enabled: false
  interface_name: "wg0"
  auto_connect: false
  auto_connect_on: [iphone]
  full_tunnel: false
```

The interface defaults to `wg0`.

## Auto-connect triggers

Enable Auto-connect and pick the triggers: iPhone plugged in, WiFi available, and on boot. They are combinable. A background reconciler in the display daemon brings the tunnel up as soon as a selected connection source appears, so it connects reliably even after errors, reconnects, or a hotspot toggled on after the phone was already plugged in. It is not a one-off boot event.

```yaml
wireguard:
  auto_connect: true
  auto_connect_on: [iphone, wifi, boot]
```

### Handshake verification

The reconciler verifies that the tunnel actually handshakes, not just that the `wg0` interface exists. If the interface comes up but no handshake completes within a grace window, it tears the tunnel down and reconnects. Two common causes of a missing handshake:

- The endpoint is unreachable
- The clock is not yet NTP-synced, so the peer rejects the handshake

:::tip
Because a wrong clock breaks the handshake, keep NTP enabled. See [Networking](../networking/) for NTP sync over WiFi or the iPhone hotspot.
:::

### udid decryption mode

In `udid` decryption mode the config is decrypted with the iPhone serial, so the tunnel can only come up while the iPhone is readable. While it waits for the phone, the persisted WireGuard log lines show:

```text
Cannot decrypt: no iPhone connected
```

The iPhone must be trusted and readable for this, which the e-ink iPhone icon shows at a glance. See [Device connectivity](../../architecture/device-connectivity/) for how readability is detected.

## Full tunnel

Full tunnel is optional. It routes all traffic through the VPN and is re-applied on every connect. Use it when the remote sync server's IP overlaps the local WiFi subnet: a plain `wg-quick` full tunnel would send that same-subnet traffic out the WiFi instead of the tunnel. It requires `AllowedIPs = 0.0.0.0/0` in the WireGuard config.

```yaml
wireguard:
  full_tunnel: true
```

### Local access is preserved

Even with full tunnel on, SSH and the web UI stay reachable from the local network. Replies to connections that arrive on a non-VPN interface are kept off the tunnel via connection-mark policy routing (iptables `CONNMARK` plus an `ip rule`), while everything the device initiates still goes through the VPN.

## Status

WireGuard status is shown on the dashboard, the e-ink VPN icon, and `GET /api/status`. The VPN icon is crossed out with a "/" when the tunnel is inactive.

## Related

- [Security](../../architecture/security/) for credential encryption and passphrase modes
- [Device connectivity](../../architecture/device-connectivity/) for how iPhone readability is detected
