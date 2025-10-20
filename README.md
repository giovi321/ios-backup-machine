[![License](https://img.shields.io/github/license/giovi321/ios-backup-machine)](LICENSE)
[![Python 3.13](https://img.shields.io/badge/Python-3.13-blue?logo=python&logoColor=white)](https://www.python.org/downloads/release/python-3130/)
[![Armbian](https://img.shields.io/badge/OS-Armbian-orange?logo=armbian)](https://www.armbian.com/radxa-zero-3/)
![Offline](https://img.shields.io/badge/network-offline--only-critical.svg)

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
- **Secure**: backups use the iPhone’s own encryption credentials.  
- **Offline and independent**: no Apple ID, no iTunes, no Internet.
- **Solid**: file corruption is prevented by a small UPS.

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
- If you push the additional button of the PiSugar UPS, it shows for 10 seconds on the e-ink
- [ ] More to be implemented

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


## Directory layout

```
/root/
├── Case ios backup machine_v8.stl   # 3D printable case
├── 90-iosbackupmachine.rules        # Starts backup on iPhone plug-in
                                     # and stops it when unplugged
├── armbianEnv.txt                   # Overlays for SPI/I2C
├── owner-message.py                 # Shows owner info on the e-ink screen
├── owner-message.service            # Systemd service for above
├── config.yaml                      # Main configuration
├── epdconfig.py                     # Display configuration
├── iosbackupmachine_launcher.sh     # Launch script
├── iosbackupmachine.py              # Main program
├── iosbackupmachine.service         # Systemd service triggered by udev
├── last-backup.py                   # Shows last backup info and memory available
├── last-backup.service              # Systemd service for above
├── [pisugar]config.json             # UPS configuration template
├── shutdown.sh                      # Shows owner info on screen and turns off device
├── unplug-notify.py                 # Handle unplug events
├── unplug-notify.service            # Service triggered by unplug rule
└── unplug-notify.sh                 # Script to call unplug-notify.py
```

## Configuration

Edit `/root/config.yaml`:

```yaml
backup_dir: /media/iosbackup/ # where the backup is saved
marker_file: .foldermarker    # file that tells the script that the microSD card was mounted correctly. if this file is missing, then the microSD card is not mounted and the backup will not run.
disk_device: /dev/mmcblk1     # name of the microSD card node. it is needed to measure the space utilization at the end of the backup
orientation: landscape_right  # you can orient the screen the other way (landscape_left)

owner_lines:                  # You can write whatever you wans as long as it fits in the screen (there are no automatic line breaks)
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
apt install rkdeveloptool
rkdeveloptool db rk356x_spl_loader_ddr1056_v1.12.109_no_check_todly.bin
xz -d Armbian_community_25.11.0-trunk.334_Radxa-zero3_trixie_vendor_6.1.115_minimal.img.xz
rkdeveloptool wl 0 Armbian_community_25.11.0-trunk.334_Radxa-zero3_trixie_vendor_6.1.115_minimal.img
rkdeveloptool rd
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
apt update
apt install -y python3 python3-venv python3-pil python3-periphery libimobiledevice-1.0-6 libimobiledevice-utils usbmuxd
```

### 4. Create Python virtual environment
```bash
python3 -m venv /root/iosbackupmachine
source /root/iosbackupmachine/bin/activate
pip install Pillow pyyaml python-periphery
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
## First run
The first run must be done using idevicebackup2 "stand alone" because you need to set the backup encryption passphrase.
Disable the udev rule `90-iosbackupmachine.rules` to prevent the program from starting
```
mv /etc/udev/rules.d/90-iosbackupmachine.rules /etc/udev/rules.d/90-iosbackupmachine.rules.disabled
udevadm control --reload-rules
```
plug your iOS device and run
```
idevicebackup2 encryption on /media/iosbackup
idevicebackup2 backup --full /media/iosbackup
```
It is going to take quite a lot of time, depending on the memory and memory usage of your iOS device.
Now you can re-enable the udev rule to autostart the backup when you plug the iPhone:
```
mv /etc/udev/rules.d/90-iosbackupmachine.rules.disabled /etc/udev/rules.d/90-iosbackupmachine.rules
udevadm control --reload-rules
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

## New features to be implemented
- [ ] Use iPhone hotspot over USB to give the iOS backup machine internet connectivity
- [ ] Add MQTT reporting of backup
- [ ] Add partial refresh of the display (not sure if it is supported)
- [ ] Filter iOS devices connected (i.e., start the backup only when a certain iPhone is connected)
- [ ] Add web interface?

## License
MIT License  
Includes code adapted from Waveshare’s [e-Paper library](https://github.com/waveshareteam/e-Paper).
