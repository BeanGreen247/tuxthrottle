#!/usr/bin/env bash
# osu-dev: build, run and test osu!lazer from source on Linux (for contributors, ruleset authors, and players
# who want to try a pull request before it ships). Part of osu-lazer-tools.
#
# Usage:
#   osu-dev setup [--framework] [--tools] [--install-dotnet]
#                                clone ppy/osu into $OSU_DEV_DIR (default ~/osu-dev), optionally osu-framework
#                                and osu-tools next to it; checks the .NET SDK version osu! needs
#   osu-dev build [Release]      build osu.Desktop
#   osu-dev run [osu args...]    run the source build (Debug), through GameMode when installed
#   osu-dev test [FILTER]        run osu.Game.Tests (FILTER: part of a test name, e.g. TestSceneBeatmapCarousel)
#   osu-dev pr NUMBER            check out a ppy/osu pull request (branch pr-NUMBER), then use "run"
#   osu-dev update               back to master and pull
#   osu-dev local-framework on|off
#                                build against ../osu-framework instead of the NuGet package (needs jq)
#   osu-dev ruleset NAME [--scrolling]
#                                start a custom ruleset from ppy's official template
#   osu-dev sr FILE.osu|FOLDER|BEATMAP_ID [-m HD -m DT ...]
#                                star rating from ppy's own difficulty calculator (osu-tools)
#   osu-dev logs [--errors] [-f] logs of the source build (it keeps its own data in osu-development)
#
# Debug builds store everything in ~/.local/share/osu-development, so they never touch your real osu! data.
set -euo pipefail

DEV_DIR="${OSU_DEV_DIR:-$HOME/osu-dev}"
OSU="$DEV_DIR/osu"
HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
c_ok=$'\e[32m'; c_warn=$'\e[33m'; c_err=$'\e[31m'; c_off=$'\e[0m'
[[ -t 1 ]] || { c_ok=; c_warn=; c_err=; c_off=; }
say()  { echo "${c_ok}::${c_off} $*"; }
warn() { echo "${c_warn}warning:${c_off} $*" >&2; }
die()  { echo "${c_err}error:${c_off} $*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

[[ -x "$HOME/.dotnet/dotnet" ]] && ! have dotnet && export PATH="$HOME/.dotnet:$PATH" DOTNET_ROOT="$HOME/.dotnet"

need_repo() { [[ -d "$OSU/.git" ]] || die "no osu! checkout in $OSU: run 'osu-dev setup' first (or set OSU_DEV_DIR)"; }

# The SDK major.minor osu! wants, from its global.json ("10.0.100" -> "10.0").
wanted_sdk() {
    local v
    v="$(grep -o '"version"[^"]*"[^"]*"' "$OSU/global.json" 2>/dev/null | sed 's/.*"\([0-9.]*\)"$/\1/')"
    [[ -n "$v" ]] && echo "${v%.*}" || echo ""
}

check_dotnet() {
    local want; want="$(wanted_sdk)"
    if ! have dotnet; then
        warn "the .NET SDK is not installed${want:+ (osu! needs $want)}"
        echo "   install it with your package manager (dnf install dotnet-sdk-$want / apt install dotnet-sdk-$want),"
        echo "   or per user without root: osu-dev setup --install-dotnet"
        return 1
    fi
    if [[ -n "$want" ]] && ! dotnet --list-sdks | grep -q "^${want}\."; then
        warn "osu! needs .NET SDK $want; installed: $(dotnet --list-sdks | awk '{print $1}' | paste -sd' ')"
        echo "   osu-dev setup --install-dotnet installs $want to ~/.dotnet alongside the system one"
        return 1
    fi
    say ".NET SDK $(dotnet --version) ok"
}

install_dotnet() {
    local want; want="$(wanted_sdk)"; want="${want:-10.0}"
    say "installing .NET SDK $want to ~/.dotnet (no root)"
    local script; script="$(mktemp)"
    curl -fsSL https://dot.net/v1/dotnet-install.sh -o "$script"
    bash "$script" --channel "$want" --install-dir "$HOME/.dotnet"
    rm -f "$script"
    export PATH="$HOME/.dotnet:$PATH" DOTNET_ROOT="$HOME/.dotnet"
    say "done. Add this to ~/.bashrc so other shells find it too:"
    echo '   export DOTNET_ROOT="$HOME/.dotnet" PATH="$HOME/.dotnet:$PATH"'
}

clone() {  # clone REPO into $DEV_DIR/NAME unless it's there
    local name="$1"
    if [[ -d "$DEV_DIR/$name/.git" ]]; then say "$name already cloned"; return; fi
    git clone --filter=blob:none "https://github.com/ppy/$name.git" "$DEV_DIR/$name"
}

cmd_setup() {
    have git || die "git is required"
    mkdir -p "$DEV_DIR"
    clone osu
    local a dotnet_install=no
    for a in "$@"; do
        case "$a" in
            --framework) clone osu-framework ;;
            --tools) clone osu-tools ;;
            --install-dotnet) dotnet_install=yes ;;
            *) die "unknown option $a" ;;
        esac
    done
    if ! check_dotnet; then
        [[ $dotnet_install == yes ]] && install_dotnet && check_dotnet || true
    fi
    say "ready: osu-dev build, then osu-dev run"
}

cmd_build() { need_repo; check_dotnet || exit 1; (cd "$OSU" && dotnet build osu.Desktop -c "${1:-Debug}"); }

cmd_run() {
    need_repo; check_dotnet || exit 1
    local pre=()
    have gamemoderun && pre=(gamemoderun)
    say "data folder for this build: ~/.local/share/osu-development"
    cd "$OSU" && exec "${pre[@]}" dotnet run --project osu.Desktop -c Debug -- "$@"
}

cmd_test() {
    need_repo; check_dotnet || exit 1
    local args=(test osu.Game.Tests)
    [[ -n "${1:-}" ]] && args+=(--filter "FullyQualifiedName~$1")
    cd "$OSU" && dotnet "${args[@]}"
}

cmd_pr() {
    need_repo
    [[ "${1:-}" =~ ^[0-9]+$ ]] || die "usage: osu-dev pr NUMBER"
    cd "$OSU"
    git diff --quiet && git diff --cached --quiet || die "you have uncommitted changes in $OSU; commit or stash them first"
    git fetch origin "pull/$1/head:pr-$1" --force
    git checkout "pr-$1"
    say "on pr-$1. Run it with: osu-dev run   (back to normal: osu-dev update)"
}

cmd_update() {
    need_repo; cd "$OSU"
    git diff --quiet && git diff --cached --quiet || die "you have uncommitted changes in $OSU; commit or stash them first"
    git checkout master && git pull --ff-only
}

LOCAL_FW_FILES=(osu.Game/osu.Game.csproj osu.Android.props osu.iOS.props osu.sln osu.Desktop.slnf osu.Android.slnf osu.iOS.slnf)

cmd_local_framework() {
    need_repo
    case "${1:-}" in
        on)
            [[ -d "$DEV_DIR/osu-framework/.git" ]] || die "no osu-framework next to osu: osu-dev setup --framework"
            have jq || die "UseLocalFramework.sh needs jq (dnf/apt install jq)"
            (cd "$OSU" && ./UseLocalFramework.sh)
            say "osu! now builds against $DEV_DIR/osu-framework" ;;
        off)
            (cd "$OSU" && git checkout -- "${LOCAL_FW_FILES[@]}")
            say "back to the NuGet osu-framework" ;;
        *) die "usage: osu-dev local-framework on|off" ;;
    esac
}

cmd_ruleset() {
    have dotnet || die "the .NET SDK is needed: osu-dev setup --install-dotnet"
    local name="${1:-}" template=ruleset
    [[ -n "$name" ]] || die "usage: osu-dev ruleset NAME [--scrolling]"
    [[ "${2:-}" == --scrolling ]] && template=ruleset-scrolling
    dotnet new list "$template" 2>/dev/null | grep -q "$template" || dotnet new install ppy.osu.Game.Templates
    mkdir -p "$DEV_DIR/rulesets"
    (cd "$DEV_DIR/rulesets" && dotnet new "$template" -n "osu.Game.Rulesets.$name" -o "osu.Game.Rulesets.$name")
    say "created $DEV_DIR/rulesets/osu.Game.Rulesets.$name"
    echo "   build it, then copy bin/Debug/*/osu.Game.Rulesets.$name.dll into your osu! data folder's rulesets/ to play it"
}

cmd_sr() {
    [[ -d "$DEV_DIR/osu-tools/.git" ]] || die "osu-tools isn't cloned: osu-dev setup --tools"
    check_dotnet || exit 1
    [[ -n "${1:-}" ]] || die "usage: osu-dev sr FILE.osu|FOLDER|BEATMAP_ID [-m HD ...]"
    local target="$1"; shift
    [[ -e "$target" ]] && target="$(readlink -f "$target")"
    cd "$DEV_DIR/osu-tools/PerformanceCalculator" && dotnet run -- difficulty "$target" "$@"
}

cmd_logs() {
    local py="$HERE/osu_report.py"
    [[ -f "$py" ]] || die "osu_report.py not found next to $0"
    exec python3 "$py" logs --dev "$@"
}

case "${1:-help}" in
    setup) shift; cmd_setup "$@" ;;
    build) shift; cmd_build "$@" ;;
    run) shift; cmd_run "$@" ;;
    test) shift; cmd_test "$@" ;;
    pr) shift; cmd_pr "$@" ;;
    update) cmd_update ;;
    local-framework) shift; cmd_local_framework "$@" ;;
    ruleset) shift; cmd_ruleset "$@" ;;
    sr) shift; cmd_sr "$@" ;;
    logs) shift; cmd_logs "$@" ;;
    -h|--help|help) sed -n '2,/^set -e/p' "$0" | sed '$d; s/^# \{0,1\}//' ;;
    *) die "unknown command $1 (osu-dev help)" ;;
esac
