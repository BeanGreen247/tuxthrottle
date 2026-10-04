#!/usr/bin/env bash
# osu-tools: one command for every osu-lazer-tools helper. Run without arguments for the list.
set -euo pipefail
HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"

usage() {
    local b=$'\e[1m' d=$'\e[2m' o=$'\e[0m'
    [[ -t 1 ]] || { b=; d=; o=; }
    cat <<EOF
${b}osu-tools${o} - osu!lazer helpers for Linux          ${d}osu-tools COMMAND --help for details${o}

${b}Players${o}
  launch            start osu! with low-latency tweaks (same as osu-lazer-launcher)
  diag              live latency dashboard: audio, GPU, input, kernel, what to change
  settings          apply competitive game settings (backs up first)
  replay            your replays: unstable rate, hit-error graph, offset advice, pp, export
  tap               tapping speed test (stream BPM, consistency, stamina)
  input             tablet setup check, tablet area / mouse DPI / sensitivity calculators
  skincheck         check a skin (.osk or folder) for broken HD images, missing frames, skin.ini mistakes
  backup            back up / restore beatmaps, skins, scores, collections and settings
  report            read osu!'s logs, make a bug-report bundle for GitHub

${b}Mappers${o}
  mapcheck          check a mapset (folder/.osz/.osu) before submitting: snapping, files, metadata, audio
  maptools          sync metadata, copy hitsounds between difficulties, resnap, clean timing, shift offset

${b}Developers${o}
  dev               build/run/test osu! from source, try pull requests, ruleset template, star rating
  report --dev      logs of your source build
EOF
}

py() { exec python3 "$HERE/$1" "${@:2}"; }

case "${1:-}" in
    launch) shift; exec bash "$HERE/osu-lazer-launcher.sh" run "$@" ;;
    diag) shift; py osu_lazer_diag.py "$@" ;;
    settings) shift; py osu_lazer_settings.py "$@" ;;
    replay) shift; py osu_replay.py "$@" ;;
    tap) shift; py osu_tap.py "$@" ;;
    input|tablet|mouse) shift; py osu_input.py "$@" ;;
    skincheck|skin) shift; py osu_skincheck.py "$@" ;;
    backup) shift; py osu_backup.py "$@" ;;
    report|logs) [[ $1 == logs ]] && set -- report logs "${@:2}"; shift; py osu_report.py "$@" ;;
    mapcheck) shift; py osu_mapcheck.py "$@" ;;
    maptools) shift; py osu_maptools.py "$@" ;;
    dev) shift; exec bash "$HERE/osu-dev.sh" "$@" ;;
    ""|-h|--help|help) usage ;;
    *) echo "osu-tools: unknown command '$1'" >&2; usage >&2; exit 1 ;;
esac
