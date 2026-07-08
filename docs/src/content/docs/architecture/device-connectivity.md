---
title: "Device connectivity"
description: How the daemon keeps a hot-plugged iPhone visible to usbmux and verifies the VPN actually handshakes.
---

The daemon works around two platform quirks so that plugging in an iPhone "just works" regardless of order or lock state: a usbmux hot-plug gap that hides devices connected after boot, and a VPN that can look up without ever handshaking. Both are handled inside the always-on display daemon, so there is no dependency on udev delivering an event at the right moment.

## The usbmux hot-plug gap

On this Armbian image, `usbmuxd` runs as a persistent service but does not receive libusb hot-plug events. A phone plugged in after boot is enumerated by the kernel (it shows up in `lsusb` as an Apple device, vendor `05ac`) but stays invisible to `idevice_id` and every other `libimobiledevice` tool until `usbmuxd` re-scans. Since the daemon detects an iPhone by polling `idevice_id`, an invisible device blocks backup detection, the trust icon, and the VPN's config decryption at the same time.

The daemon closes the gap itself. On each idle poll, if it finds no device through `idevice_id` but the kernel has enumerated an Apple device in sysfs (`/sys/bus/usb/devices/*/idVendor` reads `05ac`), it restarts `usbmuxd` so the device appears.

The restart is guarded so it never misfires or storms:

- It never runs during a backup, which would drop the `idevicebackup2` usbmux session
- It tracks a sysfs signature of the device (bus path, `devnum`, interface and configuration counts). A fresh plug, or a re-enumeration when you unlock a phone, changes the signature and triggers a restart at once
- A device that stays invisible is retried with exponential backoff (8 seconds up to 120 seconds), so unlocking it later is picked up quickly, and a phone left plugged in and locked does not restart `usbmuxd` on a tight loop

Once `usbmuxd` sees the phone, the backup path and the VPN reconciler both proceed on their next cycle.

:::note[iOS USB Restricted Mode]
A phone that has been locked for about an hour puts its USB data port into Restricted Mode: it charges but exposes no data, so no software on this side can reach it. Unlock the phone once and the daemon picks it up within a few seconds. This is an iOS security feature, not something the appliance can bypass.
:::

## Three trust states

The iPhone status icon reflects what the daemon can actually read, which is what the rest of the system depends on:

- Absent: no iPhone is visible on USB
- Untrusted (closed padlock): the phone is plugged in but lockdown data is not readable yet, either because Trust has not been granted or the phone is locked
- Trusted (checkmark): lockdown data (the serial number) is readable

The trusted state matters beyond the icon. In `udid` decryption mode the WireGuard config is decrypted with the iPhone serial, so the tunnel can only come up once the phone is readable. See [WireGuard VPN](../../guide/wireguard-vpn/) and [Security](../security/).

## Handshake-verified VPN

The VPN reconciler treats a real WireGuard handshake as the definition of "connected", not the mere existence of the `wg0` interface. A tunnel can come up and then never handshake, for example when the endpoint is unreachable or the clock is not yet NTP-synced and the peer rejects the exchange.

The reconciler runs in the display daemon and does two things:

- Brings the tunnel up as soon as a selected auto-connect source appears (iPhone plugged in, WiFi available, or on boot), so it recovers after errors, reconnects, or a hotspot toggled on after the phone was already plugged in, not just on a one-off boot event
- Checks that a handshake completes within a grace window. If the interface is up but no handshake lands, it tears the tunnel down and reconnects

In `udid` mode the reconciler waits for a readable iPhone before it can decrypt the config, and the persisted WireGuard log records `Cannot decrypt: no iPhone connected` while it waits.

For the user-facing view of triggers and the full-tunnel option, see [WireGuard VPN](../../guide/wireguard-vpn/). For the icons, see [Display and controls](../../guide/display-and-controls/).
