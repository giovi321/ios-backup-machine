---
title: "Networking"
description: How the appliance manages WiFi, roams between saved networks, prefers the iPhone hotspot, and keeps its clock in sync.
---

This page covers how the iOS Backup Machine connects to a network. WiFi is handled by the OS stack (netplan, systemd-networkd, wpa_supplicant), the device roams between the networks you save, and it prefers the iPhone USB hotspot while the phone is plugged in. Network access is optional: backups run fully offline, but a connection enables NTP sync, notifications, remote sync, and remote access.

## WiFi stack

WiFi is managed through the OS's existing netplan, systemd-networkd, and wpa_supplicant stack. The device has no NetworkManager. The `iw` and `wireless-tools` packages read the connected SSID, and `iptables` is used by the WireGuard full-tunnel routing exception.

When you save networks in the web UI, the appliance writes a managed netplan drop-in at `/etc/netplan/90-iosbackup-wifi.yaml` and applies it. The OS's own netplan files are left untouched.

:::note
Only the `90-iosbackup-wifi.yaml` drop-in is owned by the appliance. Your base OS network configuration is never rewritten.
:::

## Multiple networks and roaming

You can configure several networks, each with an optional nickname. wpa_supplicant associates to whichever configured network is in range, so the device roams automatically as you move between them.

Networks are stored in `config.yaml` under `wifi.networks`:

```yaml
wifi:
  enabled: false
  ssid: ""
  password: ""
  networks:
    - nickname: "Home"
      ssid: "HomeNetwork"
      password: "secret"
    - nickname: "Office"
      ssid: "CorpWiFi"
      password: "another-secret"
```

The legacy single `ssid` and `password` fields are the older single-network form. They are migrated into `networks[0]` automatically and kept mirrored for back-compat, so an existing single-network config keeps working.

## iPhone-hotspot preference

The WiFi route is given a higher metric than the iPhone USB tether's route. As a result, the iPhone hotspot is used when the phone is plugged in, and WiFi takes over automatically when the phone is unplugged. The WiFi connection is no longer dropped as the iPhone connects or disconnects.

## Scan and connect

The WiFi settings page has a Scan & connect button. It forces a rescan and connects to any saved network in range. Use it when the device is not associated to a network you expect to be reachable.

## Status sources

The connected SSID and its nickname are shown in three places: the dashboard, the e-ink info screen (single-tap system info), and `GET /api/status`. These values are read via `iw`, `iwgetid`, and `wpa_cli`.

The `/api/health` and `/api/status` responses expose the active interface and WiFi details, for example:

```json
"network": {
  "active_ip": "192.168.1.50",
  "interface": "wifi",
  "wifi_ssid": "HomeNetwork",
  "wifi_nickname": "Home",
  "internet": true
}
```

## NTP time sync

When `ntp.enabled` is true, the appliance auto-syncs its clock whenever internet is available, over either WiFi or the USB iPhone hotspot. A correct clock matters beyond timestamps: the WireGuard peer rejects handshakes when the clock is not yet NTP-synced.

```yaml
ntp:
  enabled: true
  servers:
    - "pool.ntp.org"
    - "time.google.com"
```

## Related

- [WireGuard VPN](../wireguard-vpn/) covers the VPN client and its WiFi and boot auto-connect triggers
- [Remote sync](../remote-sync/) covers restricting rsync to WiFi only, a specific SSID, or iPhone USB tethering
