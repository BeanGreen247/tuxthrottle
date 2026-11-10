#!/usr/bin/env bash
#
# Installer for the C++ TuxThrottle (no Python, no extra runtime libraries beyond SDL2 + FreeType + fontconfig).
#
#   sudo ./cpp/install.sh               # build + install for all users
#   sudo ./cpp/install.sh --no-deps     # skip the build-dependency install step
#   sudo ./cpp/install.sh --no-build    # install an existing ./cpp/build (from `ninja -C cpp/build`)
#   sudo ./cpp/install.sh --uninstall
#
# Binaries go to /usr/local/bin (tuxthrottle, tuxthrottlectl, tuxthrottle-tray, tuxthrottled); the data the tools read
# (config/, models/, assets, shell helpers) goes to /opt/tuxthrottle.
#
set -euo pipefail

APPID="tuxthrottle"
LIBDIR="/opt/${APPID}"
BINDIR="/usr/local/bin"
DESKTOP="/usr/share/applications/${APPID}.desktop"
ICONBASE="/usr/share/icons/hicolor"
CPP="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
SRC="$(cd "$CPP/.." && pwd)"
BUILD="$CPP/build"
ICON_SIZES=(16 24 32 48 64 128 256 512)

c_ok()   { printf '\033[32m  ok\033[0m %s\n' "$*"; }
c_info() { printf '\033[34m  ->\033[0m %s\n' "$*"; }
c_warn() { printf '\033[33m  !\033[0m %s\n' "$*"; }
c_err()  { printf '\033[31m  x\033[0m %s\n' "$*" >&2; }

[[ $EUID -eq 0 ]] || { c_err "Run with sudo: sudo $0 ${*:-}"; exit 1; }

DO_DEPS=1; DO_BUILD=1; ACTION=install
for a in "$@"; do
    case "$a" in
        --no-deps) DO_DEPS=0 ;;
        --no-build) DO_BUILD=0; DO_DEPS=0 ;;
        --uninstall|-u|uninstall) ACTION=uninstall ;;
        --install|-i|install) ACTION=install ;;
        -h|--help) sed -n 2,14p "$0"; exit 0 ;;
        *) c_err "unknown option: $a"; exit 1 ;;
    esac
done

refresh_caches() {
    update-desktop-database /usr/share/applications >/dev/null 2>&1 || true
    gtk-update-icon-cache -q -t -f "$ICONBASE" >/dev/null 2>&1 || true
    local u="${SUDO_USER:-}"
    if [[ -n "$u" ]] && command -v kbuildsycoca6 >/dev/null 2>&1; then
        sudo -u "$u" env DISPLAY="${DISPLAY:-:0}" kbuildsycoca6 --noincremental >/dev/null 2>&1 || true
    fi
}

do_uninstall() {
    c_info "Removing ${APPID}..."
    for b in tuxthrottle tuxthrottlectl tuxthrottle-tray tuxthrottled; do rm -f "$BINDIR/$b"; done
    rm -f "$DESKTOP"
    for s in "${ICON_SIZES[@]}"; do rm -f "${ICONBASE}/${s}x${s}/apps/${APPID}.png"; done
    rm -f "${ICONBASE}/scalable/apps/${APPID}.svg"
    rm -rf "$LIBDIR"
    refresh_caches
    c_ok "Uninstalled the app. Tweaks/services applied from inside the tool are left as they are."
}

install_build_deps() {
    c_info "Installing build dependencies (g++, meson, ninja, SDL2, FreeType, fontconfig)..."
    . /etc/os-release 2>/dev/null || true
    local ids=" ${ID:-} ${ID_LIKE:-} "
    if   command -v rpm-ostree >/dev/null 2>&1 && [[ -e /run/ostree-booted ]]; then
        c_warn "atomic Fedora: build inside a toolbox/distrobox, then run: sudo $0 --no-build"; return 1
    elif [[ $ids == *" fedora "* || $ids == *" rhel "* || $ids == *" nobara "* ]]; then
        dnf install -y -q gcc-c++ meson ninja-build SDL2-devel freetype-devel fontconfig-devel rsms-inter-fonts || return 1
    elif [[ $ids == *" debian "* || $ids == *" ubuntu "* ]]; then
        apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq g++ meson ninja-build libsdl2-dev libfreetype-dev libfontconfig-dev fonts-inter || return 1
    elif [[ $ids == *" arch "* ]]; then
        pacman -S --needed --noconfirm gcc meson ninja sdl2 freetype2 fontconfig inter-font || return 1
    elif [[ $ids == *" gentoo "* ]]; then
        emerge --noreplace dev-build/meson dev-build/ninja media-libs/libsdl2 media-libs/freetype media-libs/fontconfig media-fonts/inter || return 1
    elif [[ $ids == *" suse "* || $ids == *" opensuse "* ]]; then
        zypper --non-interactive install gcc-c++ meson ninja SDL2-devel freetype2-devel fontconfig-devel || return 1
    else
        c_warn "unknown distro - install g++, meson, ninja and the SDL2 / FreeType / fontconfig development packages yourself"
        return 1
    fi
}

do_build() {
    c_info "Building (optimization level 3)..."
    # an existing build tree keeps the options it was created with (a dev tree may be non-LTO): always apply the release options
    if [[ ! -d "$BUILD" ]]; then
        meson setup "$BUILD" "$CPP" --buildtype=release -Doptimization=3 -Db_lto=true -Dstrip=true >/dev/null
    else
        meson configure "$BUILD" -Dbuildtype=release -Doptimization=3 -Db_lto=true -Dstrip=true >/dev/null
    fi
    ninja -C "$BUILD" >/dev/null
    c_ok "built $(ls "$BUILD"/tuxthrottle* 2>/dev/null | wc -l) binaries"
}

do_install() {
    if (( DO_DEPS )); then install_build_deps || c_warn "dependency install did not finish - continuing"; fi
    if (( DO_BUILD )); then do_build; fi
    for b in tuxthrottle tuxthrottlectl tuxthrottle-tray tuxthrottled; do
        [[ -x "$BUILD/$b" ]] || { c_err "$BUILD/$b is missing - build first (ninja -C cpp/build)"; exit 1; }
    done

    c_info "Installing to ${LIBDIR} and ${BINDIR}"
    rm -rf "$LIBDIR"
    install -d "$LIBDIR"
    for d in config models assets clients polkit; do
        [[ -d "$SRC/$d" ]] && cp -R --preserve=mode,timestamps "$SRC/$d" "$LIBDIR/$d"
    done
    # the osu helper scripts stay Python and live in python/ in the source tree
    [[ -d "$SRC/python/osu_lazer_tools" ]] && cp -R --preserve=mode,timestamps "$SRC/python/osu_lazer_tools" "$LIBDIR/osu_lazer_tools"
    for f in tuxthrottle_wait_mounts.sh tuxthrottle_osu_launcher.sh VERSION LICENSE README.md FEATURES.md; do
        [[ -f "$SRC/$f" ]] && install -m 0644 "$SRC/$f" "$LIBDIR/$f"
    done
    chmod 0755 "$LIBDIR"/*.sh 2>/dev/null || true
    # osu_lazer_tools: only the performance setup ships (matches the RPM spec)
    if [[ -d "$LIBDIR/osu_lazer_tools" ]]; then
        find "$LIBDIR/osu_lazer_tools" -mindepth 1 -maxdepth 1 ! -name osu-lazer-launcher.sh ! -name osu_lazer_diag.py \
            ! -name osu_lazer_settings.py ! -name LICENSE -exec rm -rf {} + 2>/dev/null || true
    fi
    find "$LIBDIR" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
    # the tree is run as root (elevated GUI, cron jobs, helper scripts): nobody but root may write it
    chown -R root:root "$LIBDIR"
    chmod -R u+rwX,go+rX,go-w "$LIBDIR"
    local ver=""
    if git -C "$SRC" rev-parse --git-dir >/dev/null 2>&1; then ver="$(git -C "$SRC" log -1 --format=%cd --date=format:%y.%m.%d 2>/dev/null || true)"; fi
    local fver; fver="$(tr -d '[:space:]' < "$SRC/VERSION" 2>/dev/null || true)"
    if [[ -z "$ver" || ( -n "$fver" && "$fver" > "$ver" ) ]]; then ver="$fver"; fi
    [[ -n "$ver" ]] && printf '%s\n' "$ver" > "$LIBDIR/.version" && c_ok "version $ver"

    for b in tuxthrottle tuxthrottlectl tuxthrottle-tray tuxthrottled; do
        install -m 0755 -o root -g root "$BUILD/$b" "$BINDIR/$b"
    done
    c_ok "binaries: ${BINDIR}/{tuxthrottle,tuxthrottlectl,tuxthrottle-tray,tuxthrottled}"

    for s in "${ICON_SIZES[@]}"; do
        [[ -f "$LIBDIR/assets/icon-${s}.png" ]] && install -Dm644 "$LIBDIR/assets/icon-${s}.png" "${ICONBASE}/${s}x${s}/apps/${APPID}.png"
    done
    n_icons=$(ls "${ICONBASE}"/*/apps/${APPID}.png 2>/dev/null | wc -l)
    if (( n_icons )); then c_ok "icons installed: ${n_icons} sizes in ${ICONBASE}"; else c_warn "no icons were installed (assets/icon-*.png missing) - the menu entry and taskbar will show a generic icon"; fi
    [[ -f "$LIBDIR/assets/icon.svg" ]] && install -Dm644 "$LIBDIR/assets/icon.svg" "${ICONBASE}/scalable/apps/${APPID}.svg"

    cat > "$DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Name=TuxThrottle
GenericName=Hardware & Gaming Tweaks
Comment=Tweaks, drivers, RGB keyboard and gaming setup for Linux
Exec=${APPID}
TryExec=${APPID}
Icon=${APPID}
Terminal=false
Categories=Settings;HardwareSettings;
Keywords=tuxthrottle;tux;throttle;dell;g15;rgb;gamemode;performance;nvidia;tweak;keyboard;backlight;
StartupNotify=true
StartupWMClass=tuxthrottle
EOF
    chmod 0644 "$DESKTOP"
    desktop-file-validate "$DESKTOP" >/dev/null 2>&1 && c_ok "desktop entry: ${DESKTOP}" || c_warn "desktop entry written (validator flagged it)"

    # bring already-enabled service tweaks up to this version (no-op when a tweak isn't enabled)
    local u="${SUDO_USER:-$(logname 2>/dev/null || echo root)}"
    # an older per-user menu entry still launching the Python app shadows the system one and the app seems to do nothing
    for h in $(getent passwd | awk -F: '$3>=1000 && $3<65000 {print $6}'); do
        uf="$h/.local/share/applications/${APPID}.desktop"
        if [[ -f "$uf" ]] && grep -qE '^Exec=.*(python|\.py)' "$uf"; then
            rm -f "$uf"; c_ok "removed stale menu entry that launched the old Python app: $uf"
        fi
    done
    # the tray autostart entry from the Python era launches a file that is gone
    for h in $(getent passwd | awk -F: '$3>=1000 && $3<65000 {print $6}'); do
        af="$h/.config/autostart/tuxthrottle-tray.desktop"
        if [[ -f "$af" ]] && grep -qE '^Exec=.*(python|\.py)' "$af"; then
            sed -i "s#^Exec=.*#Exec=$BINDIR/tuxthrottle-tray#" "$af"; c_ok "tray autostart entry now starts $BINDIR/tuxthrottle-tray ($af)"
        fi
    done
    # re-apply every enabled tweak whose services / cron jobs still call the Python helpers
    "$BINDIR/tuxthrottlectl" tweaks refresh-legacy 2>&1 | sed -n '/^refreshed/p;/FAILED/p' || true
    # the keyboard-backlight service set gained a guard (resume / re-enumeration / server-restart recovery): upgrade an existing install
    if [[ -f /etc/systemd/system/tuxthrottle-kbd.service ]]; then
        "$BINDIR/tuxthrottlectl" apply-tweak KbdBacklightFix --only-if-present --user "$u" 2>&1 | tail -2 || true
    fi
    if [[ -f /usr/local/bin/tuxthrottle-wait-mounts && -f "$LIBDIR/tuxthrottle_wait_mounts.sh" ]]; then
        install -m 0755 -o root -g root "$LIBDIR/tuxthrottle_wait_mounts.sh" /usr/local/bin/tuxthrottle-wait-mounts
    fi
    if grep -q tuxthrottle-mangohud-gate /usr/local/bin/mangohud 2>/dev/null; then
        if "$BINDIR/tuxthrottlectl" mangohud-games gate-script > /usr/local/bin/mangohud.new 2>/dev/null && [[ -s /usr/local/bin/mangohud.new ]]; then
            chmod 0755 /usr/local/bin/mangohud.new && mv -f /usr/local/bin/mangohud.new /usr/local/bin/mangohud
            c_ok "refreshed the per-game MangoHud gate"
        else
            rm -f /usr/local/bin/mangohud.new
        fi
    fi
    if [[ "$u" != root ]] && id "$u" >/dev/null 2>&1; then
        sudo -u "$u" "$BINDIR/tuxthrottlectl" steamperf sweep-autostart 2>/dev/null | grep -q '^moved ' \
            && c_ok "moved a stray Steam autostart backup out of ~/.config/autostart"
    fi
    refresh_caches
    echo
    c_ok "Installed. Launch it from the KDE menu ('TuxThrottle') or run: ${APPID}"
}

case "$ACTION" in
    uninstall) do_uninstall ;;
    install) do_install ;;
esac
