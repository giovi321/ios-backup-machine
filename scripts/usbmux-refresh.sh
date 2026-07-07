#!/bin/bash
# usbmux-refresh.sh — force usbmuxd to notice a hot-plugged iPhone.
#
# On this ARM image usbmuxd runs persistently but does not receive libusb hotplug
# events, so an iPhone plugged AFTER boot is invisible to idevice_* (idevice_id
# returns nothing) until usbmuxd re-scans. The kernel still enumerates it (lsusb
# shows 05ac:*). Restarting usbmuxd forces the re-scan. Triggered by the udev
# `add` rule for Apple devices; must never restart usbmuxd mid-backup, since
# idevicebackup2 holds a usbmux session that a restart would kill.
LOG=/var/lib/iosbackupmachine/autostart.log
mkdir -p "$(dirname "$LOG")" 2>/dev/null
log() { echo "$(date '+%F %T') [usbmux-refresh] $1" >>"$LOG"; }

# Never disrupt an active backup.
if pgrep -f idevicebackup2 >/dev/null 2>&1; then
    log "backup running — skip"
    exit 0
fi

# Give usbmuxd a moment in case hotplug did work this time.
sleep 1

# Already visible? Nothing to do (also covers boot-with-plugged, which works).
if idevice_id -l 2>/dev/null | grep -q .; then
    exit 0
fi

# Kernel sees an Apple device but usbmux doesn't -> the hotplug gap. Re-scan.
if lsusb 2>/dev/null | grep -qiE "05ac:|apple"; then
    log "Apple device present but invisible to usbmux — restarting usbmuxd"
    systemctl restart usbmuxd
fi
