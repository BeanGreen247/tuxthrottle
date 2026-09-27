#!/usr/bin/env bash
# Compatibility shim: the osu! launcher moved to the osu_lazer_tools module (github.com/BeanGreen247/osu-lazer-tools).
# Older ~/.local/bin/osu-lazer-launcher wrappers point here; re-run Setup Games → osu!lazer step 2 to repoint them.
exec bash "$(dirname "$(readlink -f "$0")")/osu_lazer_tools/osu-lazer-launcher.sh" "$@"
