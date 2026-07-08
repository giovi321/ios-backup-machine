<p align="center">
  <img src="app/webui_static/icon.svg" alt="iOS Backup Machine" width="120" height="120">
</p>

<h1 align="center">iOS Backup Machine</h1>

<p align="center">
  <a href="https://github.com/giovi321/ios-backup-machine/actions/workflows/ci.yml"><img src="https://github.com/giovi321/ios-backup-machine/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/giovi321/ios-backup-machine/actions/workflows/docs.yml"><img src="https://github.com/giovi321/ios-backup-machine/actions/workflows/docs.yml/badge.svg" alt="Docs"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/giovi321/ios-backup-machine" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/version-4.4.3-brightgreen" alt="Version 4.4.3">
  <img src="https://img.shields.io/badge/Python-3.13-blue?logo=python&logoColor=white" alt="Python 3.13">
</p>

<p align="center">
  <a href="https://img.shields.io/badge/Board-Radxa%20Zero%203W-lightgrey.svg"><img src="https://img.shields.io/badge/Board-Radxa%20Zero%203W-lightgrey.svg" alt="Radxa Zero 3W"></a>
  <img src="https://img.shields.io/badge/Display-Waveshare%202.13%E2%80%9D%20ePaper-lightgrey.svg" alt="Waveshare 2.13 inch ePaper">
  <img src="https://img.shields.io/badge/UPS-PiSugar%203-green.svg" alt="PiSugar 3">
</p>

<p align="center">
  <a href="https://giovi321.github.io/ios-backup-machine/"><img src="https://img.shields.io/badge/Read_the_docs-2563eb?style=for-the-badge&logo=readthedocs&logoColor=white" alt="Read the documentation"></a>
</p>

Plug in an iPhone and it backs up on its own. No iCloud, no iTunes, no computer, no account.

The whole appliance runs on a Radxa Zero 3W (an upgraded Raspberry Pi Zero W) with a small e-ink screen and a battery UPS. When you connect a phone, it runs an encrypted `idevicebackup2` backup to the microSD card, shows progress and any errors on the display, and keeps a log you can read later. An optional rsync-over-SSH job ships each backup to a remote server, over WireGuard if you want it off your LAN.

Every backup stays on storage you control, and it can be restored anytime with tools from [libimobiledevice](https://libimobiledevice.org).

![Normal operation](./assets/images/Normal_operation.gif)

> **Personal appliance, not an internet service.** The web UI password is optional and basic, so keep the device on your LAN or reach it over the VPN. The backup payload is encrypted by iOS with a password that never leaves your iPhone.

## Quick start

Flash Armbian on the Radxa Zero 3W, then from the device as root:

```bash
cd /root
git clone https://github.com/giovi321/ios-backup-machine.git
bash ios-backup-machine/install.sh
```

The installer enables the I2C and SPI overlays, installs the packages, builds the Python environment, links the e-Paper driver, installs the systemd services and udev rules, prepares the backup storage, and sets up the PiSugar UPS. When it finishes, open `http://<device-ip>:8080` and complete the first-start wizard.

Full flashing steps, the manual install, and updating are in the [Installation guide](https://giovi321.github.io/ios-backup-machine/getting-started/installation/).

## What it does

- Starts a backup automatically when an iPhone is plugged in, using the iPhone's own encryption
- Shows progress, status, and errors on the e-ink display, with a status icon row for power, VPN, internet, WiFi, and a three-state iPhone icon
- Keeps running on a UPS so a power cut does not corrupt a backup or the filesystem
- Ships backups to a remote server over rsync and SSH, with a WireGuard client for off-LAN transfers
- Configures everything from a web UI, with a health endpoint for external monitoring
- Encrypts WireGuard and remote sync credentials at rest with AES-256-GCM

## Documentation

The full documentation is at **[giovi321.github.io/ios-backup-machine](https://giovi321.github.io/ios-backup-machine/)**:

- [Overview](https://giovi321.github.io/ios-backup-machine/getting-started/overview/) and [Hardware](https://giovi321.github.io/ios-backup-machine/getting-started/hardware/)
- [Installation](https://giovi321.github.io/ios-backup-machine/getting-started/installation/) and [First backup](https://giovi321.github.io/ios-backup-machine/getting-started/first-backup/)
- Guides for [backups](https://giovi321.github.io/ios-backup-machine/guide/backups/), [remote sync](https://giovi321.github.io/ios-backup-machine/guide/remote-sync/), [networking](https://giovi321.github.io/ios-backup-machine/guide/networking/), [WireGuard VPN](https://giovi321.github.io/ios-backup-machine/guide/wireguard-vpn/), the [web UI](https://giovi321.github.io/ios-backup-machine/guide/web-ui/), and [logs](https://giovi321.github.io/ios-backup-machine/guide/logs/)
- [Architecture](https://giovi321.github.io/ios-backup-machine/architecture/overview/), including [device connectivity](https://giovi321.github.io/ios-backup-machine/architecture/device-connectivity/) and [security](https://giovi321.github.io/ios-backup-machine/architecture/security/)

## Inspect your backups with Apple Juicer

To browse the backups this device creates, point [Apple Juicer](https://github.com/giovi321/apple-juicer) at the backup directory. It parses artifacts such as WhatsApp and Messages (with Photos, Notes, Calendar, and Contacts in progress), unlocks encrypted backups with the password, and lets you search everything in a browser. Docs: https://giovi321.github.io/apple-juicer/

## Contributing

Issues and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) and the [development docs](https://giovi321.github.io/ios-backup-machine/development/contributing/). Report security issues privately through [Security Advisories](https://github.com/giovi321/ios-backup-machine/security/advisories/new).

## License

MIT License. Includes code adapted from Waveshare's [e-Paper library](https://github.com/waveshareteam/e-Paper).
