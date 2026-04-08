#!/bin/bash
# wg-autoconnect.sh — Auto-connect WireGuard based on config triggers.
# Called at boot (via systemd) and can be called by NetworkManager dispatcher.
# Checks config for auto_connect and auto_connect_on triggers.

VENV=/root/iosbackupmachine
PY="$VENV/bin/python3"
CFG=/root/iosbackupmachine/config.yaml
TRIGGER="${1:-boot}"  # "boot", "wifi", "iphone"

# Check if WireGuard auto-connect is enabled and this trigger is selected
SHOULD_CONNECT=$("$PY" -c "
import yaml, sys
try:
    with open('$CFG') as f:
        cfg = yaml.safe_load(f) or {}
    wg = cfg.get('wireguard', {})
    if not wg.get('enabled') or not wg.get('auto_connect'):
        sys.exit(1)
    triggers = wg.get('auto_connect_on', ['iphone'])
    if '$TRIGGER' in triggers:
        sys.exit(0)
    sys.exit(1)
except:
    sys.exit(1)
" 2>/dev/null && echo "yes" || echo "no")

if [ "$SHOULD_CONNECT" != "yes" ]; then
    exit 0
fi

IFACE=$("$PY" -c "
import yaml
with open('$CFG') as f:
    cfg = yaml.safe_load(f) or {}
print(cfg.get('wireguard', {}).get('interface_name', 'wg0'))
" 2>/dev/null || echo "wg0")

# Skip if already up
if ip link show "$IFACE" >/dev/null 2>&1; then
    exit 0
fi

# Try to start WireGuard
"$PY" -c "
import wg_manager
ok, err = wg_manager.start_wireguard('$IFACE')
if not ok:
    print(f'[WG-AUTO] Failed: {err}')
else:
    print(f'[WG-AUTO] Connected {\"$IFACE\"} (trigger: $TRIGGER)')
" 2>&1
