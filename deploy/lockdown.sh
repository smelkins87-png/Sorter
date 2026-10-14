#!/usr/bin/env bash
# Cuts the Magic Sorter off from the vendor cloud and points it at localsorter.
#
#   sudo bash deploy/lockdown.sh       point the sorter at the local server, block
#                                      the vendor/telemetry hosts, switch off
#                                      automatic update checks, and firewall all
#                                      outgoing traffic except the local network
#                                      and localsorter's Scryfall sync
#   ... --allow-os-updates             also let Raspberry Pi OS updates through
#                                      (apt; still only when you run them)
#   ... --no-firewall                  host blocks only (not recommended)
#   sudo bash deploy/lockdown.sh --check   show what is blocked and what got through
#   sudo bash deploy/lockdown.sh --undo    restore the vendor setup
#
# SSH, VNC and Samba from the local network keep working. Remote-access
# services that go through the internet (Tailscale, Raspberry Pi Connect,
# RealVNC cloud) are blocked by the firewall; use --no-firewall if you need one.
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
OS_UPDATES_STATE=/etc/localsorter/os-updates.disabled

# Every outside host the vendor software contacts (from the v4.5 image):
#   api*.magic-sorter.com  card photos + device id/secret, set lists, config backup, results email
#   www.magic-sorter.com   self-update downloads (installed without verification)
#   *.ingest.sentry.io     crash reports and warning logs tagged with the device id
#   ipify/ifconfig/icanhazip  public IP lookup
#   magic-sorter-public.s3.amazonaws.com  vendor image downloads
# Both the sorter software (at every boot) and the touchscreen app (at start,
# silently, no prompt) download and install vendor updates from
# www.magic-sorter.com. The firewall below stops any address not listed here.
BLOCKED_HOSTS=(
    magic-sorter.com www.magic-sorter.com api.magic-sorter.com
    api2.magic-sorter.com api3.magic-sorter.com api4.magic-sorter.com api5.magic-sorter.com
    magic-sorter-public.s3.amazonaws.com
    o509538.ingest.sentry.io o509538.ingest.us.sentry.io o509538.ingest.de.sentry.io
    api.ipify.org ifconfig.me icanhazip.com
)

# Raspberry Pi OS: daily package-list downloads, and the desktop's update
# notifier (PackageKit). unattended-upgrades is not installed on this image,
# so nothing is ever installed automatically; these only check
OS_UPDATE_UNITS=(apt-daily.timer apt-daily-upgrade.timer packagekit.service)

SORTER_SERVICES=(magicsorter_py.service magicsorter_touch.service magicsorter_cp.service)

die() { echo "error: $*" >&2; exit 1; }
say() { echo "==> $*"; }

[[ $EUID -eq 0 ]] || die "run with sudo"

MODE=apply
FIREWALL=1
ALLOW_OS_UPDATES=0

for arg in "$@"; do
    case "$arg" in
        --strict) ;;                       # the default now; kept for old instructions
        --no-firewall) FIREWALL=0 ;;
        --allow-os-updates) ALLOW_OS_UPDATES=1 ;;
        --undo) MODE=undo ;;
        --check) MODE=check ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        *) die "unknown option: $arg (see --help)" ;;
    esac
done

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

    local apt_rule="# OS updates blocked (lockdown.sh --allow-os-updates lets apt through)"
    if [[ $ALLOW_OS_UPDATES -eq 1 ]] && id _apt >/dev/null 2>&1; then
        apt_rule='meta skuid "_apt" tcp dport { 80, 443 } accept comment "OS updates, run by hand"'
    fi

    say "Writing firewall rules to $FIREWALL_RULES"
    mkdir -p "$(dirname "$FIREWALL_RULES")"
    cat > "$FIREWALL_RULES" <<EOF
# Managed by localsorter deploy/lockdown.sh. Outbound only: inbound
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

disable_os_update_checks() {
    say "Switching off automatic update checks (apt daily timers, PackageKit)"
    mkdir -p "$(dirname "$OS_UPDATES_STATE")"
    : > "$OS_UPDATES_STATE.new"
    for unit in "${OS_UPDATE_UNITS[@]}"; do
        state=$(systemctl is-enabled "$unit" 2>/dev/null || true)
        # Remember the original state once, for --undo
        if [[ ! -f $OS_UPDATES_STATE ]] && [[ -n $state ]]; then
            echo "$unit $state" >> "$OS_UPDATES_STATE.new"
        fi
        systemctl stop "$unit" 2>/dev/null || true
        systemctl mask "$unit" >/dev/null 2>&1 || true
    done
    if [[ -f $OS_UPDATES_STATE ]]; then
        rm -f "$OS_UPDATES_STATE.new"
    else
        mv "$OS_UPDATES_STATE.new" "$OS_UPDATES_STATE"
    fi
}

restore_os_update_checks() {
    [[ -f $OS_UPDATES_STATE ]] || return 0
    say "Restoring automatic update checks"
    for unit in "${OS_UPDATE_UNITS[@]}"; do
        systemctl unmask "$unit" >/dev/null 2>&1 || true
    done
    while read -r unit state; do
        case "$state" in
            enabled) systemctl enable --now "$unit" >/dev/null 2>&1 || true ;;
            static) systemctl start "$unit" >/dev/null 2>&1 || true ;;
        esac
    done < "$OS_UPDATES_STATE"
    rm -f "$OS_UPDATES_STATE"
}

check() {
    echo "Sorter endpoint:   $(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("endpoint"))' "$SORTER_CONFIG" 2>/dev/null || echo "?")"
    if grep -qF "$MARK_BEGIN" "$HOSTS"; then
        echo "Host blocks:       $(sed -n "/^$MARK_BEGIN\$/,/^$MARK_END\$/p" "$HOSTS" | grep -c '^0.0.0.0') vendor/telemetry names blocked"
    else
        echo "Host blocks:       NOT installed"
    fi
    if nft list table inet localsorter >/dev/null 2>&1; then
        dropped=$(nft list table inet localsorter | sed -n 's/.*counter packets \([0-9]*\).*drop.*/\1/p' | tail -1)
        echo "Firewall:          active, ${dropped:-0} outgoing packets dropped since boot"
    else
        echo "Firewall:          NOT active"
    fi
    for unit in "${OS_UPDATE_UNITS[@]}"; do
        # is-enabled prints "masked" *and* fails for masked units
        state=$(systemctl is-enabled "$unit" 2>/dev/null || true)
        printf "%-19s%s\n" "$unit:" "${state:-absent}"
    done
    echo
    echo "Blocked outgoing attempts in the last 24 h (destination, count):"
    journalctl -k --since "-24h" --no-pager 2>/dev/null | grep -o 'localsorter-blocked: .*DST=[0-9a-f.:]*' \
        | sed 's/.*DST=//' | sort | uniq -c | sort -rn | head -20 || true
    echo
    echo "Open connections leaving the local network right now:"
    ss -tnpH state established 2>/dev/null | awk '{print $4, $NF}' \
        | grep -vE '^(127\.|10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.|169\.254\.|\[?::1|\[?fe80|\[?f[cd])' || echo "  none"
}

if [[ $MODE == check ]]; then
    check
    exit 0
fi

if [[ $MODE == undo ]]; then
    remove_firewall
    restore_os_update_checks
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

if [[ $FIREWALL -eq 1 ]]; then
    install_firewall
else
    remove_firewall
fi

if [[ $ALLOW_OS_UPDATES -eq 1 ]]; then
    restore_os_update_checks
else
    disable_os_update_checks
fi

restart_sorter

cat <<EOF

Done. Check it:
  sudo bash deploy/lockdown.sh --check
  curl -s $ENDPOINT/status
  tail -f /tmp/magicsorter.log        (look for "Matching card: ...")
Scans are written to the "Public" share, folder "scans".
EOF
