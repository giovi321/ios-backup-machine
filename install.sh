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
# Persistent logs (rootfs, survive power loss). Runtime IPC stays on the volatile
# zram-backed /var/log/iosbackupmachine (RUNTIME_DIR), created by the services.
LOG_DIR="/var/lib/iosbackupmachine"
RUNTIME_DIR="/var/log/iosbackupmachine"
LOCK_FILE="/tmp/iosbackupmachine-install.lock"
VERSION_FILE="${INSTALL_DIR}/.installed_version"
BACKUP_ARCHIVE_DIR="/root/iosbackupmachine-backups"

# Required overlays for SPI and I2C
REQUIRED_OVERLAYS="rk3568-spi3-m1-cs0-spidev rk3568-i2c3-m0"

# Get version from repo
REPO_VERSION=$(sed -n 's/^VERSION *= *"\([^"]*\)".*/\1/p' "${REPO_DIR}/app/webui.py" 2>/dev/null || echo "unknown")

# Files to copy (repo_path:install_name)
APP_FILES=(
    "app/iosbackupmachine.py:iosbackupmachine.py"
    "app/webui.py:webui.py"
    "app/backup-sync.py:backup-sync.py"
    "app/ntp-sync.py:ntp-sync.py"
    "app/notifications.py:notifications.py"
    "app/netutil.py:netutil.py"
    "app/epdconfig.py:epdconfig.py"
    "app/wg_crypto.py:wg_crypto.py"
    "app/wg_manager.py:wg_manager.py"
    "app/wifi_manager.py:wifi_manager.py"
    "app/sync_crypto.py:sync_crypto.py"
    "app/sync_manager.py:sync_manager.py"
    "app/notify_crypto.py:notify_crypto.py"
    "app/config_schema.py:config_schema.py"
    "app/power.py:power.py"
    "app/logutil.py:logutil.py"
    "scripts/unplug-notify.sh:unplug-notify.sh"
    "scripts/shutdown.sh:shutdown.sh"
    "scripts/long-press-backup.sh:long-press-backup.sh"
    "scripts/wg-autoconnect.sh:wg-autoconnect.sh"
    "scripts/usbmux-refresh.sh:usbmux-refresh.sh"
    "assets/UbuntuMono-Regular.ttf:UbuntuMono-Regular.ttf"
    "requirements.txt:requirements.txt"
)

# Services. iosbackupmachine.service is now the always-on single EPD owner, so it
# is enabled at boot (previously it was only udev-triggered per iPhone plug).
ENABLE_SERVICES=(
    iosbackupmachine.service
    webui.service
    ntp-sync.service
    rtc-sync.service
    wg-autoconnect.service
)

ALL_SERVICES=(
    iosbackupmachine.service
    webui.service
    owner-message.service
    shutdown-display.service
    ntp-sync.service
    rtc-sync.service
    last-backup.service
    unplug-notify.service
    button-info.service
    backup-sync.service
    wg-autoconnect.service
    usbmux-refresh.service
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

if [ ! -f "${REPO_DIR}/app/iosbackupmachine.py" ]; then
    fail "Cannot find app/iosbackupmachine.py in ${REPO_DIR}. Run this script from the cloned repo directory."
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

if [ "${INSTALLED_VERSION}" = "${REPO_VERSION}" ] && [ "${IOSBACKUP_SKIP_VERSION_CHECK:-}" != "1" ]; then
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
    for entry in "${APP_FILES[@]}"; do
        install_name="${entry##*:}"
        src="${INSTALL_DIR}/${install_name}"
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

# Per-release reboot flag: a commit that needs a reboot bumps the integer in the
# repo's REBOOT_EPOCH file. If the installed epoch is behind, require a reboot
# (in addition to any overlay change above). Lets each release decide for itself
# instead of hardcoding "no reboot required".
REPO_REBOOT_EPOCH=$(cat "${REPO_DIR}/REBOOT_EPOCH" 2>/dev/null || echo 0)
INSTALLED_REBOOT_EPOCH=$(cat "${INSTALL_DIR}/.reboot_epoch" 2>/dev/null || echo 0)
case "${REPO_REBOOT_EPOCH}${INSTALLED_REBOOT_EPOCH}" in
    *[!0-9]*) REPO_REBOOT_EPOCH=0; INSTALLED_REBOOT_EPOCH=0 ;;  # non-numeric → ignore
esac
if [ "${IS_UPGRADE}" = true ] && [ "${REPO_REBOOT_EPOCH}" -gt "${INSTALLED_REBOOT_EPOCH}" ]; then
    NEED_REBOOT=true
    info "This update requires a reboot (reboot flag ${INSTALLED_REBOOT_EPOCH} -> ${REPO_REBOOT_EPOCH})"
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
    iptables
    sshpass
    rsync
    netcat-traditional
    iw
    wireless-tools
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
# requirements.txt stays at repo root
info "Python packages installed"

# ---------------------------------------------------------------------------
# Step: Install Waveshare e-Paper driver
# ---------------------------------------------------------------------------
step "Install Waveshare e-Paper driver"

if [ -d "${EPAPER_DIR}" ]; then
    info "e-Paper repository already exists at ${EPAPER_DIR}"
else
    git clone --quiet --depth 1 "${EPAPER_REPO}" "${EPAPER_DIR}"
    info "Cloned e-Paper repository"
fi

WAVESHARE_LIB="${EPAPER_DIR}/RaspberryPi_JetsonNano/python/lib/waveshare_epd"
if [ -d "${WAVESHARE_LIB}" ]; then
    cp "${REPO_DIR}/app/epdconfig.py" "${WAVESHARE_LIB}/epdconfig.py"
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

for entry in "${APP_FILES[@]}"; do
    repo_path="${entry%%:*}"
    install_name="${entry##*:}"
    src="${REPO_DIR}/${repo_path}"
    dst="${INSTALL_DIR}/${install_name}"
    if [ -f "${src}" ]; then
        cp "${src}" "${dst}"
        detail "Copied ${install_name}"
    else
        warn "File not found: ${src}"
    fi
done

# --- Config migration ---
if [ -f "${INSTALL_DIR}/config.yaml" ]; then
    info "Migrating config: versioned schema migration + new defaults"
    # config_schema.py (installed above) is the single migration step: it pulls
    # in any shipped defaults from the example, runs the versioned migration,
    # fills schema defaults, and saves atomically.
    "${VENV_DIR}/bin/python3" - "${INSTALL_DIR}" "${REPO_DIR}/config/config.yaml.example" "${INSTALL_DIR}/config.yaml" <<'PYEOF'
import sys, yaml

install_dir, example_path, config_path = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, install_dir)
import config_schema

with open(example_path, "r") as f:
    example = yaml.safe_load(f) or {}
with open(config_path, "r") as f:
    current = yaml.safe_load(f) or {}

# Bring in shipped example defaults (e.g. error_codes) without overwriting user
# values, then run the versioned schema migration + atomic save.
merged = config_schema._deep_merge(example, current)
merged = config_schema.migrate(merged)
merged = config_schema.apply_defaults(merged)
config_schema.atomic_save(merged, config_path)

added = [k for k in merged if k not in current]
if added:
    print(f"    Added new config keys: {', '.join(added)}")
PYEOF
else
    cp "${REPO_DIR}/config/config.yaml.example" "${INSTALL_DIR}/config.yaml"
    detail "Copied config.yaml (fresh install)"
fi

# Clean stale .py files from install dir that are no longer in APP_FILES
if [ "${IS_UPGRADE}" = true ]; then
    for f in "${INSTALL_DIR}"/*.py; do
        [ -f "$f" ] || continue
        base=$(basename "$f")
        found=false
        for entry in "${APP_FILES[@]}"; do
            install_name="${entry##*:}"
            if [ "$install_name" = "$base" ]; then
                found=true
                break
            fi
        done
        if [ "$found" = false ]; then
            rm -f "$f"
            detail "Removed stale file: $base"
        fi
    done
fi

# Clean Python bytecode cache
find "${INSTALL_DIR}" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

# Copy webui directories from app/ (remove first to avoid cp -r nesting)
for d in webui_templates webui_static; do
    if [ -d "${REPO_DIR}/app/${d}" ]; then
        rm -rf "${INSTALL_DIR}/${d}"
        cp -r "${REPO_DIR}/app/${d}" "${INSTALL_DIR}/${d}"
        info "Copied ${d}/"
    fi
done

# Make shell scripts executable
chmod +x "${INSTALL_DIR}/unplug-notify.sh" 2>/dev/null || true
chmod +x "${INSTALL_DIR}/shutdown.sh" 2>/dev/null || true
chmod +x "${INSTALL_DIR}/long-press-backup.sh" 2>/dev/null || true
chmod +x "${INSTALL_DIR}/wg-autoconnect.sh" 2>/dev/null || true
chmod +x "${INSTALL_DIR}/usbmux-refresh.sh" 2>/dev/null || true

info "Application files installed to ${INSTALL_DIR}"

# ---------------------------------------------------------------------------
# Step: Install systemd services and udev rules
# ---------------------------------------------------------------------------
step "Install systemd services and udev rules"

for f in "${REPO_DIR}"/config/*.rules; do
    [ -f "$f" ] || continue
    cp "$f" /etc/udev/rules.d/
    detail "Installed $(basename "$f")"
done

# Build list of service files from repo
REPO_SERVICES=()
for f in "${REPO_DIR}"/services/*.service; do
    [ -f "$f" ] || continue
    cp "$f" /etc/systemd/system/
    REPO_SERVICES+=("$(basename "$f")")
    detail "Installed $(basename "$f")"
done

# Remove stale service files from previous installs
if [ "${IS_UPGRADE}" = true ]; then
    for svc in "${ALL_SERVICES[@]}"; do
        found=false
        for rs in "${REPO_SERVICES[@]}"; do
            if [ "$svc" = "$rs" ]; then
                found=true
                break
            fi
        done
        if [ "$found" = false ] && [ -f "/etc/systemd/system/${svc}" ]; then
            systemctl disable "${svc}" 2>/dev/null || true
            rm -f "/etc/systemd/system/${svc}"
            detail "Removed stale service: ${svc}"
        fi
    done
fi

systemctl daemon-reload
info "Reloaded systemd daemon"

for svc in "${ENABLE_SERVICES[@]}"; do
    if [ -f "/etc/systemd/system/${svc}" ]; then
        systemctl enable "${svc}" >/dev/null 2>&1 || true
        if systemctl is-enabled --quiet "${svc}" 2>/dev/null; then
            detail "Enabled ${svc}"
        else
            warn "Could NOT enable ${svc} — it will not start at boot"
        fi
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

# Start the display daemon now on upgrades (SPI/I2C overlays are already active).
# On a fresh install the overlays need a reboot first, so it starts on next boot.
if [ "${IS_UPGRADE}" = true ] && [ -f /etc/systemd/system/iosbackupmachine.service ]; then
    systemctl restart iosbackupmachine.service 2>/dev/null && \
        info "Restarted iosbackupmachine.service (display daemon)" || \
        warn "Could not start iosbackupmachine.service (will start on next boot)"
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

mkdir -p "${LOG_DIR}" "${RUNTIME_DIR}"
info "Backup directory ready: ${BACKUP_DIR}"

# Migrate logs out of the volatile zram /var/log (and its on-disk backing store
# /var/log.hdd) into the persistent LOG_DIR, once. Older builds wrote logs to the
# zram-backed /var/log, where a power cut wiped anything not yet synced to disk.
# Copy without overwriting so re-running the installer is safe.
for src in "${RUNTIME_DIR}" /var/log.hdd/iosbackupmachine; do
    if [ -d "${src}" ] && [ "${src}" != "${LOG_DIR}" ]; then
        for f in "${src}"/*.log; do
            [ -e "${f}" ] || continue
            dest="${LOG_DIR}/$(basename "${f}")"
            [ -e "${dest}" ] || cp -p "${f}" "${dest}" 2>/dev/null || true
        done
    fi
done
info "Logs now persist in ${LOG_DIR}"

# Install logrotate config. Remove any stale copy first: older versions rotated
# the per-run logs under /var/log, which renamed them out of the web UI's view.
if [ -f "${REPO_DIR}/config/logrotate-iosbackupmachine" ]; then
    cp "${REPO_DIR}/config/logrotate-iosbackupmachine" /etc/logrotate.d/iosbackupmachine
    info "Installed logrotate config"
fi

# Install NetworkManager dispatcher for WireGuard WiFi auto-connect
if [ -d /etc/NetworkManager/dispatcher.d ] && [ -f "${REPO_DIR}/config/99-wg-autoconnect" ]; then
    cp "${REPO_DIR}/config/99-wg-autoconnect" /etc/NetworkManager/dispatcher.d/99-wg-autoconnect
    chmod +x /etc/NetworkManager/dispatcher.d/99-wg-autoconnect
    info "Installed NetworkManager dispatcher for WireGuard auto-connect"
fi

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

PISUGAR_CONFIG="${REPO_DIR}/config/[pisugar]config.json"
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
    # Wait for PiSugar server to be ready after restart
    for i in 1 2 3 4 5; do
        if echo "rtc_pi2rtc" | nc -q 1 127.0.0.1 8423 2>/dev/null; then
            info "RTC synced"
            break
        fi
        sleep 2
    done
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

# Check the display daemon (single EPD owner)
if systemctl is-active --quiet iosbackupmachine.service 2>/dev/null; then
    info "iosbackupmachine.service (display daemon) is running"
elif [ "${NEED_REBOOT}" = true ]; then
    info "iosbackupmachine.service will start after the reboot"
else
    warn "iosbackupmachine.service is not running (check: journalctl -u iosbackupmachine)"
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

# Record the reboot epoch we just installed, so the next update can compare.
echo "${REPO_REBOOT_EPOCH}" > "${INSTALL_DIR}/.reboot_epoch" 2>/dev/null || true

if [ "${NEED_REBOOT}" = true ]; then
    echo -e "  ${YELLOW}⚠ A reboot is required to apply this update.${NC}"
    echo ""
    read -rp "  Reboot now? [y/N] " answer
    if [[ "${answer}" =~ ^[Yy]$ ]]; then
        echo "  Rebooting..."
        reboot
    else
        echo -e "  ${YELLOW}Remember to reboot to finish applying the update.${NC}"
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
