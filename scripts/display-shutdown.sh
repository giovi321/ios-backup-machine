#!/bin/bash
# display-shutdown.sh — Show owner info on e-ink before system powers off
# Called by shutdown-display.service ExecStop

export EPD_GPIO_CHIP=/dev/gpiochip3
export EPD_PIN_DC=17
export EPD_PIN_RST=1
export EPD_PIN_BUSY=10
export EPD_SPI_DEV=/dev/spidev3.0
export EPD_SPI_HZ=2000000
export IOSBACKUP_CONFIG=/root/iosbackupmachine/config.yaml

VENV=/root/iosbackupmachine
PY="$VENV/bin/python3"

# Kill any running display scripts to free GPIO
pkill -f "python.*iosbackupmachine.py" 2>/dev/null || true
pkill -f "python.*button-info.py" 2>/dev/null || true
pkill -f "python.*boot-message.py" 2>/dev/null || true
pkill -f "python.*backup-sync.py" 2>/dev/null || true
sleep 1

# Release GPIO handles
"$PY" -c "
from waveshare_epd import epdconfig
try: epdconfig.module_exit()
except: pass
" 2>/dev/null || true

# Show owner info
exec "$PY" /root/iosbackupmachine/owner-message.py
