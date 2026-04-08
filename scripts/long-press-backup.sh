#!/bin/bash
# long-press-backup.sh — Start backup on long press of UPS button
# Safety checks prevent interference with running operations.

LOG=/var/log/iosbackupmachine/autostart.log
mkdir -p "$(dirname "$LOG")"

log() { echo "$(date '+%F %T') [long-press] $1" >> "$LOG"; }

# Check 1: Is install/update running?
if [ -f /tmp/iosbackupmachine-install.lock ]; then
    log "Install/update in progress, ignoring long press."
    exit 0
fi

# Check 2: Is backup already running?
if pgrep -f "python.*iosbackupmachine\.py" >/dev/null 2>&1; then
    log "Backup already running, ignoring long press."
    exit 0
fi

# Check 3: Is idevicebackup2 already running?
if pgrep -f "idevicebackup2" >/dev/null 2>&1; then
    log "idevicebackup2 already running, ignoring long press."
    exit 0
fi

# Check 4: Is an iPhone connected?
if ! /root/iosbackupmachine/bin/python3 -c "
import subprocess, sys
r = subprocess.run(['idevice_id', '-l'], capture_output=True, text=True, timeout=5)
sys.exit(0 if r.stdout.strip() else 1)
" 2>/dev/null; then
    log "No iPhone connected, ignoring long press."
    exit 0
fi

# Check 5: Is setup completed?
if ! /root/iosbackupmachine/bin/python3 -c "
import yaml, sys
with open('/root/iosbackupmachine/config.yaml') as f:
    cfg = yaml.safe_load(f) or {}
sys.exit(0 if cfg.get('setup_completed') else 1)
" 2>/dev/null; then
    log "Setup not completed, ignoring long press."
    exit 0
fi

# All checks passed — start the backup
log "Starting backup via long press."
systemctl reset-failed iosbackupmachine.service 2>/dev/null
systemctl restart iosbackupmachine.service
