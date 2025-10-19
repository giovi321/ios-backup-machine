#!/bin/bash
set -euo pipefail

VENV=/root/iosbackupmachine
PY="$VENV/bin/python3"
NOTIFY=/root/shutdown.py

# E-ink env (keep it here so the unit stays simple)
export IOSBACKUP_CONFIG="$CFG"
export EPD_GPIO_CHIP=/dev/gpiochip3
export EPD_PIN_DC=17
export EPD_PIN_RST=1
export EPD_PIN_BUSY=10
export EPD_SPI_DEV=/dev/spidev3.0
export EPD_SPI_HZ=2000000

# Draw the shutdown screen
"$PY" "$NOTIFY"
