#!/bin/bash
# Show owner info on e-ink, then trigger system shutdown.
# The display is updated here (not just in the systemd service)
# to ensure it happens before PiSugar cuts power.

export EPD_GPIO_CHIP=/dev/gpiochip3
export EPD_PIN_DC=17
export EPD_PIN_RST=1
export EPD_PIN_BUSY=10
export EPD_SPI_DEV=/dev/spidev3.0
export EPD_SPI_HZ=2000000
export IOSBACKUP_CONFIG=/root/iosbackupmachine/config.yaml

# Kill display processes and show owner info
pkill -f "python.*iosbackupmachine.py" 2>/dev/null || true
pkill -f "python.*button-info.py" 2>/dev/null || true
pkill -f "python.*boot-message.py" 2>/dev/null || true
sleep 1
/root/iosbackupmachine/bin/python3 /root/iosbackupmachine/owner-message.py 2>/dev/null || true

# Now shut down
shutdown -h now
