[![License](https://img.shields.io/github/license/giovi321/ios-backup-machine)](LICENSE)
![Version](https://img.shields.io/badge/version-2.0-brightgreen)
[![Python 3.13](https://img.shields.io/badge/Python-3.13-blue?logo=python&logoColor=white)](https://www.python.org/downloads/release/python-3130/)
[![Armbian](https://img.shields.io/badge/OS-Armbian-orange?logo=armbian)](https://www.armbian.com/radxa-zero-3/)
![Offline](https://img.shields.io/badge/network-optional-blue.svg)

[![Hardware](https://img.shields.io/badge/Board-Radxa%20Zero%203W-lightgrey.svg)](https://radxa.com/products/zeros/zero3w/)
[![Display](https://img.shields.io/badge/Display-Waveshare%202.13%E2%80%9D%20ePaper-lightgrey.svg)](https://github.com/waveshareteam/e-Paper)
[![UPS](https://img.shields.io/badge/UPS-PiSugar%203-green.svg)](https://github.com/PiSugar)

[![Issues](https://img.shields.io/github/issues/giovi321/ios-backup-machine)](https://github.com/giovi321/ios-backup-machine/issues)



# iOS Backup Machine
**Offline, portable and automatic iPhone backup system** running entirely on a **Radxa Zero 3W** (an upgraded Raspberry Pi Zero W).  
When you plug in your iPhone, the system automatically runs an encrypted `idevicebackup2` backup to local storage, shows progress and messages on an e-ink display, and logs all activity locally — **no iCloud, no iTunes, you own your data.**

![Normal_operation](https://github.com/user-attachments/assets/a126f7bf-9173-4a85-b462-f8181bd77aae)

![Image2](https://github.com/user-attachments/assets/d473ad1f-d80a-4214-8b85-8363d4b1f9a8)


## Objective
A **self-contained iOS backup appliance** with no reliance on Apple services or computers.  
All backups stay local on the microSD card and can be restored anytime using tools from [libimobiledevice](https://libimobiledevice.org).

### Key features
- **Fully automated**: starts as soon as an iPhone is plugged in.  
- **Live feedback**: e-ink shows progress, status, and errors.  
- **Secure**: backups use the iPhone's own encryption credentials.  
- **Offline and independent**: no Apple ID, no iTunes, no Internet required.
- **Solid**: file corruption is prevented by a small UPS.
- **Web UI**: configure all settings from a browser.
- **NTP sync**: auto-syncs clock when internet is available (WiFi or USB iPhone hotspot).
- **Notifications**: webhook and MQTT alerts for backup events.
- **WireGuard VPN**: built-in client with iPhone-encrypted config.
- **Power-on indicator**: visible on every e-ink screen.

## How it works

### Normal operation
- Turn on the iOS Backup Machine (press once the UPS's power button and then keep pressed until all LEDs light up)
- Wait for the boot to complete (you will see a screen refresh)
- Plug in your iPhone → the backup starts automatically.
- The display:
  - prompts to unlock the phone if needed
  - shows encryption status
  - shows progress percentage
- At the end:
  - displays success confirmation and timestamp  
  - shows owner info (persistent on screen even after power off or power loss)

**In case you unplug the iPhone** the process stops safely and the screen shows the interruption timestamp.

### Interactive functions
- If you push the additional button of the PiSugar UPS, it shows for 10 seconds on the e-ink the last backup timestamp and available memory. After 10 seconds it goes back to owner information.
- **IP address display**: press the PiSugar button when idle (not during backup) to show the current IP address and connection type (WiFi or USB iPhone hotspot) for 10 seconds.
- A small **power-on icon** is displayed in the bottom-left corner of every screen.

### UPS integration
- **Battery protection**: backup stops cleanly if battery <30%.  
- **Safe shutdown**: power loss or UPS switch-off triggers graceful shutdown.  
- Prevents data corruption during unexpected disconnections.

### General behavior
- Errors appear directly on the display.  
- Logs are written under `/var/log/iosbackupmachine/`.  
- On boot, owner info is displayed.  
- When idle, the screen shows last backup result, timestamp, disk usage, and owner info.


## Hardware

| Component | Purpose | Rationale |
|------------|----------|-----------|
| **Radxa Zero 3W (8GB eMMC)** | Main controller | eMMC is faster and more reliable than a microSD |
| **Waveshare 2.13" e-Paper HAT V4 (250×122)** | Status display | Persistent output, readable, low power |
| **PiSugar 3** | UPS and safe shutdown | Prevents corruption on power loss |
| **MicroSD card** | Backup storage | Dedicated storage separate from OS |
| **3D printed case** | --- | Based on the design by PiSugar and edited for this purpose |


## Software

| Component | Role |
|------------|------|
| **Armbian (Trixie)** | Base OS |
| **Python 3.13** | Runtime for backup and display scripts |
| **libimobiledevice** | iPhone communication (`idevicebackup2`, `idevicepair`) |
| **udev + systemd** | Automation and event handling |
| **Flask** | Web UI for settings |
| **WireGuard** | Optional VPN client |


## Directory layout

```
/root/
├── 90-iosbackupmachine.rules        # Starts backup on iPhone plug-in and stops it when unplugged
├── armbianEnv.txt                   # Overlays for SPI/I2C
├── Case ios backup machine_v8.stl   # 3D printable case
├── config.yaml                      # Main configuration (YAML, all settings)
├── epdconfig.py                     # Display configuration
├── iosbackupmachine_launcher.sh     # Launch script
├── iosbackupmachine.py              # Main program
├── iosbackupmachine.service         # Systemd service triggered by udev
├── last-backup.py                   # Shows last backup info and memory available
├── last-backup.service              # Systemd service for above
├── netutil.py                       # Network utilities (IP detection)
├── notifications.py                 # Webhook and MQTT notification module
├── ntp-sync.py                      # NTP time sync script
├── ntp-sync.service                 # Systemd service for NTP sync
├── owner-message.py                 # Shows owner info on the e-ink screen
├── owner-message.service            # Systemd service for above
├── [pisugar]config.json             # UPS configuration template
├── requirements.txt                 # Python dependencies
├── rtc-sync.service                 # Syncs the Radxa Zero clock to the RTC at boot
├── shutdown.sh                      # Shows owner info on screen and turns off device
├── UbuntuMono-Regular.ttf           # Font for the display, you can choose your own
├── unplug-notify.py                 # Handle unplug events
├── unplug-notify.service            # Service triggered by unplug rule
├── unplug-notify.sh                 # Script to call unplug-notify.py
├── webui.py                         # Flask web UI application
├── webui.service                    # Systemd service for web UI
├── webui_static/                    # Static assets for web UI
│   └── icon.svg                     # Project icon
├── webui_templates/                 # HTML templates for web UI
├── wg_crypto.py                     # WireGuard config encryption (iPhone UUID)
└── wg_manager.py                    # WireGuard interface management
```

## Configuration

Edit `/root/config.yaml` directly or use the **Web UI** at `http://<device-ip>:8080`.

All settings are stored in a single YAML file. The web UI reads and writes to this file directly.

```yaml
# --- Core backup settings ---
backup_dir: /media/iosbackup/
marker_file: .foldermarker
disk_device: /dev/mmcblk1
orientation: landscape_right
font_path: "/root/UbuntuMono-Regular.ttf"
owner_lines:
  - "Property of Titius Caius"
  - "+33 123 456 7890"
  - "write@titiuscaius.com"
  - "Reward if found €€€"
error_codes:
  "100": "Snapshot failure at path"
  # ...

# --- Authentication ---
auth:
  password_hash: ""   # auto-managed; set via web UI

# --- Backup behavior ---
backup:
  auto_start: true              # start backup when iPhone is plugged in
  notify_on_rejected: true      # notify when a non-allowed device is connected

# --- Backup encryption ---
backup_encryption:
  encryption_confirmed: false   # set true once encryption verified enabled

# --- Device filter ---
device_filter:
  enabled: false
  allowed_devices:              # list of {udid, name} dicts
    # - udid: "00008030-001A..."
    #   name: "John's iPhone 15"

# --- WiFi ---
wifi:
  enabled: false
  ssid: ""
  password: ""

# --- NTP time sync ---
ntp:
  enabled: true
  servers:
    - "pool.ntp.org"
    - "time.google.com"

# --- Web UI ---
webui:
  enabled: true
  port: 8080
  bind_interfaces:
    - "all"          # options: all, wifi, usb_iphone
  secret_key: ""     # auto-generated on first start

# --- Notifications ---
notifications:
  webhook:
    enabled: false
    url: ""
    events: [backup_complete, backup_error]
  mqtt:
    enabled: false
    broker: ""
    port: 1883
    username: ""
    password: ""
    topic_prefix: "iosbackupmachine"
    events: [backup_complete, backup_error]

# --- WireGuard client ---
wireguard:
  enabled: false
  interface_name: "wg0"
```

**Notes**
- The `.foldermarker` file confirms the SD card is mounted correctly.  
- Edit `owner_lines` to customize contact info shown on the e-ink display.  
- `disk_device` allows monitoring disk usage after backup.
- **WireGuard config** is stored separately in `/root/wireguard.enc`, encrypted with the iPhone's UUID.
- **`secret_key`** is auto-generated on first start and persisted to `config.yaml`. Never needs manual editing.



## Installation

### 1. Flash Armbian on the Radxa Zero 3W
Follow the [Radxa Zero 3W official guide](https://docs.radxa.com/en/zero/zero3/low-level-dev/install-os-on-emmc).  
In short:
```bash
apt install rkdeveloptool
rkdeveloptool db rk356x_spl_loader_ddr1056_v1.12.109_no_check_todly.bin
xz -d Armbian_community_25.11.0-trunk.334_Radxa-zero3_trixie_vendor_6.1.115_minimal.img.xz
rkdeveloptool wl 0 Armbian_community_25.11.0-trunk.334_Radxa-zero3_trixie_vendor_6.1.115_minimal.img
rkdeveloptool rd
```

### 2. Automated install (recommended)

After flashing Armbian and logging in as root, run:
```bash
cd /root
git clone https://github.com/giovi321/ios-backup-machine.git
bash ios-backup-machine/install.sh
```

The install script automatically performs all remaining setup steps:
- Enables I2C and SPI overlays in `/boot/armbianEnv.txt`
- Installs system packages (`libimobiledevice`, `python3`, `wireguard-tools`, etc.)
- Creates a Python virtual environment and installs dependencies
- Clones and links the Waveshare e-Paper driver
- Copies application files to `/root`
- Installs systemd services and udev rules
- Prepares the backup storage directory
- Downloads and configures PiSugar UPS
- Prompts to reboot if overlay changes were made

After the script finishes, open the web UI at `http://<device-ip>:8080` to complete the first-start wizard.

---

<details>
<summary><strong>Manual installation (step-by-step reference)</strong></summary>

If you prefer to install manually, follow these steps after flashing Armbian:

### 2. Enable I2C and SPI
Edit `/boot/armbianEnv.txt`:
```
overlays=rk3568-spi3-m1-cs0-spidev rk3568-i2c3-m0
overlay_prefix=rk35xx
```
Reboot.

### 3. Install dependencies
```bash
apt update
apt install -y python3 python3-venv python3-pil python3-periphery \
  libimobiledevice-1.0-6 libimobiledevice-utils usbmuxd \
  wireguard-tools
```

### 4. Create Python virtual environment
```bash
python3 -m venv /root/iosbackupmachine
source /root/iosbackupmachine/bin/activate
pip install -r /root/ios-backup-machine/requirements.txt
deactivate
```

### 5. Clone repositories and install drivers
```bash
cd /root
git clone https://github.com/waveshareteam/e-Paper.git
git clone https://github.com/giovi321/ios-backup-machine.git
cp ios-backup-machine/epdconfig.py e-Paper/RaspberryPi_JetsonNano/python/lib/waveshare_epd/
```

Link the driver into the venv (adjust Python version if needed):
```bash
ln -s /root/e-Paper/RaspberryPi_JetsonNano/python/lib/waveshare_epd   /root/iosbackupmachine/lib/python3.13/site-packages/
```

### 6. Install systemd and udev integrations
```bash
cp ios-backup-machine/*.rules /etc/udev/rules.d/
cp ios-backup-machine/*.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable boot-message
systemctl enable webui.service
systemctl enable ntp-sync.service
udevadm control --reload-rules
```

### 7. Prepare backup storage
```bash
mkdir -p /media/iosbackup
touch /media/iosbackup/.foldermarker
```

### 8. Configure PiSugar UPS
Install from official script and add out config file:
```bash
wget https://cdn.pisugar.com/release/pisugar-power-manager.sh
bash pisugar-power-manager.sh -c release
rm /etc/pisugar-server/config.json
cp /root/ios-backup-machine/[pisugar]config.json /etc/pisugar-server/config.json
```
Set the time of the RTC based on the current time of the Radxa Zero:
```bash
apt install netcat-traditional
echo "rtc_pi2rtc" | nc -q 1 127.0.0.1 8423
```
Automatically set the time of the Radxa Zero based on the RTC time at every boot
```bash
systemctl enable rtc-sync.service
```

</details>

## First run

1. Open the web UI at `http://<device-ip>:8080`. The **first-start wizard** will guide you through owner info, backup directory, encryption password, display orientation, and optional web UI password.
2. **Backup encryption**: during setup (or later via **Encryption** in the sidebar), enter a password with your iPhone connected and unlocked. The password is sent directly to the iPhone and **never stored on this device** — write it down.
3. If no iPhone is connected during setup, skip the encryption step and visit the **Encryption** page later when your iPhone is plugged in.
4. Plug in your iPhone, unlock it, and tap **Trust** when prompted.
5. The first backup runs (this takes a long time depending on device storage). All subsequent backups are incremental and much faster.
6. **If the first backup is interrupted**: encryption (if enabled) remains active on the iPhone. The next backup attempt will proceed normally — no data is lost.

**Manual alternative** (without the web UI):
```bash
idevicebackup2 encryption on <your-password> /media/iosbackup
idevicebackup2 backup --full /media/iosbackup
```

## Restoring a backup
To restore a backup simply plug your iOS device in a computer, plug the microSD card in the same computer and run:
```
idevicebackup2 restore --password <your backup password> /media/sdcard/iosbackup/
```

## Logs
All logs are stored under `/var/log/iosbackupmachine/`.  
Each run creates a file:  
```
backup-YYYYMMDD-HHMMSS.log
```

## Web UI

Access the web interface at `http://<device-ip>:8080`.

### First-start wizard

On the very first boot (when owner info has not been configured), the web UI automatically shows a **guided setup wizard** that walks you through:
1. **Owner information** — displayed on the e-ink screen when idle
2. **WiFi** (optional) — connect to a wireless network for NTP sync, notifications, and remote access
3. **Date & Time** — set the system clock manually or enable automatic NTP synchronization
4. **Backup directory** — where backups are stored
5. **Backup encryption** — set directly on your iPhone (password is never stored on this device)
6. **Device filter** (optional) — restrict which iPhones can trigger a backup; auto-detects connected device
7. **Notifications** (optional) — webhook and MQTT alerts for backup events
8. **Display orientation** — landscape left or right
9. **Web UI password** (optional) — protect the settings interface

The Flask session `secret_key` is automatically generated on first start and saved to `config.yaml` — no manual configuration needed.

### Settings pages

- **Backup Settings**: auto-start toggle, notification on rejected devices
- **Device Filter**: allow only specific iPhones by UDID (auto-detect connected device or manual entry)
- **Encryption**: enable/change backup encryption on connected iPhone (password never stored)
- **General**: backup directory, display orientation, owner information
- **Date & Time**: manual date setting, NTP sync configuration
- **WiFi**: enable/disable, SSID and password
- **Notifications**: webhook URLs and MQTT broker settings
- **WireGuard**: upload and encrypt VPN config, start/stop interface, backup encryption key
- **Web UI**: select which network interfaces the web UI listens on
- **Password**: protect the web UI with a password (set, change, or remove)
- **Logs**: browse and view backup log files directly from the browser

### Authentication

By default the web UI has no password (or you can set one during the first-start wizard).  
Once set, all pages require login. The password is hashed (SHA-256 + salt) and stored in `config.yaml`.  
You can change or remove the password at any time from the **Password** page.

## WireGuard Encryption

WireGuard configuration is encrypted using a key derived from the connected iPhone's UUID (UDID).  
This means the VPN credentials are protected even if the device is lost.

- **Encrypt config**: connect iPhone, then upload WireGuard `.conf` via web UI or CLI
- **Backup key**: via web UI button or `python3 wg_crypto.py backup-key`
- **Decrypt via CLI**: `python3 wg_crypto.py decrypt`
- **Show key**: `python3 wg_crypto.py show-key` (requires iPhone connected)

## Notifications

Backup events can be sent via **webhook** (JSON POST) and/or **MQTT**.  
Supported events: `backup_start`, `backup_complete`, `backup_error`, `device_connected`, `device_disconnected`, `device_rejected`.

Configure via the web UI or directly in `config.yaml`.

## Device Filter

Restrict which iPhones can trigger a backup:
1. Enable the filter in **Device Filter** settings.
2. Add devices by connecting an iPhone and clicking "Add connected device", or enter a UDID manually.
3. When a non-allowed device is plugged in, backup is blocked and a notification is sent (configurable).

When the filter is disabled (default), any iPhone triggers a backup.

## Feature checklist
- [x] Add partial refresh of the display (only when showing the backup progress percentage)
- [x] NTP time sync (WiFi or USB iPhone hotspot)
- [x] Power-on icon on every screen
- [x] IP address display on button press (idle only)
- [x] Web UI for all settings
- [x] Notifications via webhook/MQTT
- [x] WireGuard VPN client
- [x] Web UI interface binding selection
- [x] All config in YAML file
- [x] Encrypted WireGuard settings (iPhone UUID as key)
- [x] Filter iOS devices connected (UDID allow-list with auto-detect)
- [x] Web UI password authentication
- [x] Auto-backup toggle (enable/disable from web UI)
- [x] Log viewer in web UI
- [x] Guided first-start setup wizard
- [x] Auto-generated session secret key
- [x] Backup encryption management (set, change, auto-enable via web UI)

## License
MIT License  
Includes code adapted from Waveshare’s [e-Paper library](https://github.com/waveshareteam/e-Paper).
