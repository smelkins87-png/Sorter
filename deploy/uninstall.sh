#!/bin/bash
# Removes the localsorter services, timer and package from /opt/localsorter.
# Keeps the card database, the settings file, the localsorter user and all scans.
#
# Usage:   sudo deploy/uninstall.sh
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Usage: sudo deploy/uninstall.sh" >&2
    exit 1
fi

step() {
    echo
    echo "==> $*"
}

step "Stopping and disabling units"
# Errors are ignored so that a re-run on a partly removed install still works
systemctl disable --now localsorter.service localsorter-sync.timer 2>/dev/null || true
systemctl stop localsorter-sync.service 2>/dev/null || true

step "Removing units"
rm -f /etc/systemd/system/localsorter.service \
      /etc/systemd/system/localsorter-sync.service \
      /etc/systemd/system/localsorter-sync.timer
systemctl daemon-reload
systemctl reset-failed localsorter.service localsorter-sync.service 2>/dev/null || true

step "Removing /opt/localsorter"
rm -rf /opt/localsorter

step "Kept in place (delete by hand if no longer needed)"
echo "  card database and index:  /var/lib/localsorter"
echo "  settings:                 /etc/localsorter.json"
echo "  saved scans and photos:   /home/pi/public/scans"
echo "  system user:              localsorter"

step "Reminder"
echo "The sorter still points at this server until you restore the vendor endpoint:"
echo "  sudo deploy/lockdown.sh --undo"
