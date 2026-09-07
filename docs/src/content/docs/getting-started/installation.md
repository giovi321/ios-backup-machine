---
title: "Installation"
description: Flash Armbian, run the automated installer, complete setup in the web UI, and keep the appliance updated.
---

Install the appliance in three moves: flash Armbian on the Radxa Zero 3W, run the automated installer over SSH, then finish setup in the web UI at `http://<device-ip>:8080`. A manual, step-by-step reference is included at the end for anyone who prefers to install by hand.

## Flash Armbian on the Radxa Zero 3W

Follow the [Radxa Zero 3W official guide](https://docs.radxa.com/en/zero/zero3/low-level-dev/install-os-on-emmc). In short:

```bash
apt install rkdeveloptool
rkdeveloptool db rk356x_spl_loader_ddr1056_v1.12.109_no_check_todly.bin
xz -d Armbian_community_25.11.0-trunk.334_Radxa-zero3_trixie_vendor_6.1.115_minimal.img.xz
rkdeveloptool wl 0 Armbian_community_25.11.0-trunk.334_Radxa-zero3_trixie_vendor_6.1.115_minimal.img
rkdeveloptool rd
```

## Automated install (recommended)

After flashing Armbian and logging in as root, clone the repository and run the installer:

```bash
cd /root
git clone https://github.com/giovi321/ios-backup-machine.git
bash ios-backup-machine/install.sh
```

The repository is cloned to `/root/ios-backup-machine/` and the application is installed to `/root/iosbackupmachine/` (a flat directory holding the venv and app files).

### What the installer does

- Enables I2C and SPI overlays in `/boot/armbianEnv.txt`
- Installs system packages (`libimobiledevice`, `python3`, `wireguard-tools`, and others)
- Creates a Python virtual environment and installs dependencies
- Clones and links the Waveshare e-Paper driver
- Copies application files to `/root/iosbackupmachine/`
- Migrates config, merging new defaults without overwriting existing settings, and reports which keys it added
- Installs the systemd units and udev rules, enables the six units the appliance needs, and starts any enabled timer instead of leaving it armed for the next boot
- Prepares the backup storage directory
- Downloads and configures the PiSugar UPS
- Runs a post-install health check
- Prompts to reboot when overlay changes were made or a release sets a reboot flag, so the always-on display daemon reloads

## Complete setup in the web UI

After the script finishes, open the web UI at `http://<device-ip>:8080` to complete the first-start wizard. See [First backup](../first-backup/) for the wizard steps and the first backup run.

## Updating

From the web UI: go to Tools > Update, click "Check for Updates", then "Install Update". The device reboots when the update finishes, so the web UI and the VPN are unreachable for about a minute. The e-ink shows `Updating - device will reboot` for the whole run.

From SSH:

```bash
bash /root/ios-backup-machine/update.sh
```

The update process:

- Stops all services before touching files
- Backs up the current installation, keeping the last 5 backups in `/root/iosbackupmachine-backups/`
- Pulls the latest code from GitHub
- Migrates config, adding new settings without overwriting your values
- Runs the installer with a post-install health check
- Restarts services, then reboots: always for a web UI update, and after a prompt over SSH when the release requires one

An update started from the web UI runs in its own systemd unit (`iosbackup-update`), not as a child of the web UI, so restarting the web UI partway through the install cannot cut it short. Follow it with `journalctl -u iosbackup-update -f`, or read `/var/lib/iosbackupmachine/update.log` afterwards.

<details>
<summary>Manual installation (step-by-step reference)</summary>

If you prefer to install manually, follow these steps after flashing Armbian.

#### Enable I2C and SPI

Edit `/boot/armbianEnv.txt`:

```text
overlays=rk3568-spi3-m1-cs0-spidev rk3568-i2c3-m0
overlay_prefix=rk35xx
```

Reboot.

#### Install dependencies

```bash
apt update
apt install -y python3 python3-venv python3-pil python3-periphery \
  libimobiledevice-1.0-6 libimobiledevice-utils usbmuxd \
  wireguard-tools iptables sshpass rsync netcat-traditional \
  iw wireless-tools git
```

WiFi is managed through the OS's existing netplan, systemd-networkd, and wpa_supplicant stack (the device has no NetworkManager). `iw` and `wireless-tools` read the connected SSID, and `iptables` is used by the WireGuard full-tunnel routing exception.

#### Create the Python virtual environment

```bash
python3 -m venv /root/iosbackupmachine
source /root/iosbackupmachine/bin/activate
pip install -r /root/ios-backup-machine/requirements.txt
deactivate
```

#### Clone repositories and install drivers

```bash
cd /root
git clone https://github.com/waveshareteam/e-Paper.git
git clone https://github.com/giovi321/ios-backup-machine.git
cp ios-backup-machine/app/epdconfig.py e-Paper/RaspberryPi_JetsonNano/python/lib/waveshare_epd/
```

The application files live under `app/`, the systemd units under `services/`, and the udev rules and the PiSugar config under `config/`. The installer flattens `app/` into `/root/iosbackupmachine/`, which is why the modules import each other by bare name.

Link the driver into the venv (adjust the Python version if needed):

```bash
ln -s /root/e-Paper/RaspberryPi_JetsonNano/python/lib/waveshare_epd   /root/iosbackupmachine/lib/python3.13/site-packages/
```

#### Install systemd and udev integrations

```bash
cp ios-backup-machine/config/*.rules /etc/udev/rules.d/
cp ios-backup-machine/services/*.service ios-backup-machine/services/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable iosbackupmachine.service webui.service ntp-sync.service \
  rtc-sync.service wg-autoconnect.service
systemctl enable --now ntp-sync.timer
udevadm control --reload-rules
udevadm trigger
```

`iosbackupmachine.service` is the always-on display daemon. Those five units plus the timer are exactly what the installer enables. The rest of `services/` (`backup-sync.service`, `unplug-notify.service`, `usbmux-refresh.service`) are one-shots started on demand by udev or the PiSugar button, so they are installed but deliberately not enabled. A sync started from the web UI runs in its own transient `iosbackup-web-sync` unit rather than through `backup-sync.service`.

`ntp-sync.timer` needs `--now`. Enabling a timer only arms it for the next boot, and the whole point of this one is to retry a clock sync that failed while the network was still coming up.

#### Prepare backup storage

```bash
mkdir -p /media/iosbackup
touch /media/iosbackup/.foldermarker
```

#### Configure the PiSugar UPS

Install from the official script and add the project config file:

```bash
wget https://cdn.pisugar.com/release/pisugar-power-manager.sh
bash pisugar-power-manager.sh -c release
rm /etc/pisugar-server/config.json
cp "/root/ios-backup-machine/config/[pisugar]config.json" /etc/pisugar-server/config.json
```

Quote that path. Unquoted, `[pisugar]` is a shell character class and the copy silently matches nothing. The file sets the three button gestures, the 30% `auto_shutdown_level`, and the soft power-off shell that lets the display daemon paint the owner screen before the board goes down.

Set the RTC time from the current time of the Radxa Zero:

```bash
apt install netcat-traditional
echo "rtc_pi2rtc" | nc -q 1 127.0.0.1 8423
```

Set the Radxa Zero time from the RTC at every boot:

```bash
systemctl enable rtc-sync.service
```

</details>

## Next step

Continue to [First backup](../first-backup/) to complete the first-start wizard and run your first backup.
