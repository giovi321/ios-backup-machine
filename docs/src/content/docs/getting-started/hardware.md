---
title: "Hardware"
description: The bill of materials and the software stack that make up the iOS Backup Machine appliance.
---

The appliance is built from four hardware parts (a Radxa Zero 3W, a Waveshare e-ink HAT, a PiSugar 3 UPS, and a microSD card) inside a 3D printed case, running an Armbian and Python software stack. This page lists each part and its role.

## Bill of materials

| Component | Purpose | Rationale |
|-----------|---------|-----------|
| Radxa Zero 3W (8GB eMMC) | Main controller | eMMC is faster and more reliable than a microSD |
| Waveshare 2.13" e-Paper HAT V4 (250x122) | Status display | Persistent output, readable, low power |
| PiSugar 3 | UPS and safe shutdown | Prevents corruption on power loss |
| MicroSD card | Backup storage | Dedicated storage separate from the OS |
| 3D printed case | Enclosure | Based on the design by PiSugar and edited for this purpose |

:::note
The 3D printed case is based on the PiSugar case design, edited to fit this build.
:::

## Software stack

| Component | Role |
|-----------|------|
| Armbian (Trixie) | Base OS |
| Python 3.13 | Runtime for backup and display scripts |
| libimobiledevice | iPhone communication (`idevicebackup2`, `idevicepair`) |
| udev and systemd | Automation and event handling |
| netplan, systemd-networkd, wpa_supplicant | WiFi management (no NetworkManager) |
| Flask | Web UI for settings |
| WireGuard | Optional VPN client (auto-connect, optional full tunnel) |
