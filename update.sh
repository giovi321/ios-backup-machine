#!/bin/bash
# update.sh - One-command updater for iOS Backup Machine
#
# Pulls the latest code from GitHub and runs the installer.
# Must be run as root.
#
# Usage:
#   bash /root/ios-backup-machine/update.sh

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

echo ""
echo -e "${BLUE}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║      iOS Backup Machine - Updater                          ║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════════════════════════╝${NC}"
echo ""

if [ "$(id -u)" -ne 0 ]; then
    echo -e "${RED}  ✗ This script must be run as root. Try: sudo bash $0${NC}"
    exit 1
fi

if [ ! -d "${REPO_DIR}/.git" ]; then
    echo -e "${RED}  ✗ Not a git repository: ${REPO_DIR}${NC}"
    exit 1
fi

# Show current and available versions
CURRENT_VERSION=$(grep -oP 'VERSION\s*=\s*"\K[^"]+' "${REPO_DIR}/webui.py" 2>/dev/null || echo "unknown")
echo -e "  Current version: ${CURRENT_VERSION}"

# Pull latest code
echo -e "  ${BLUE}Pulling latest code from GitHub...${NC}"
cd "${REPO_DIR}"
git fetch --quiet origin 2>/dev/null || true

# Check if there are updates
LOCAL=$(git rev-parse HEAD 2>/dev/null)
REMOTE=$(git rev-parse origin/main 2>/dev/null || echo "")

if [ -n "${REMOTE}" ] && [ "${LOCAL}" = "${REMOTE}" ]; then
    echo -e "  ${GREEN}✓ Already up to date (${CURRENT_VERSION}).${NC}"
    echo ""
    read -rp "  Re-install anyway? [y/N] " answer
    if [[ ! "${answer}" =~ ^[Yy]$ ]]; then
        echo "  Done."
        exit 0
    fi
fi

git pull --quiet origin main || {
    echo -e "${RED}  ✗ git pull failed. Check network and repository access.${NC}"
    exit 1
}

NEW_VERSION=$(grep -oP 'VERSION\s*=\s*"\K[^"]+' "${REPO_DIR}/webui.py" 2>/dev/null || echo "unknown")
echo -e "  ${GREEN}✓ Updated to ${NEW_VERSION}${NC}"

# Show changelog (commits since last installed version)
INSTALLED_VERSION_FILE="/root/iosbackupmachine/.installed_version"
if [ -f "${INSTALLED_VERSION_FILE}" ]; then
    echo ""
    echo -e "  ${BLUE}Recent changes:${NC}"
    git log --oneline -10 "${LOCAL}..HEAD" 2>/dev/null | while IFS= read -r line; do
        echo -e "    ${line}"
    done
    echo ""
fi

# Run installer with skip-version-check flag (we already confirmed above)
echo -e "  ${BLUE}Running installer...${NC}"
echo ""
export IOSBACKUP_SKIP_VERSION_CHECK=1
exec bash "${REPO_DIR}/install.sh"
