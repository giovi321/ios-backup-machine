#!/bin/bash
# Soft power-off (PiSugar button / soft_poweroff_shell).
#
# Stop the display daemon first: its SIGTERM handler paints the owner-info
# screen on the e-ink and sleeps the panel so the image persists after PiSugar
# cuts power. `systemctl stop` blocks until that finishes (TimeoutStopSec).
# Then shut the system down.

systemctl stop iosbackupmachine.service 2>/dev/null || true
shutdown -h now
