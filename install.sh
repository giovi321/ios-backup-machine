#!/bin/bash
# install.sh - Automated installer for iOS Backup Machine
#
# Run on a fresh Armbian system (Radxa Zero 3W) after flashing.
# Also handles upgrades: stops services, backs up files, migrates config.
# Must be run as root.
#
# Usage:
#   git clone https://github.com/giovi321/ios-backup-machine.git /root/ios-backup-machine
#   bash /root/ios-backup-machine/install.sh
#
# Or for updates:
#   bash /root/ios-backup-machine/update.sh

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
LOCK_FILE="/tmp/iosbackupmachine-install.lock"
VERSION_FILE="${INSTALL_DIR}/.installed_version"
BACKUP_ARCHIVE_DIR="/root/iosbackupmachine-backups"

# Required overlays for SPI and I2C
REQUIRED_OVERLAYS="rk3568-spi3-m1-cs0-spidev rk3568-i2c3-m0"

# Get version from repo
REPO_VERSION=$(grep -oP 'VERSION\s*=\s*"\K[^"]+' "${REPO_DIR}/webui.py" 2>/dev/null || echo "unknown")

# Files to copy
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

# Services
ENABLE_SERVICES=(
    owner-message.service
    webui.service
    ntp-sync.service
    rtc-sync.service
)

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

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

step_n=0
step() {
    step_n=$((step_n + 1))
    echo ""
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BLUE}  Step ${step_n}: $1${NC}"
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
}

info()    { echo -e "${GREEN}  ✓ $1${NC}"; }
warn()    { echo -e "${YELLOW}  ⚠ $1${NC}"; }
error()   { echo -e "${RED}  ✗ $1${NC}"; }
detail()  { echo -e "    $1"; }

fail() {
    error "$1"
    # Remove lock on failure
    rm -f "${LOCK_FILE}"
    exit 1
}

cleanup() {
    rm -f "${LOCK_FILE}"
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------
echo ""
echo -e "${BLUE}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║      iOS Backup Machine - Installer v${REPO_VERSION}                    ║${NC}"
echo -e "${BLUE}║      https://github.com/giovi321/ios-backup-machine        ║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════════════════════════╝${NC}"
echo ""

if [ "$(id -u)" -ne 0 ]; then
    fail "This script must be run as root. Try: sudo bash $0"
fi

if [ ! -f "${REPO_DIR}/iosbackupmachine.py" ]; then
    fail "Cannot find iosbackupmachine.py in ${REPO_DIR}. Run this script from the cloned repo directory."
fi

# --- Lock file: prevent concurrent installs ---
if [ -f "${LOCK_FILE}" ]; then
    LOCK_PID=$(cat "${LOCK_FILE}" 2>/dev/null || echo "")
    if [ -n "${LOCK_PID}" ] && kill -0 "${LOCK_PID}" 2>/dev/null; then
        fail "Another install is running (PID ${LOCK_PID}). Remove ${LOCK_FILE} if this is stale."
    else
        warn "Stale lock file found, removing."
        rm -f "${LOCK_FILE}"
    fi
fi
echo $$ > "${LOCK_FILE}"

# --- Version check ---
IS_UPGRADE=false
INSTALLED_VERSION="none"
if [ -f "${VERSION_FILE}" ]; then
    INSTALLED_VERSION=$(cat "${VERSION_FILE}" 2>/dev/null || echo "unknown")
    IS_UPGRADE=true
fi

info "Running from: ${REPO_DIR}"
info "Install target: ${INSTALL_DIR}"
info "Repo version: ${REPO_VERSION} | Installed: ${INSTALLED_VERSION}"

if [ "${INSTALLED_VERSION}" = "${REPO_VERSION}" ]; then
    warn "Version ${REPO_VERSION} is already installed."
    read -rp "  Re-install anyway? [y/N] " answer
    if [[ ! "${answer}" =~ ^[Yy]$ ]]; then
        echo "  Aborted."
        exit 0
    fi
fi

# ---------------------------------------------------------------------------
# Step: Stop all services before updating
# ---------------------------------------------------------------------------
step "Stop running services"

for svc in "${ALL_SERVICES[@]}"; do
    if systemctl is-active --quiet "${svc}" 2>/dev/null; then
        systemctl stop "${svc}" 2>/dev/null || true
        detail "Stopped ${svc}"
    fi
done

# Verify none are still running
STILL_RUNNING=""
for svc in "${ALL_SERVICES[@]}"; do
    if systemctl is-active --quiet "${svc}" 2>/dev/null; then
        STILL_RUNNING="${STILL_RUNNING} ${svc}"
    fi
done

if [ -n "${STILL_RUNNING}" ]; then
    fail "Services still running:${STILL_RUNNING}. Stop them manually, then re-run."
fi

info "All services stopped"

# ---------------------------------------------------------------------------
# Step: Backup current installation (upgrades only)
# ---------------------------------------------------------------------------
if [ "${IS_UPGRADE}" = true ]; then
    step "Backup current installation"

    BACKUP_TS=$(date '+%Y%m%d-%H%M%S')
    BACKUP_PATH="${BACKUP_ARCHIVE_DIR}/${BACKUP_TS}"
    mkdir -p "${BACKUP_PATH}"

    # Backup app files
    for f in "${APP_FILES[@]}"; do
        src="${INSTALL_DIR}/${f}"
        if [ -f "${src}" ]; then
            cp "${src}" "${BACKUP_PATH}/" 2>/dev/null || true
        fi
    done

    # Backup config
    if [ -f "${INSTALL_DIR}/config.yaml" ]; then
        cp "${INSTALL_DIR}/config.yaml" "${BACKUP_PATH}/config.yaml"
    fi

    # Backup webui dirs
    for d in webui_templates webui_static; do
        if [ -d "${INSTALL_DIR}/${d}" ]; then
            cp -r "${INSTALL_DIR}/${d}" "${BACKUP_PATH}/${d}" 2>/dev/null || true
        fi
    done

    info "Backed up to ${BACKUP_PATH}"

    # Keep only the 5 most recent backups
    if [ -d "${BACKUP_ARCHIVE_DIR}" ]; then
        ls -1dt "${BACKUP_ARCHIVE_DIR}"/*/ 2>/dev/null | tail -n +6 | xargs rm -rf 2>/dev/null || true
    fi
fi

# ---------------------------------------------------------------------------
# Step: Enable I2C and SPI overlays
# ---------------------------------------------------------------------------
step "Enable I2C and SPI overlays"

NEED_REBOOT=false

if [ -f "${ARMBIAN_ENV}" ]; then
    if grep -q "^overlay_prefix=" "${ARMBIAN_ENV}"; then
        info "overlay_prefix already set"
    else
        echo "overlay_prefix=rk35xx" >> "${ARMBIAN_ENV}"
        info "Added overlay_prefix=rk35xx"
        NEED_REBOOT=true
    fi

    if grep -q "^overlays=" "${ARMBIAN_ENV}"; then
        CURRENT_OVERLAYS=$(grep "^overlays=" "${ARMBIAN_ENV}" | cut -d= -f2-)
        MISSING=""
        for ov in ${REQUIRED_OVERLAYS}; do
            if ! echo "${CURRENT_OVERLAYS}" | grep -q "${ov}"; then
                MISSING="${MISSING} ${ov}"
            fi
        done
        if [ -n "${MISSING}" ]; then
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
fi

# ---------------------------------------------------------------------------
# Step: Install system dependencies
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
# Step: Create Python virtual environment
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
# Step: Install Waveshare e-Paper driver
# ---------------------------------------------------------------------------
step "Install Waveshare e-Paper driver"

if [ -d "${EPAPER_DIR}" ]; then
    info "e-Paper repository already exists at ${EPAPER_DIR}"
else
    git clone --quiet "${EPAPER_REPO}" "${EPAPER_DIR}"
    info "Cloned e-Paper repository"
fi

WAVESHARE_LIB="${EPAPER_DIR}/RaspberryPi_JetsonNano/python/lib/waveshare_epd"
if [ -d "${WAVESHARE_LIB}" ]; then
    cp "${REPO_DIR}/epdconfig.py" "${WAVESHARE_LIB}/epdconfig.py"
    info "Copied custom epdconfig.py to waveshare driver"
else
    warn "Waveshare driver lib not found at expected path"
fi

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
# Step: Install application files
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

# --- Config migration ---
if [ -f "${INSTALL_DIR}/config.yaml" ]; then
    info "Migrating config: merging new defaults into existing config.yaml"
    # Use Python to merge new defaults without overwriting existing values
    "${VENV_DIR}/bin/python3" - "${REPO_DIR}/config.yaml.example" "${INSTALL_DIR}/config.yaml" <<'PYEOF'
import sys, yaml

example_path, config_path = sys.argv[1], sys.argv[2]

with open(example_path, "r") as f:
    defaults = yaml.safe_load(f) or {}
with open(config_path, "r") as f:
    current = yaml.safe_load(f) or {}

def merge(base, override):
    """Recursively merge base into override (override wins)."""
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = merge(result[k], v)
        else:
            result[k] = v
    return result

merged = merge(defaults, current)

with open(config_path, "w") as f:
    yaml.dump(merged, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

added = [k for k in defaults if k not in current]
if added:
    print(f"    Added new config keys: {', '.join(added)}")
PYEOF
else
    cp "${REPO_DIR}/config.yaml.example" "${INSTALL_DIR}/config.yaml"
    detail "Copied config.yaml (fresh install)"
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
# Step: Install systemd services and udev rules
# ---------------------------------------------------------------------------
step "Install systemd services and udev rules"

for f in "${REPO_DIR}"/*.rules; do
    [ -f "$f" ] || continue
    cp "$f" /etc/udev/rules.d/
    detail "Installed $(basename "$f")"
done

for f in "${REPO_DIR}"/*.service; do
    [ -f "$f" ] || continue
    cp "$f" /etc/systemd/system/
    detail "Installed $(basename "$f")"
done

systemctl daemon-reload
info "Reloaded systemd daemon"

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

systemctl start usbmuxd.service 2>/dev/null || true
info "Ensured usbmuxd is running"

if [ -f /etc/systemd/system/webui.service ]; then
    systemctl restart webui.service 2>/dev/null && \
        info "Restarted webui.service" || \
        warn "Could not start webui.service (will start on next boot)"
fi

# ---------------------------------------------------------------------------
# Step: Prepare backup storage
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
# Step: Install and configure PiSugar UPS
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
        warn "Failed to download PiSugar installer."
        rm -f "${PISUGAR_TMP}"
    fi
fi

PISUGAR_CONFIG="${REPO_DIR}/[pisugar]config.json"
if [ -f "${PISUGAR_CONFIG}" ] && [ -d /etc/pisugar-server ]; then
    cp "${PISUGAR_CONFIG}" /etc/pisugar-server/config.json
    info "Installed PiSugar configuration"
    systemctl restart pisugar-server 2>/dev/null && \
        info "Restarted pisugar-server" || \
        warn "Could not restart pisugar-server (may not be running yet)"
else
    if [ ! -d /etc/pisugar-server ]; then
        warn "/etc/pisugar-server not found - PiSugar config not copied"
    fi
fi

if command -v nc &>/dev/null; then
    info "Syncing system clock to RTC..."
    echo "rtc_pi2rtc" | nc -q 1 127.0.0.1 8423 2>/dev/null || {
        warn "Could not sync RTC (PiSugar server may not be running yet)"
    }
fi

if [ -f /etc/systemd/system/rtc-sync.service ]; then
    systemctl enable rtc-sync.service 2>/dev/null
    info "Enabled rtc-sync.service"
fi

# ---------------------------------------------------------------------------
# Step: Write version file
# ---------------------------------------------------------------------------
echo "${REPO_VERSION}" > "${VERSION_FILE}"

# ---------------------------------------------------------------------------
# Post-install health check
# ---------------------------------------------------------------------------
step "Post-install health check"

HEALTH_OK=true

# Check critical files exist
for f in iosbackupmachine.py webui.py config.yaml UbuntuMono-Regular.ttf; do
    if [ ! -f "${INSTALL_DIR}/${f}" ]; then
        error "Missing: ${INSTALL_DIR}/${f}"
        HEALTH_OK=false
    fi
done

# Check venv python works
if ! "${VENV_DIR}/bin/python3" -c "import yaml, flask, PIL" 2>/dev/null; then
    error "Python dependencies not importable"
    HEALTH_OK=false
fi

# Check key services are enabled
for svc in "${ENABLE_SERVICES[@]}"; do
    if ! systemctl is-enabled --quiet "${svc}" 2>/dev/null; then
        warn "Service not enabled: ${svc}"
    fi
done

# Check webui is running
if systemctl is-active --quiet webui.service 2>/dev/null; then
    info "webui.service is running"
else
    warn "webui.service is not running"
fi

if [ "${HEALTH_OK}" = true ]; then
    info "All health checks passed"
else
    warn "Some health checks failed - review the output above"
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════════════╗${NC}"
if [ "${IS_UPGRADE}" = true ]; then
echo -e "${GREEN}║           Update to v${REPO_VERSION} complete!                          ║${NC}"
else
echo -e "${GREEN}║                Installation complete!                        ║${NC}"
fi
echo -e "${GREEN}╚══════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  ${GREEN}✓${NC} Version: ${REPO_VERSION}"
echo -e "  ${GREEN}✓${NC} Application installed to ${INSTALL_DIR}"
echo -e "  ${GREEN}✓${NC} Virtual environment at ${VENV_DIR}"
echo -e "  ${GREEN}✓${NC} Backup directory at ${BACKUP_DIR}"
echo -e "  ${GREEN}✓${NC} Services installed and enabled"
if [ "${IS_UPGRADE}" = true ]; then
echo -e "  ${GREEN}✓${NC} Previous version backed up to ${BACKUP_PATH}"
fi
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
if [ "${IS_UPGRADE}" != true ]; then
echo -e "  ${BLUE}Next steps:${NC}"
echo -e "    1. Open the web UI at http://<device-ip>:8080"
echo -e "    2. Complete the first-start setup wizard"
echo -e "    3. Plug in your iPhone and tap Trust"
echo -e "    4. The first backup will start automatically"
fi
echo ""
