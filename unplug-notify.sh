#!/bin/bash
set -euo pipefail

LOG=/var/log/iosbackup/autostart.log
VENV=/root/iosbackupmachine
PY="$VENV/bin/python3"
NOTIFY=/root/unplug-notify.py
CFG=/root/config.yaml

# E-ink env (keep it here so the unit stays simple)
export IOSBACKUP_CONFIG="$CFG"
export EPD_GPIO_CHIP=/dev/gpiochip3
export EPD_PIN_DC=17
export EPD_PIN_RST=1
export EPD_PIN_BUSY=10
export EPD_SPI_DEV=/dev/spidev3.0
export EPD_SPI_HZ=2000000

mkdir -p "$(dirname "$LOG")"

echo "$(date '+%F %T') [unplug-notify] invoked" >>"$LOG"

# Run only if the backup script is active
if ! pgrep -f "iosbackupmachine.py" >/dev/null; then
  echo "$(date '+%F %T') [unplug-notify] backup not running, nothing to do" >>"$LOG"
  exit 0
fi

# Stop the backup cleanly
pids=$(pgrep -f "iosbackupmachine.py" || true)
if [[ -n "${pids:-}" ]]; then
  echo "$(date '+%F %T') [unplug-notify] killing PIDs: $pids" >>"$LOG"
  kill -TERM $pids || true
  sleep 0.5
  kill -KILL $pids 2>/dev/null || true
fi

# Release any stuck GPIO handles
"$PY" - <<'PY' || true
from waveshare_epd import epdconfig
try: epdconfig.module_exit()
except Exception: pass
PY

# Draw the unplug screen
if ! "$PY" "$NOTIFY"; then
  echo "$(date '+%F %T') [unplug-notify] notifier failed" >>"$LOG"
  exit 1
fi

echo "$(date '+%F %T') [unplug-notify] done" >>"$LOG"
exit 0
