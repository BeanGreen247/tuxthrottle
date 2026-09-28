#!/usr/bin/env bash
# osu-lazer-tools installer (per user, no root).
#   ./install.sh [PATH/TO/osu.AppImage | --download]   copy the tools to ~/.local/share/osu-lazer-tools and set up
#                                                     the osu-lazer-launcher command, menu entry and config,
#                                                     plus the osu-tools command (every other helper)
#   ./install.sh --uninstall                           remove all of that (osu! and its data are not touched)
set -euo pipefail
SRC="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
DEST="${XDG_DATA_HOME:-$HOME/.local/share}/osu-lazer-tools"
TOOLS_BIN="$HOME/.local/bin/osu-tools"
FILES=(osu-lazer-launcher.sh osu-tools.sh osu-dev.sh osu_common.py osu_lazer_diag.py osu_lazer_settings.py
       osu_replay.py osu_mapcheck.py osu_maptools.py osu_skincheck.py osu_input.py osu_tap.py osu_backup.py
       osu_report.py LICENSE README.md)

if [[ "${1:-}" == --uninstall ]]; then
    [[ -x "$DEST/osu-lazer-launcher.sh" ]] && bash "$DEST/osu-lazer-launcher.sh" uninstall || true
    rm -rf "$DEST" "$TOOLS_BIN"
    echo ":: removed $DEST and $TOOLS_BIN"
    exit 0
fi

if [[ "$SRC" != "$DEST" ]]; then
    mkdir -p "$DEST"
    for f in "${FILES[@]}"; do install -m "$( [[ $f == *.sh || $f == *.py ]] && echo 755 || echo 644 )" "$SRC/$f" "$DEST/$f"; done
    echo ":: tools copied to $DEST"
fi
mkdir -p "$(dirname "$TOOLS_BIN")"
printf '#!/usr/bin/env bash\n# osu-tools: thin wrapper around the osu-lazer-tools copy in %s\nexec bash "%s/osu-tools.sh" "$@"\n' \
    "$DEST" "$DEST" > "$TOOLS_BIN"
chmod 755 "$TOOLS_BIN"
echo ":: osu-tools installed ($TOOLS_BIN): run 'osu-tools' to see every helper"
command -v gamemoderun >/dev/null || echo "note: GameMode is required at launch time (sudo dnf install gamemode / apt install gamemode)"
python3 -c 'import textual' 2>/dev/null || echo "note: 'osu-lazer-launcher diag' needs Textual for the live view (python3-textual); --once works without it"
python3 -c 'import rosu_pp_py' 2>/dev/null || echo "note: 'pip install --user rosu-pp-py' adds star rating and pp to 'osu-tools replay' and 'mapcheck'"
exec bash "$DEST/osu-lazer-launcher.sh" install "$@"
