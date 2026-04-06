#!/bin/bash
# uninstall.sh - Uninstaller for iOS Backup Machine
#
# Stops and removes all services, udev rules, and application files
# installed by install.sh. Optionally removes backup data and logs.
#
# Must be run as root.
#
# Usage:
#   bash /root/ios-backup-machine/uninstall.sh

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration (must match install.sh)
# ---------------------------------------------------------------------------
INSTALL_DIR="/root/iosbackupmachine"
VENV_DIR="/root/iosbackupmachine"
BACKUP_DIR="/media/iosbackup"
EPAPER_DIR="/root/e-Paper"
LOG_DIR="/var/log/iosbackupmachine"

APP_FILES=(
    iosbackupmachine.py
    iosbackupmachine_launcher.sh
    last-backup.py
    owner-message.py
    boot-message.py
    button-info.py
    backup-sync.py
    unplug-notify.py
    unplug-notify.sh
    shutdown.sh
    ntp-sync.py
    webui.py
    netutil.py
    notifications.py
    wg_crypto.py
    wg_manager.py
    sync_crypto.py
    sync_manager.py
    epdconfig.py
    config.yaml
    UbuntuMono-Regular.ttf
    requirements.txt
    wireguard.enc
    wireguard-key-backup.txt
    sync.enc
    sync-key-backup.txt
)

APP_DIRS=(
    webui_templates
    webui_static
)

SERVICES=(
    iosbackupmachine.service
    webui.service
    owner-message.service
    ntp-sync.service
    rtc-sync.service
    last-backup.service
    unplug-notify.service
    button-info.service
    backup-sync.service
)

UDEV_RULES=(
    90-iosbackupmachine.rules
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info()    { echo -e "${GREEN}  * $1${NC}"; }
warn()    { echo -e "${YELLOW}  ! $1${NC}"; }
error()   { echo -e "${RED}  x $1${NC}"; }
detail()  { echo -e "    $1"; }

# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------
echo ""
echo -e "${RED}+--------------------------------------------------------------+${NC}"
echo -e "${RED}|       iOS Backup Machine -- Uninstaller                      |${NC}"
echo -e "${RED}|       https://github.com/giovi321/ios-backup-machine         |${NC}"
echo -e "${RED}+--------------------------------------------------------------+${NC}"
echo ""

if [ "$(id -u)" -ne 0 ]; then
    error "This script must be run as root. Try: sudo bash $0"
    exit 1
fi

echo -e "  ${YELLOW}This will remove iOS Backup Machine from this system.${NC}"
echo ""
read -rp "  Continue? [y/N] " answer
if [[ ! "${answer}" =~ ^[Yy]$ ]]; then
    echo "  Aborted."
    exit 0
fi

# ---------------------------------------------------------------------------
# Step 1: Stop and disable services
# ---------------------------------------------------------------------------
echo ""
echo -e "${BLUE}  [1/5] Stopping and disabling services...${NC}"

for svc in "${SERVICES[@]}"; do
    if systemctl is-active --quiet "${svc}" 2>/dev/null; then
        systemctl stop "${svc}" 2>/dev/null || true
        detail "Stopped ${svc}"
    fi
    if systemctl is-enabled --quiet "${svc}" 2>/dev/null; then
        systemctl disable "${svc}" 2>/dev/null || true
        detail "Disabled ${svc}"
    fi
    if [ -f "/etc/systemd/system/${svc}" ]; then
        rm -f "/etc/systemd/system/${svc}"
        detail "Removed /etc/systemd/system/${svc}"
    fi
done

systemctl daemon-reload 2>/dev/null || true
info "Services removed"

# ---------------------------------------------------------------------------
# Step 2: Remove udev rules
# ---------------------------------------------------------------------------
echo ""
echo -e "${BLUE}  [2/5] Removing udev rules...${NC}"

for rule in "${UDEV_RULES[@]}"; do
    if [ -f "/etc/udev/rules.d/${rule}" ]; then
        rm -f "/etc/udev/rules.d/${rule}"
        detail "Removed /etc/udev/rules.d/${rule}"
    fi
done

udevadm control --reload-rules 2>/dev/null || true
udevadm trigger 2>/dev/null || true
info "Udev rules removed"

# ---------------------------------------------------------------------------
# Step 3: Remove application files and install directory
# ---------------------------------------------------------------------------
echo ""
echo -e "${BLUE}  [3/5] Removing application files...${NC}"

# Remove individual files (handles case where INSTALL_DIR == VENV_DIR)
for f in "${APP_FILES[@]}"; do
    target="${INSTALL_DIR}/${f}"
    if [ -f "${target}" ]; then
        rm -f "${target}"
        detail "Removed ${target}"
    fi
done

for d in "${APP_DIRS[@]}"; do
    target="${INSTALL_DIR}/${d}"
    if [ -d "${target}" ]; then
        rm -rf "${target}"
        detail "Removed ${target}/"
    fi
done

# Also clean up any files from old /root install location
OLD_FILES=(
    /root/iosbackupmachine.py /root/iosbackupmachine_launcher.sh
    /root/last-backup.py /root/owner-message.py /root/boot-message.py
    /root/button-info.py /root/backup-sync.py /root/unplug-notify.py
    /root/unplug-notify.sh /root/shutdown.sh /root/ntp-sync.py
    /root/webui.py /root/netutil.py /root/notifications.py
    /root/wg_crypto.py /root/wg_manager.py /root/sync_crypto.py
    /root/sync_manager.py /root/epdconfig.py /root/UbuntuMono-Regular.ttf
    /root/requirements.txt /root/config.yaml
    /root/wireguard.enc /root/wireguard-key-backup.txt
    /root/sync.enc /root/sync-key-backup.txt
)
for f in "${OLD_FILES[@]}"; do
    if [ -f "$f" ]; then
        rm -f "$f"
        detail "Removed old-location file: $f"
    fi
done
for d in /root/webui_templates /root/webui_static; do
    if [ -d "$d" ]; then
        rm -rf "$d"
        detail "Removed old-location dir: $d/"
    fi
done

info "Application files removed"

# ---------------------------------------------------------------------------
# Step 4: Remove virtual environment and e-Paper driver
# ---------------------------------------------------------------------------
echo ""
echo -e "${BLUE}  [4/5] Removing virtual environment and drivers...${NC}"

if [ -d "${VENV_DIR}" ]; then
    rm -rf "${VENV_DIR}"
    info "Removed virtual environment: ${VENV_DIR}"
else
    detail "Virtual environment not found (already removed?)"
fi

if [ -d "${EPAPER_DIR}" ]; then
    rm -rf "${EPAPER_DIR}"
    info "Removed e-Paper driver: ${EPAPER_DIR}"
else
    detail "e-Paper driver not found (already removed?)"
fi

# ---------------------------------------------------------------------------
# Step 5: Optionally remove logs and backup data
# ---------------------------------------------------------------------------
echo ""
echo -e "${BLUE}  [5/5] Optional cleanup...${NC}"

if [ -d "${LOG_DIR}" ]; then
    read -rp "  Remove log files in ${LOG_DIR}? [y/N] " rm_logs
    if [[ "${rm_logs}" =~ ^[Yy]$ ]]; then
        rm -rf "${LOG_DIR}"
        info "Removed log directory"
    else
        warn "Log directory kept: ${LOG_DIR}"
    fi
fi

if [ -d "${BACKUP_DIR}" ]; then
    echo ""
    echo -e "  ${YELLOW}WARNING: The backup directory contains your iPhone backups!${NC}"
    read -rp "  Remove backup data in ${BACKUP_DIR}? [y/N] " rm_backups
    if [[ "${rm_backups}" =~ ^[Yy]$ ]]; then
        read -rp "  Are you SURE? This cannot be undone. Type 'yes' to confirm: " confirm
        if [ "${confirm}" = "yes" ]; then
            rm -rf "${BACKUP_DIR}"
            info "Removed backup directory"
        else
            warn "Backup directory kept: ${BACKUP_DIR}"
        fi
    else
        warn "Backup directory kept: ${BACKUP_DIR}"
    fi
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo -e "${GREEN}+--------------------------------------------------------------+${NC}"
echo -e "${GREEN}|              Uninstallation complete!                         |${NC}"
echo -e "${GREEN}+--------------------------------------------------------------+${NC}"
echo ""
echo -e "  ${GREEN}*${NC} All iOS Backup Machine services and files have been removed."
echo -e "  ${GREEN}*${NC} System packages (python3, libimobiledevice, etc.) were NOT removed."
echo -e "  ${GREEN}*${NC} PiSugar software was NOT removed (uninstall it separately if needed)."
echo ""
