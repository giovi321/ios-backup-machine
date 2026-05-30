#!/bin/bash
# unplug-notify.sh — iPhone removed.
#
# The display service is the single owner of the e-ink and renders the
# interrupted / idle screen itself (it detects the disconnect via device
# presence). This script only stops a running idevicebackup2 quickly so the
# daemon notices the unplug without waiting for the USB timeout. It must NOT
# touch the EPD or the display daemon.

LOG=/var/log/iosbackupmachine/autostart.log
mkdir -p "$(dirname "$LOG")"
echo "$(date '+%F %T') [unplug-notify] invoked" >>"$LOG"

pids=$(pgrep -f "idevicebackup2" || true)
if [ -n "${pids:-}" ]; then
  echo "$(date '+%F %T') [unplug-notify] stopping idevicebackup2: $pids" >>"$LOG"
  kill -TERM $pids 2>/dev/null || true
  sleep 0.5
  kill -KILL $pids 2>/dev/null || true
fi

exit 0
