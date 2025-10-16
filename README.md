# iOS Backup Machine
**Offline, automatic iPhone backup system** running entirely on a **Radxa Zero 3W** (an upgraded Raspberry Pi Zero W).  
When you plug in your iPhone, the system automatically runs an encrypted `idevicebackup2` backup to local storage, shows progress and messages on an e-ink display, and logs all activity locally — **no iCloud, no iTunes, you own your data.**


## Objective
A **self-contained iOS backup appliance** with no reliance on Apple services or computers.  
All backups stay local on the microSD card and can be restored anytime using tools from [libimobiledevice](https://libimobiledevice.org).

### Key features
- **Fully automated**: starts as soon as an iPhone is plugged in.  
- **Live feedback**: e-ink shows progress, status, and errors.  
- **Secure**: backups use the iPhone’s own encryption credentials.  
- **Offline and independent**: no Apple ID, no iTunes, no Internet.

## How it works

### Normal operation
- Plug in your iPhone → the backup starts automatically.  
- The display shows:
  - progress percentage  
  - encryption status  
  - prompts to unlock the phone if needed  
- At the end:
  - displays success confirmation and timestamp  
  - shows owner info (persistent on screen even after power loss)

If the device is unplugged mid-backup, the process stops safely and the screen shows the interruption timestamp.

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


## Software

| Component | Role |
|------------|------|
| **Armbian (Trixie)** | Base OS |
| **Python 3.13** | Runtime for backup and display scripts |
| **libimobiledevice** | iPhone communication (`idevicebackup2`, `idevicepair`) |
| **udev + systemd** | Automation and event handling |


## Directory layout

```
/root/
├── 90-iosbackupmachine.rules        # Starts backup on iPhone plug-in
├── 91-unplug-notify.rules           # Stops backup when unplugged
├── armbianEnv.txt                   # Overlays for SPI/I2C
├── boot-message.py                  # Shows owner info at boot
├── boot-message.service             # Systemd service for above
├── config.yaml                      # Main configuration
├── epdconfig.py                     # Display configuration
├── iosbackupmachine_launcher.sh     # Launch script
├── iosbackupmachine.py              # Main program
├── iosbackupmachine.service         # Systemd service triggered by udev
├── [pisugar]config.json             # UPS configuration template
├── pisugar-power-manager.sh         # UPS installation helper
├── unplug-notify.py                 # Handle unplug events
├── unplug-notify.service            # Service triggered by unplug rule
└── unplug-notify.sh                 # Script to call unplug-notify.py
```



## Configuration

Edit `/root/config.yaml`:

```yaml
backup_dir: /media/iosbackup/
marker_file: .foldermarker
disk_device: /dev/mmcblk1
orientation: landscape_right

owner_lines:
  - "Property of Titius Caius"
  - "+33 123 456 7890 - write@titiuscaius.com"
  - "Reward if found €€€"

error_codes:
  105: "Insufficient free disk space on backup drive"
  208: "Wrong iPhone password. Disconnect and reconnect"
  -4:  "iPhone disconnected during backup"
```

**Notes**
- The `.foldermarker` file confirms the SD card is mounted correctly.  
- Edit `owner_lines` to customize contact info shown on the e-ink display.  
- `disk_device` allows monitoring disk usage after backup.



## Installation

### 1. Flash Armbian on the Radxa Zero 3W
Follow the [Radxa Zero 3W official guide](https://docs.radxa.com/en/zero/zero3/low-level-dev/install-os-on-emmc).  
In short:
```bash
sudo apt install rkdeveloptool
sudo rkdeveloptool db rk356x_spl_loader_ddr1056_v1.12.109_no_check_todly.bin
xz -d Armbian_community_25.11.0-trunk.334_Radxa-zero3_trixie_vendor_6.1.115_minimal.img.xz
sudo rkdeveloptool wl 0 Armbian_community_25.11.0-trunk.334_Radxa-zero3_trixie_vendor_6.1.115_minimal.img
sudo rkdeveloptool rd
```

### 2. Enable I2C and SPI
Edit `/boot/armbianEnv.txt`:
```
overlays=rk3568-spi3-m1-cs0-spidev rk3568-i2c3-m0
overlay_prefix=rk35xx
```
Reboot.

### 3. Install dependencies
```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pil python3-periphery   libimobiledevice6 libimobiledevice-utils usbmuxd
```

### 4. Create Python virtual environment
```bash
python3 -m venv /root/iosbackupmachine
source /root/iosbackupmachine/bin/activate
pip install Pillow pyyaml python-periphery
deactivate
```

### 5. Clone repositories
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
sudo systemctl daemon-reload
sudo systemctl enable iosbackupmachine unplug-notify boot-message
sudo udevadm control --reload-rules
```

### 7. Prepare backup storage
```bash
mkdir -p /media/iosbackup
touch /media/iosbackup/.foldermarker
```

### 8. Configure PiSugar UPS
Install from official script:
```bash
wget https://cdn.pisugar.com/release/pisugar-power-manager.sh
bash pisugar-power-manager.sh -c release
rm /etc/pisugar/config.json
cp /root/ios-backup-machine/[pisugar]config.json /etc/pisugar/config.json
```

## Logs
All logs are stored under `/var/log/iosbackupmachine/`.  
Each run creates a file:  
```
backup-YYYYMMDD-HHMMSS.log
```

## License
MIT License  
Includes code adapted from Waveshare’s [e-Paper library](https://github.com/waveshareteam/e-Paper).
