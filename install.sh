#!/usr/bin/env bash
# osu-lazer-tools installer (per user, no root).
#   ./install.sh [PATH/TO/osu.AppImage | --download]   copy the tools to ~/.local/share/osu-lazer-tools and set up
#                                                     the osu-lazer-launcher command, menu entry and config
#   ./install.sh --uninstall                           remove all of that (osu! and its data are not touched)
set -euo pipefail
SRC="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
DEST="${XDG_DATA_HOME:-$HOME/.local/share}/osu-lazer-tools"
FILES=(osu-lazer-launcher.sh osu_lazer_diag.py osu_lazer_settings.py LICENSE README.md)

if [[ "${1:-}" == --uninstall ]]; then
    [[ -x "$DEST/osu-lazer-launcher.sh" ]] && bash "$DEST/osu-lazer-launcher.sh" uninstall || true
    rm -rf "$DEST"
    echo ":: removed $DEST"
    exit 0
fi

if [[ "$SRC" != "$DEST" ]]; then
    mkdir -p "$DEST"
    for f in "${FILES[@]}"; do install -m "$( [[ $f == *.sh || $f == *.py ]] && echo 755 || echo 644 )" "$SRC/$f" "$DEST/$f"; done
    echo ":: tools copied to $DEST"
fi
command -v gamemoderun >/dev/null || echo "note: GameMode is required at launch time (sudo dnf install gamemode / apt install gamemode)"
python3 -c 'import textual' 2>/dev/null || echo "note: 'osu-lazer-launcher diag' needs Textual for the live view (python3-textual); --once works without it"
exec bash "$DEST/osu-lazer-launcher.sh" install "$@"
