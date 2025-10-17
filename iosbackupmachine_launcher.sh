#!/bin/bash
LOGFILE=/var/log/iosbackup/autostart.log
VENV=/root/iosbackupmachine
SCRIPT=/root/iosbackupmachine.py

# Variables
export EPD_GPIO_CHIP=/dev/gpiochip3
export EPD_PIN_DC=17
export EPD_PIN_RST=1
export EPD_PIN_BUSY=10
export EPD_SPI_DEV=/dev/spidev3.0
export EPD_SPI_HZ=2000000

# Wait a few seconds to let the device settle
sleep 5

echo "$(date '+%Y-%m-%d %H:%M:%S') - iPhone detected. Starting backup." >> "$LOGFILE"

# Start the backup in background to avoid blocking udev
/usr/bin/nohup "$VENV/bin/python3" "$SCRIPT" 2>&1 &
