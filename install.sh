#!/bin/bash
# install.sh - Automated installer for iOS Backup Machine
#
# Run on a fresh Armbian system (Radxa Zero 3W) after flashing.
# Must be run as root.
#
# Usage:
#   git clone https://github.com/giovi321/ios-backup-machine.git /root/ios-backup-machine
#   bash /root/ios-backup-machine/install.sh
#
# Or if already cloned:
#   bash install.sh

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="/root/iosbackupmachine"
VENV_DIR="/root/iosbackupmachine"
BACKUP_DIR="/media/iosbackup"
MARKER_FILE=".foldermarker"
EPAPER_REPO="https://github.com/waveshareteam/e-Paper.git"
EPAPER_DIR="/root/e-Paper"
PISUGAR_SCRIPT_URL="https://cdn.pisugar.com/release/pisugar-power-manager.sh"
ARMBIAN_ENV="/boot/armbianEnv.txt"
LOG_DIR="/var/log/iosbackupmachine"

# Required overlays for SPI and I2C
REQUIRED_OVERLAYS="rk3568-spi3-m1-cs0-spidev rk3568-i2c3-m0"

# Files to copy to /root
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
    UbuntuMono-Regular.ttf
    requirements.txt
)

# Services to install (all .service files)
# Services that should be enabled at boot
ENABLE_SERVICES=(
    owner-message.service
    webui.service
    ntp-sync.service
    rtc-sync.service
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

step=0
step() {
    step=$((step + 1))
    echo ""
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BLUE}  Step ${step}: $1${NC}"
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
}

info()    { echo -e "${GREEN}  ✓ $1${NC}"; }
warn()    { echo -e "${YELLOW}  ⚠ $1${NC}"; }
error()   { echo -e "${RED}  ✗ $1${NC}"; }
detail()  { echo -e "    $1"; }

fail() {
    error "$1"
    exit 1
}

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------
echo ""
echo -e "${BLUE}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║        iOS Backup Machine - Automated Installer v2.1           ║${NC}"
echo -e "${BLUE}║        https://github.com/giovi321/ios-backup-machine        ║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════════════════════════╝${NC}"
echo ""

if [ "$(id -u)" -ne 0 ]; then
    fail "This script must be run as root. Try: sudo bash $0"
fi

if [ ! -f "${REPO_DIR}/iosbackupmachine.py" ]; then
    fail "Cannot find iosbackupmachine.py in ${REPO_DIR}. Run this script from the cloned repo directory."
fi

info "Running from: ${REPO_DIR}"
info "Install target: ${INSTALL_DIR}"

# ---------------------------------------------------------------------------
# Step 0: Stop all iOS Backup Machine services before updating
# ---------------------------------------------------------------------------
step "Stop running services"

ALL_SERVICES=(
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

for svc in "${ALL_SERVICES[@]}"; do
    if systemctl is-active --quiet "${svc}" 2>/dev/null; then
        systemctl stop "${svc}" 2>/dev/null || true
        detail "Stopped ${svc}"
    fi
done

# Verify no services are still running
STILL_RUNNING=""
for svc in "${ALL_SERVICES[@]}"; do
    if systemctl is-active --quiet "${svc}" 2>/dev/null; then
        STILL_RUNNING="${STILL_RUNNING} ${svc}"
    fi
done

if [ -n "${STILL_RUNNING}" ]; then
    error "The following services are still running:${STILL_RUNNING}"
    error "Cannot proceed with update while services are active."
    fail "Stop them manually with: systemctl stop <service>, then re-run the installer."
fi

info "All services stopped"

# ---------------------------------------------------------------------------
# Step 1: Enable I2C and SPI overlays in /boot/armbianEnv.txt
# ---------------------------------------------------------------------------
step "Enable I2C and SPI overlays"

NEED_REBOOT=false

if [ -f "${ARMBIAN_ENV}" ]; then
    # Check if overlay_prefix exists
    if grep -q "^overlay_prefix=" "${ARMBIAN_ENV}"; then
        info "overlay_prefix already set"
    else
        echo "overlay_prefix=rk35xx" >> "${ARMBIAN_ENV}"
        info "Added overlay_prefix=rk35xx"
        NEED_REBOOT=true
    fi

    # Check if overlays line exists and contains required overlays
    if grep -q "^overlays=" "${ARMBIAN_ENV}"; then
        CURRENT_OVERLAYS=$(grep "^overlays=" "${ARMBIAN_ENV}" | cut -d= -f2-)
        MISSING=""
        for ov in ${REQUIRED_OVERLAYS}; do
            if ! echo "${CURRENT_OVERLAYS}" | grep -q "${ov}"; then
                MISSING="${MISSING} ${ov}"
            fi
        done
        if [ -n "${MISSING}" ]; then
            # Append missing overlays
            NEW_OVERLAYS="${CURRENT_OVERLAYS}${MISSING}"
            sed -i "s|^overlays=.*|overlays=${NEW_OVERLAYS}|" "${ARMBIAN_ENV}"
            info "Added missing overlays:${MISSING}"
            NEED_REBOOT=true
        else
            info "All required overlays already present"
        fi
    else
        echo "overlays=${REQUIRED_OVERLAYS}" >> "${ARMBIAN_ENV}"
        info "Added overlays line"
        NEED_REBOOT=true
    fi
else
    warn "${ARMBIAN_ENV} not found - skipping overlay configuration"
    warn "You may need to configure SPI/I2C overlays manually"
fi

# ---------------------------------------------------------------------------
# Step 2: Install system dependencies
# ---------------------------------------------------------------------------
step "Install system dependencies"

info "Updating package lists..."
apt-get update -qq

PACKAGES=(
    python3
    python3-venv
    python3-pil
    python3-periphery
    libimobiledevice-1.0-6
    libimobiledevice-utils
    usbmuxd
    wireguard-tools
    sshpass
    rsync
    netcat-traditional
    git
)

info "Installing packages..."
apt-get install -y -qq "${PACKAGES[@]}"
info "System dependencies installed"

# ---------------------------------------------------------------------------
# Step 3: Create Python virtual environment
# ---------------------------------------------------------------------------
step "Create Python virtual environment"

if [ -d "${VENV_DIR}" ] && [ -f "${VENV_DIR}/bin/python3" ]; then
    info "Virtual environment already exists at ${VENV_DIR}"
else
    python3 -m venv "${VENV_DIR}"
    info "Created virtual environment at ${VENV_DIR}"
fi

info "Installing Python packages..."
"${VENV_DIR}/bin/pip" install --quiet --upgrade pip
"${VENV_DIR}/bin/pip" install --quiet -r "${REPO_DIR}/requirements.txt"
info "Python packages installed"

# ---------------------------------------------------------------------------
# Step 4: Clone e-Paper driver and install
# ---------------------------------------------------------------------------
step "Install Waveshare e-Paper driver"

if [ -d "${EPAPER_DIR}" ]; then
    info "e-Paper repository already exists at ${EPAPER_DIR}"
else
    git clone --quiet "${EPAPER_REPO}" "${EPAPER_DIR}"
    info "Cloned e-Paper repository"
fi

# Copy custom epdconfig.py
WAVESHARE_LIB="${EPAPER_DIR}/RaspberryPi_JetsonNano/python/lib/waveshare_epd"
if [ -d "${WAVESHARE_LIB}" ]; then
    cp "${REPO_DIR}/epdconfig.py" "${WAVESHARE_LIB}/epdconfig.py"
    info "Copied custom epdconfig.py to waveshare driver"
else
    warn "Waveshare driver lib not found at expected path"
fi

# Symlink waveshare_epd into venv site-packages
PYTHON_VERSION=$("${VENV_DIR}/bin/python3" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
SITE_PACKAGES="${VENV_DIR}/lib/python${PYTHON_VERSION}/site-packages"

if [ -L "${SITE_PACKAGES}/waveshare_epd" ]; then
    info "waveshare_epd symlink already exists"
elif [ -d "${SITE_PACKAGES}/waveshare_epd" ]; then
    info "waveshare_epd directory already exists in site-packages"
else
    ln -s "${WAVESHARE_LIB}" "${SITE_PACKAGES}/waveshare_epd"
    info "Linked waveshare_epd into venv (Python ${PYTHON_VERSION})"
fi

# ---------------------------------------------------------------------------
# Step 5: Copy application files to /root
# ---------------------------------------------------------------------------
step "Install application files"

for f in "${APP_FILES[@]}"; do
    src="${REPO_DIR}/${f}"
    dst="${INSTALL_DIR}/${f}"
    if [ -f "${src}" ]; then
        cp "${src}" "${dst}"
        detail "Copied ${f}"
    else
        warn "File not found: ${src}"
    fi
done

# Copy config.yaml only if it doesn't already exist (preserve user settings on upgrade)
if [ ! -f "${INSTALL_DIR}/config.yaml" ]; then
    cp "${REPO_DIR}/config.yaml.example" "${INSTALL_DIR}/config.yaml"
    detail "Copied config.yaml (fresh install)"
else
    info "config.yaml already exists, preserving user settings"
fi

# Copy webui directories
if [ -d "${REPO_DIR}/webui_templates" ]; then
    cp -r "${REPO_DIR}/webui_templates" "${INSTALL_DIR}/webui_templates"
    info "Copied webui_templates/"
fi
if [ -d "${REPO_DIR}/webui_static" ]; then
    cp -r "${REPO_DIR}/webui_static" "${INSTALL_DIR}/webui_static"
    info "Copied webui_static/"
fi

# Make shell scripts executable
chmod +x "${INSTALL_DIR}/iosbackupmachine_launcher.sh" 2>/dev/null || true
chmod +x "${INSTALL_DIR}/unplug-notify.sh" 2>/dev/null || true
chmod +x "${INSTALL_DIR}/shutdown.sh" 2>/dev/null || true

info "Application files installed to ${INSTALL_DIR}"

# ---------------------------------------------------------------------------
# Step 6: Install systemd services and udev rules
# ---------------------------------------------------------------------------
step "Install systemd services and udev rules"

# Copy udev rules
for f in "${REPO_DIR}"/*.rules; do
    [ -f "$f" ] || continue
    cp "$f" /etc/udev/rules.d/
    detail "Installed $(basename "$f")"
done

# Copy systemd service files
for f in "${REPO_DIR}"/*.service; do
    [ -f "$f" ] || continue
    cp "$f" /etc/systemd/system/
    detail "Installed $(basename "$f")"
done

systemctl daemon-reload
info "Reloaded systemd daemon"

# Enable services
for svc in "${ENABLE_SERVICES[@]}"; do
    if [ -f "/etc/systemd/system/${svc}" ]; then
        systemctl enable "${svc}" 2>/dev/null
        detail "Enabled ${svc}"
    else
        warn "Service file not found: ${svc}"
    fi
done

udevadm control --reload-rules
udevadm trigger
info "Reloaded udev rules"

# Ensure usbmuxd is running so iPhone detection works immediately
systemctl start usbmuxd.service 2>/dev/null || true
info "Ensured usbmuxd is running"

# (Re)start the web UI service so new routes are available immediately
if [ -f /etc/systemd/system/webui.service ]; then
    systemctl restart webui.service 2>/dev/null && \
        info "Restarted webui.service" || \
        warn "Could not start webui.service (will start on next boot)"
fi

# ---------------------------------------------------------------------------
# Step 7: Prepare backup storage
# ---------------------------------------------------------------------------
step "Prepare backup storage"

mkdir -p "${BACKUP_DIR}"
if [ ! -f "${BACKUP_DIR}/${MARKER_FILE}" ]; then
    touch "${BACKUP_DIR}/${MARKER_FILE}"
    info "Created ${BACKUP_DIR}/${MARKER_FILE}"
else
    info "Marker file already exists"
fi

mkdir -p "${LOG_DIR}"
info "Backup directory ready: ${BACKUP_DIR}"

# ---------------------------------------------------------------------------
# Step 8: Install and configure PiSugar UPS
# ---------------------------------------------------------------------------
step "Install and configure PiSugar UPS"

if command -v pisugar-server &>/dev/null || [ -f /etc/pisugar-server/config.json ]; then
    info "PiSugar server appears to already be installed"
else
    info "Downloading PiSugar installer..."
    PISUGAR_TMP=$(mktemp)
    if wget -q -O "${PISUGAR_TMP}" "${PISUGAR_SCRIPT_URL}"; then
        info "Running PiSugar installer (this may take a moment)..."
        bash "${PISUGAR_TMP}" -c release || {
            warn "PiSugar installer returned non-zero. Check output above."
        }
        rm -f "${PISUGAR_TMP}"
        info "PiSugar installer finished"
    else
        warn "Failed to download PiSugar installer. You can install it manually later."
        warn "URL: ${PISUGAR_SCRIPT_URL}"
        rm -f "${PISUGAR_TMP}"
    fi
fi

# Copy PiSugar config
PISUGAR_CONFIG="${REPO_DIR}/[pisugar]config.json"
if [ -f "${PISUGAR_CONFIG}" ] && [ -d /etc/pisugar-server ]; then
    cp "${PISUGAR_CONFIG}" /etc/pisugar-server/config.json
    info "Installed PiSugar configuration"
    # Restart PiSugar server to pick up new button config
    systemctl restart pisugar-server 2>/dev/null && \
        info "Restarted pisugar-server" || \
        warn "Could not restart pisugar-server (may not be running yet)"
else
    if [ ! -d /etc/pisugar-server ]; then
        warn "/etc/pisugar-server not found - PiSugar config not copied"
    fi
fi

# Sync RTC
if command -v nc &>/dev/null; then
    info "Syncing system clock to RTC..."
    echo "rtc_pi2rtc" | nc -q 1 127.0.0.1 8423 2>/dev/null || {
        warn "Could not sync RTC (PiSugar server may not be running yet)"
    }
fi

# Enable rtc-sync service
if [ -f /etc/systemd/system/rtc-sync.service ]; then
    systemctl enable rtc-sync.service 2>/dev/null
    info "Enabled rtc-sync.service"
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║                    Installation complete!                    ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  ${GREEN}✓${NC} Application installed to ${INSTALL_DIR}"
echo -e "  ${GREEN}✓${NC} Virtual environment at ${VENV_DIR}"
echo -e "  ${GREEN}✓${NC} Backup directory at ${BACKUP_DIR}"
echo -e "  ${GREEN}✓${NC} Services installed and enabled"
echo ""

if [ "${NEED_REBOOT}" = true ]; then
    echo -e "  ${YELLOW}⚠ SPI/I2C overlays were changed in ${ARMBIAN_ENV}.${NC}"
    echo -e "  ${YELLOW}  A reboot is required for the e-ink display to work.${NC}"
    echo ""
    read -rp "  Reboot now? [y/N] " answer
    if [[ "${answer}" =~ ^[Yy]$ ]]; then
        echo "  Rebooting..."
        reboot
    else
        echo -e "  ${YELLOW}Remember to reboot before using the e-ink display.${NC}"
    fi
else
    echo -e "  No reboot required."
fi

echo ""
echo -e "  ${BLUE}Next steps:${NC}"
echo -e "    1. Open the web UI at http://<device-ip>:8080"
echo -e "    2. Complete the first-start setup wizard"
echo -e "    3. Plug in your iPhone and tap Trust"
echo -e "    4. The first backup will start automatically"
echo ""
