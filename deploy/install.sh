#!/bin/bash
# Installs localsorter on the Raspberry Pi. Safe to re-run: the package and the
# units are replaced, while the settings file, card database and scans are kept.
#
# Usage, from the repository root:   sudo bash deploy/install.sh
set -euo pipefail

# The units run /usr/bin/python3, so check that interpreter specifically
PY=/usr/bin/python3

usage() {
    echo "Usage: sudo bash deploy/install.sh   (run from the repository root)" >&2
    exit 1
}

if [[ $EUID -ne 0 ]] || [[ ! -f ./localsorter/__init__.py ]]; then
    usage
fi

step() {
    echo
    echo "==> $*"
}

step "Checking Python packages for $PY"
if ! "$PY" -c "import numpy, PIL, cv2"; then
    echo "Missing Python packages. Install them with:" >&2
    echo "  sudo apt-get install python3-numpy python3-pil python3-opencv" >&2
    exit 1
fi
echo "numpy, PIL and cv2 are available"

step "Checking Tesseract (reads set code and collector number)"
if ! command -v tesseract >/dev/null; then
    echo "Installing tesseract-ocr"
    apt-get install -y tesseract-ocr tesseract-ocr-eng
fi
tesseract --version | head -1

step "Ensuring system user localsorter exists"
if id -u localsorter >/dev/null 2>&1; then
    echo "User localsorter already exists"
else
    useradd --system --home /var/lib/localsorter --shell /usr/sbin/nologin localsorter
    echo "Created user localsorter"
fi

step "Copying the localsorter package to /opt/localsorter/localsorter"
mkdir -p /opt/localsorter
rm -rf /opt/localsorter/localsorter
cp -r ./localsorter /opt/localsorter/localsorter
find /opt/localsorter/localsorter -name __pycache__ -type d -prune -exec rm -rf {} +
chown -R root:root /opt/localsorter
chmod -R u=rwX,go=rX /opt/localsorter
echo "Package installed (root-owned, read-only for the service)"

step "Creating data and scan directories"
if ! getent group pi >/dev/null; then
    echo "No group 'pi' on this system. The scan folder is shared with that group;" >&2
    echo "adjust the chown below in deploy/install.sh." >&2
    exit 1
fi
mkdir -p /var/lib/localsorter /home/pi/public/scans
chown localsorter:localsorter /var/lib/localsorter
# Setgid 2775: new scans inherit group pi, so the Samba user pi can read and delete them
chown localsorter:pi /home/pi/public/scans
chmod 2775 /home/pi/public/scans
echo "/var/lib/localsorter (localsorter) and /home/pi/public/scans (localsorter:pi, 2775) ready"

step "Installing /etc/localsorter.json"
if [[ -e /etc/localsorter.json ]]; then
    echo "Keeping existing /etc/localsorter.json"
else
    install -m 0644 deploy/localsorter.json /etc/localsorter.json
    echo "Installed default settings to /etc/localsorter.json"
fi

step "Installing systemd units"
for unit in localsorter.service localsorter-sync.service localsorter-sync.timer; do
    install -m 0644 "deploy/systemd/$unit" "/etc/systemd/system/$unit"
    echo "Installed /etc/systemd/system/$unit"
done
systemctl daemon-reload
systemctl enable localsorter-sync.timer localsorter.service
systemctl start localsorter-sync.timer
# Restart rather than start so a re-run picks up the new code
systemctl restart localsorter.service
echo "Timer and server enabled; server restarted"

step "Done. Next steps"
cat <<'EOF'
1. Build the recognition index. The first sync takes hours (it downloads card
   data and hashes every image), so start it and follow its progress:
     sudo systemctl start localsorter-sync.service
     journalctl -fu localsorter-sync
   or run it in the foreground as the service user:
     sudo -u localsorter env PYTHONPATH=/opt/localsorter python3 -m localsorter --config /etc/localsorter.json sync

2. Check that the server is up:
     curl -s http://127.0.0.1:8765/status

3. Once the index is ready, point the sorter at this server:
     sudo bash deploy/lockdown.sh
EOF
