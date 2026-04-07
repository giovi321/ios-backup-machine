#!/bin/bash
set -euo pipefail

LOG=/var/log/iosbackupmachine/autostart.log
VENV=/root/iosbackupmachine
PY="$VENV/bin/python3"
NOTIFY=/root/iosbackupmachine/unplug-notify.py
CFG=/root/iosbackupmachine/config.yaml

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

STATUS_FILE="/var/log/iosbackupmachine/backup_status.json"
BOOT_SCRIPT="/root/iosbackupmachine/boot-message.py"

# Check if backup was actually in progress
BACKUP_WAS_ACTIVE=false
if [ -f "$STATUS_FILE" ]; then
  STATE=$("$PY" -c "import json; print(json.load(open('$STATUS_FILE')).get('state',''))" 2>/dev/null || echo "")
  if [ "$STATE" = "backing_up" ] || [ "$STATE" = "connected" ]; then
    BACKUP_WAS_ACTIVE=true
  fi
fi

# Also check if processes are running
pids=$(pgrep -f "python.*iosbackupmachine\.py" || true)
if [[ -n "${pids:-}" ]]; then
  BACKUP_WAS_ACTIVE=true
  echo "$(date '+%F %T') [unplug-notify] killing PIDs: $pids" >>"$LOG"
  kill -TERM $pids || true
  sleep 0.5
  kill -KILL $pids 2>/dev/null || true
fi

pids2=$(pgrep -f "idevicebackup2" || true)
if [[ -n "${pids2:-}" ]]; then
  BACKUP_WAS_ACTIVE=true
  echo "$(date '+%F %T') [unplug-notify] killing PIDs: $pids2" >>"$LOG"
  kill -TERM $pids2 || true
  sleep 0.5
  kill -KILL $pids2 2>/dev/null || true
fi

# Release any stuck GPIO handles
"$PY" - <<'PY' || true
from waveshare_epd import epdconfig
try: epdconfig.module_exit()
except Exception: pass
PY

if [ "$BACKUP_WAS_ACTIVE" = true ]; then
  # Update status file to interrupted
  "$PY" -c "import json; json.dump({'state':'interrupted','reason':'iPhone unplugged','timestamp':'$(date -Iseconds)'}, open('$STATUS_FILE','w'))" 2>/dev/null || true
  # Show interrupted screen
  if ! "$PY" "$NOTIFY"; then
    echo "$(date '+%F %T') [unplug-notify] notifier failed" >>"$LOG"
  fi
else
  echo "$(date '+%F %T') [unplug-notify] backup was not active, showing boot screen" >>"$LOG"
  # Show normal boot screen instead
  "$PY" "$BOOT_SCRIPT" 2>/dev/null || true
fi

echo "$(date '+%F %T') [unplug-notify] done" >>"$LOG"
exit 0
