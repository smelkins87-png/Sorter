#!/usr/bin/env bash
# Cuts the Magic Sorter off from the vendor cloud and points it at localsorter.
#
#   sudo deploy/lockdown.sh            point the sorter at the local server and
#                                      block the vendor/telemetry hosts
#   sudo deploy/lockdown.sh --strict   ...and add a firewall: nothing but the
#                                      localsorter sync (and apt) may reach the
#                                      internet; the LAN keeps working
#   sudo deploy/lockdown.sh --undo     restore the vendor setup
#
# Safe to re-run. Run deploy/install.sh first.
set -euo pipefail

SORTER_CONFIG=/home/pi/.magicsorter/config.json
BACKUP=/home/pi/.magicsorter/config.json.vendor-backup
ENDPOINT_FILE=/etc/localsorter.json
HOSTS=/etc/hosts
MARK_BEGIN="# >>> localsorter lockdown >>>"
MARK_END="# <<< localsorter lockdown <<<"
FIREWALL_RULES=/etc/localsorter/firewall.nft
FIREWALL_UNIT=/etc/systemd/system/localsorter-firewall.service

# Every outside host the vendor software contacts (from the v4.5 image):
#   api*.magic-sorter.com  card photos + device id/secret, set lists, config backup, results email
#   www.magic-sorter.com   self-update downloads (installed without verification)
#   *.ingest.sentry.io     crash reports and warning logs tagged with the device id
#   ipify/ifconfig/icanhazip  public IP lookup
BLOCKED_HOSTS=(
    magic-sorter.com www.magic-sorter.com api.magic-sorter.com
    api2.magic-sorter.com api3.magic-sorter.com api4.magic-sorter.com api5.magic-sorter.com
    o509538.ingest.sentry.io o509538.ingest.us.sentry.io o509538.ingest.de.sentry.io
    api.ipify.org ifconfig.me icanhazip.com
)

SORTER_SERVICES=(magicsorter_py.service magicsorter_touch.service magicsorter_cp.service)

die() { echo "error: $*" >&2; exit 1; }
say() { echo "==> $*"; }

[[ $EUID -eq 0 ]] || die "run with sudo"

MODE=apply
case "${1:-}" in
    "") ;;
    --strict) MODE=strict ;;
    --undo) MODE=undo ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) die "unknown option: $1 (use --strict, --undo or --help)" ;;
esac

local_endpoint() {
    python3 - "$ENDPOINT_FILE" <<'EOF'
import json, sys
try:
    with open(sys.argv[1]) as f:
        cfg = json.load(f)
except FileNotFoundError:
    cfg = {}
host = cfg.get("listen_host", "127.0.0.1")
if host in ("0.0.0.0", "::", ""):
    host = "127.0.0.1"
print("http://%s:%d" % (host, cfg.get("listen_port", 8765)))
EOF
}

set_endpoint() {
    # Rewrites only the "endpoint" key, keeping the vendor's 4-space layout
    sudo -u pi python3 - "$SORTER_CONFIG" "$1" <<'EOF'
import json, sys
path, endpoint = sys.argv[1], sys.argv[2]
with open(path) as f:
    cfg = json.load(f)
cfg["endpoint"] = endpoint
with open(path, "w") as f:
    json.dump(cfg, f, indent=4)
EOF
}

strip_hosts_block() {
    if grep -qF "$MARK_BEGIN" "$HOSTS"; then
        sed -i "/^$MARK_BEGIN\$/,/^$MARK_END\$/d" "$HOSTS"
    fi
}

restart_sorter() {
    say "Restarting the sorter software"
    for unit in "${SORTER_SERVICES[@]}"; do
        if systemctl is-enabled --quiet "$unit" 2>/dev/null; then
            systemctl restart "$unit" || echo "    (could not restart $unit)"
        fi
    done
}

remove_firewall() {
    if [[ -f $FIREWALL_UNIT ]]; then
        say "Removing firewall"
        systemctl disable --now localsorter-firewall.service 2>/dev/null || true
        rm -f "$FIREWALL_UNIT"
        systemctl daemon-reload
    fi
    nft delete table inet localsorter 2>/dev/null || true
    rm -f "$FIREWALL_RULES"
}

install_firewall() {
    command -v nft >/dev/null || die "nft not found (sudo apt-get install nftables)"
    id localsorter >/dev/null 2>&1 || die "user 'localsorter' missing: run deploy/install.sh first"

    local apt_rule=""
    if id _apt >/dev/null 2>&1; then
        apt_rule='meta skuid "_apt" tcp dport { 80, 443 } accept comment "OS security updates"'
    fi

    say "Writing firewall rules to $FIREWALL_RULES"
    mkdir -p "$(dirname "$FIREWALL_RULES")"
    cat > "$FIREWALL_RULES" <<EOF
# Managed by localsorter deploy/lockdown.sh --strict. Outbound only: inbound
# traffic (SSH, VNC, Samba, the touch UI) is not touched.
table inet localsorter
delete table inet localsorter
table inet localsorter {
    chain output {
        type filter hook output priority 0; policy drop;

        oifname "lo" accept
        ct state established,related accept

        # Local network: router, PCs on the LAN, Samba, mDNS
        ip daddr { 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 169.254.0.0/16, 224.0.0.0/4, 255.255.255.255 } accept
        ip6 daddr { fc00::/7, fe80::/10, ff00::/8 } accept
        # DHCPv6 / neighbour discovery
        meta l4proto ipv6-icmp accept

        # Name resolution and clock
        udp dport { 53, 123 } accept
        tcp dport 53 accept

        # Scryfall card data, prices and images
        meta skuid "localsorter" tcp dport 443 accept comment "localsorter sync"
        $apt_rule

        limit rate 6/minute log prefix "localsorter-blocked: " level info
        counter drop
    }
}
EOF
    nft -c -f "$FIREWALL_RULES" || die "firewall rules failed validation; nothing applied"

    cat > "$FIREWALL_UNIT" <<EOF
[Unit]
Description=localsorter outbound firewall
After=network-pre.target
Wants=network-pre.target
Before=magicsorter_py.service magicsorter_touch.service magicsorter_cp.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f $FIREWALL_RULES
ExecStop=/usr/sbin/nft delete table inet localsorter

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable localsorter-firewall.service
    systemctl restart localsorter-firewall.service
    say "Firewall active. Blocked attempts are logged: journalctl -k | grep localsorter-blocked"
}

if [[ $MODE == undo ]]; then
    remove_firewall
    say "Removing host blocks from $HOSTS"
    strip_hosts_block
    if [[ -f $BACKUP ]]; then
        vendor_endpoint=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("endpoint",""))' "$BACKUP")
        say "Restoring sorter endpoint to ${vendor_endpoint:-https://api2.magic-sorter.com}"
        set_endpoint "${vendor_endpoint:-https://api2.magic-sorter.com}"
    else
        echo "    no backup at $BACKUP; endpoint left as is"
    fi
    restart_sorter
    say "Done. The sorter uses the vendor cloud again."
    exit 0
fi

[[ -f $SORTER_CONFIG ]] || die "$SORTER_CONFIG not found: is this the sorter's Pi?"
systemctl is-active --quiet localsorter.service || echo "warning: localsorter.service is not running (sudo systemctl status localsorter)"

if [[ ! -f $BACKUP ]]; then
    say "Backing up the vendor config to $BACKUP"
    cp -p "$SORTER_CONFIG" "$BACKUP"
fi

ENDPOINT=$(local_endpoint)
say "Pointing the sorter at $ENDPOINT"
set_endpoint "$ENDPOINT"

say "Blocking vendor and telemetry hosts in $HOSTS"
strip_hosts_block
{
    echo "$MARK_BEGIN"
    for host in "${BLOCKED_HOSTS[@]}"; do
        echo "0.0.0.0 $host"
        echo ":: $host"
    done
    echo "$MARK_END"
} >> "$HOSTS"

if [[ $MODE == strict ]]; then
    install_firewall
else
    remove_firewall
fi

restart_sorter

cat <<EOF

Done. Check it:
  curl -s $ENDPOINT/status
  tail -f /tmp/magicsorter.log        (look for "Matching card: ...")
Scans are written to the "Public" share, folder "scans".
EOF
