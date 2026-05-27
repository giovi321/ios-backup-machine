#!/bin/bash
# long-press-backup.sh — Long press: trigger remote sync
# Safety checks prevent interference with running operations.

LOG=/var/log/iosbackupmachine/autostart.log
mkdir -p "$(dirname "$LOG")"

log() { echo "$(date '+%F %T') [long-press] $1" >> "$LOG"; }

# Check 1: Is install/update running?
if [ -f /tmp/iosbackupmachine-install.lock ]; then
    log "Install/update in progress, ignoring long press."
    exit 0
fi

# Check 2: Is backup running?
if pgrep -f "python.*iosbackupmachine\.py" >/dev/null 2>&1; then
    log "Backup running, ignoring long press."
    exit 0
fi

# Check 3: Is sync already running?
if pgrep -f "python.*backup-sync\.py" >/dev/null 2>&1; then
    log "Sync already running, ignoring long press."
    exit 0
fi

# Check 4: Is setup completed?
if ! /root/iosbackupmachine/bin/python3 -c "
import yaml, sys
with open('/root/iosbackupmachine/config.yaml') as f:
    cfg = yaml.safe_load(f) or {}
sys.exit(0 if cfg.get('setup_completed') else 1)
" 2>/dev/null; then
    log "Setup not completed, ignoring long press."
    exit 0
fi

# Check 5: Is sync enabled?
if ! /root/iosbackupmachine/bin/python3 -c "
import yaml, sys
with open('/root/iosbackupmachine/config.yaml') as f:
    cfg = yaml.safe_load(f) or {}
sys.exit(0 if cfg.get('sync', {}).get('enabled') else 1)
" 2>/dev/null; then
    log "Sync not enabled, ignoring long press."
    exit 0
fi

# All checks passed — start sync
log "Starting sync via long press."
/root/iosbackupmachine/bin/python3 /root/iosbackupmachine/backup-sync.py >> "$LOG" 2>&1 &
