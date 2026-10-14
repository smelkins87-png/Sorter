#!/usr/bin/env bash
# Dark theme for the sorter's touchscreen app.
#
#   sudo bash deploy/theme.sh          install the theme
#   sudo bash deploy/theme.sh --undo   back to the vendor look
#
# Adds a stylesheet and a background image to the touchscreen app and one
# <link> line to its index.html. Nothing else in the app changes.
set -euo pipefail

APP=/home/pi/magic-sorter-touch
ASSETS=$APP/assets
INDEX=$APP/index.html
MARK="<!-- localsorter theme -->"
HERE=$(cd "$(dirname "$0")" && pwd)

die() { echo "error: $*" >&2; exit 1; }
say() { echo "==> $*"; }

[[ $EUID -eq 0 ]] || die "run with sudo"
[[ -f $INDEX ]] || die "$INDEX not found: is this the sorter's Pi?"

restart_touch() {
    if systemctl is-enabled --quiet magicsorter_touch.service 2>/dev/null; then
        say "Restarting the touchscreen app"
        systemctl restart magicsorter_touch.service
    fi
}

if [[ ${1:-} == --undo ]]; then
    say "Removing the theme"
    sed -i "\|$MARK|d" "$INDEX"
    rm -f "$ASSETS/localsorter-theme.css" "$ASSETS/background.jpg"
    restart_touch
    exit 0
fi

[[ -z ${1:-} ]] || die "unknown option: $1 (use --undo)"

say "Copying theme files to $ASSETS"
install -o pi -g pi -m 0644 "$HERE/theme/localsorter-theme.css" "$ASSETS/localsorter-theme.css"
install -o pi -g pi -m 0644 "$HERE/theme/background.jpg" "$ASSETS/background.jpg"

if ! grep -qF "$MARK" "$INDEX"; then
    [[ -f $INDEX.pre-theme ]] || cp -p "$INDEX" "$INDEX.pre-theme"
    grep -q 'assets/overrides.css' "$INDEX" || die "unexpected $INDEX layout; nothing changed"
    say "Linking the stylesheet in $INDEX"
    # After the vendor's own overrides, so ours win
    sed -i "\|assets/overrides.css|a\\  <link rel=\"stylesheet\" href=\"./assets/localsorter-theme.css\">$MARK" "$INDEX"
fi

restart_touch
say "Done. To swap the background, replace $ASSETS/background.jpg (dark backgrounds blend best)."
