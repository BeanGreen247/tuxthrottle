#!/usr/bin/env bash
#
# System-wide installer for TuxThrottle (Nobara Linux).
#
#   sudo ./install.sh            # install for all users, add to the KDE menu
#   sudo ./install.sh --uninstall
#
# Installs to /opt/tuxthrottle, a launcher at /usr/local/bin/tuxthrottle,
# a .desktop entry in /usr/share/applications (so every user can search for it
# in KDE), and the icon into the hicolor theme.
#
set -euo pipefail

APPID="tuxthrottle"
LIBDIR="/opt/${APPID}"
BIN="/usr/local/bin/${APPID}"
DESKTOP="/usr/share/applications/${APPID}.desktop"
ICONBASE="/usr/share/icons/hicolor"
SRC="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"

c_ok()   { printf '\033[32m  ✓\033[0m %s\n' "$*"; }
c_info() { printf '\033[34m  →\033[0m %s\n' "$*"; }
c_warn() { printf '\033[33m  !\033[0m %s\n' "$*"; }
c_err()  { printf '\033[31m  ✗\033[0m %s\n' "$*" >&2; }

[[ $EUID -eq 0 ]] || { c_err "Run with sudo: sudo $0 ${*:-}"; exit 1; }

ICON_SIZES=(16 24 32 48 64 128 256 512)

refresh_caches() {
    update-desktop-database /usr/share/applications >/dev/null 2>&1 || true
    gtk-update-icon-cache -q -t -f "$ICONBASE" >/dev/null 2>&1 || true
    # make it show up in KDE search without a re-login
    local u="${SUDO_USER:-}"
    if [[ -n "$u" ]] && command -v kbuildsycoca6 >/dev/null 2>&1; then
        sudo -u "$u" env DISPLAY="${DISPLAY:-:0}" kbuildsycoca6 --noincremental >/dev/null 2>&1 || true
    fi
}

do_uninstall() {
    c_info "Removing ${APPID}…"
    rm -f "$BIN" "$(dirname "$BIN")/tuxthrottlectl" "$DESKTOP"
    for s in "${ICON_SIZES[@]}"; do rm -f "${ICONBASE}/${s}x${s}/apps/${APPID}.png"; done
    rm -f "${ICONBASE}/scalable/apps/${APPID}.svg"
    rm -rf "$LIBDIR"
    refresh_caches
    c_ok "Uninstalled the app. (Tweaks/services applied from inside the tool are left"
    c_ok " as-is — 'sudo ./uninstall.sh --purge' removes those too.)"
}

do_install() {
    [[ -f "$SRC/tuxthrottle.py" ]] || { c_err "run this from the toolkit source dir"; exit 1; }

    # ---- dependencies -------------------------------------------------------
    c_info "Checking dependencies…"
    command -v python3 >/dev/null 2>&1 || { c_err "python3 not found — install it first"; exit 1; }
    if ! command -v dnf >/dev/null 2>&1; then
        c_err "dnf not found. TuxThrottle targets Nobara / Fedora; every tweak and"
        c_err "the dependency install below use dnf. Aborting."
        exit 1
    fi

    # tkinter — the GUI toolkit base
    if ! python3 -c 'import tkinter' 2>/dev/null; then
        c_info "installing python3-tkinter"
        dnf install -y -q python3-tkinter || { c_err "could not install python3-tkinter"; exit 1; }
    fi

    # ttkbootstrap — only via pip (not packaged for Fedora/Nobara). Make sure
    # pip itself is present first, or the install below dies with "No module
    # named pip" on a minimal system.
    if ! python3 -c 'import ttkbootstrap' 2>/dev/null; then
        if ! python3 -m pip --version >/dev/null 2>&1; then
            c_info "installing python3-pip (needed to fetch ttkbootstrap)"
            dnf install -y -q python3-pip || { c_err "could not install python3-pip"; exit 1; }
        fi
        c_info "installing ttkbootstrap (pip, system-wide — it isn't packaged for Fedora/Nobara)"
        python3 -m pip install --break-system-packages --root-user-action=ignore -q ttkbootstrap \
            || python3 -m pip install --root-user-action=ignore -q ttkbootstrap \
            || { c_err "ttkbootstrap install failed — the GUI needs it"; exit 1; }
    fi
    python3 -c 'import ttkbootstrap' 2>/dev/null && c_ok "ttkbootstrap OK" || { c_err "ttkbootstrap still not importable"; exit 1; }

    # optional extras — a failure here is a warning, not a stop
    python3 -c 'import PySide6' 2>/dev/null || dnf install -y -q python3-pyside6 \
        || c_warn "python3-pyside6 not installed — the tray monitor (tray_monitor.py) won't run"
    python3 -c 'import evdev'   2>/dev/null || dnf install -y -q python3-evdev \
        || c_warn "python3-evdev not installed — the G-key HotkeyListener tweak won't run"
    # desktop plumbing used at the end of this script (non-fatal if missing)
    command -v desktop-file-validate >/dev/null 2>&1 || command -v update-desktop-database >/dev/null 2>&1 \
        || dnf install -y -q desktop-file-utils 2>/dev/null \
        || c_warn "desktop-file-utils missing — the menu entry still installs, just unvalidated"

    # ---- files ------------------------------------------------------------
    c_info "Installing to ${LIBDIR}"
    rm -rf "$LIBDIR"
    install -d "$LIBDIR"
    if command -v rsync >/dev/null 2>&1; then
        rsync -a --exclude='.git' --exclude='.github' --exclude='__pycache__' \
              --exclude='.pytest_cache' --exclude='tests' --exclude='tasks' \
              --exclude='install.sh' "$SRC"/ "$LIBDIR"/
    else
        cp -a "$SRC"/. "$LIBDIR"/
        rm -rf "$LIBDIR/.git" "$LIBDIR/.github" "$LIBDIR/.pytest_cache" \
               "$LIBDIR/tests" "$LIBDIR/tasks" "$LIBDIR/install.sh"
    fi
    find "$LIBDIR" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
    chmod -R a+rX "$LIBDIR"
    # stamp the version so the Diagnostics / About page can show it (no .git in
    # /opt). Always re-derive: `git describe` if $SRC is a repo, else fall back
    # to a .version the source tree carries (rsync'd from a dev machine).
    _ver=""
    if git -C "$SRC" rev-parse --git-dir >/dev/null 2>&1; then
        _ver="$(git -C "$SRC" describe --tags --always --dirty 2>/dev/null \
                || git -C "$SRC" rev-parse --short HEAD 2>/dev/null || true)"
    fi
    [[ -z "$_ver" && -s "$SRC/.version" ]] && _ver="$(cat "$SRC/.version")"
    [[ -n "$_ver" ]] && printf '%s\n' "$_ver" > "$LIBDIR/.version"
    [[ -s "$LIBDIR/.version" ]] && c_ok "version $(cat "$LIBDIR/.version")"
    c_ok "copied $(find "$LIBDIR" -type f | wc -l) files"

    cat > "$BIN" <<EOF
#!/usr/bin/env bash
# TuxThrottle launcher (self-elevates via pkexec/sudo)
exec /usr/bin/python3 "${LIBDIR}/tuxthrottle.py" "\$@"
EOF
    chmod 0755 "$BIN"
    c_ok "launcher: ${BIN}"

    # ---- headless CLI (tuxthrottlectl) ---------------------------------
    CTL="$(dirname "$BIN")/tuxthrottlectl"
    cat > "$CTL" <<EOF
#!/usr/bin/env bash
exec /usr/bin/python3 "${LIBDIR}/tuxthrottlectl.py" "\$@"
EOF
    chmod 0755 "$CTL"
    c_ok "CLI: ${CTL}"

    # ---- icon -----------------------------------------------------------
    for s in "${ICON_SIZES[@]}"; do
        if [[ -f "$LIBDIR/assets/icon-${s}.png" ]]; then
            install -Dm644 "$LIBDIR/assets/icon-${s}.png" "${ICONBASE}/${s}x${s}/apps/${APPID}.png"
        fi
    done
    [[ -f "$LIBDIR/assets/icon.svg" ]] && install -Dm644 "$LIBDIR/assets/icon.svg" "${ICONBASE}/scalable/apps/${APPID}.svg"
    c_ok "icon installed into ${ICONBASE}"

    # ---- .desktop -----------------------------------------------------
    cat > "$DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Name=TuxThrottle
GenericName=Hardware & Gaming Tweaks
Comment=Tweaks, drivers, RGB keyboard and gaming setup for the Dell G15 5515 on Nobara
Exec=${APPID}
TryExec=${APPID}
Icon=${APPID}
Terminal=false
Categories=Settings;HardwareSettings;
Keywords=tuxthrottle;tux;throttle;dell;g15;rgb;gamemode;performance;nvidia;tweak;keyboard;backlight;
StartupNotify=true
EOF
    chmod 0644 "$DESKTOP"
    desktop-file-validate "$DESKTOP" >/dev/null 2>&1 && c_ok "desktop entry: ${DESKTOP}" \
        || c_warn "desktop entry written but desktop-file-validate flagged it"

    # ---- refresh service files for already-enabled tweaks --------------
    # install.sh does NOT turn features on, but if the keyboard-backlight or
    # cpu-perf tuxthrottle-* service is already installed, re-run its apply so
    # the /usr/local/bin scripts + unit files match this version.
    # apply_tweak.py --only-if-present exits 3 (no-op) when it isn't enabled.
    local _u="${SUDO_USER:-$(logname 2>/dev/null || echo root)}"
    for tw in KbdBacklightFix CpuMaxPerformance; do
        local _rc=0
        python3 "$LIBDIR/apply_tweak.py" "$tw" --only-if-present \
            --toolkit-dir "$LIBDIR" --user "$_u" >/dev/null 2>&1 || _rc=$?
        case "$_rc" in
            0) c_ok "refreshed service files: ${tw}" ;;
            3) : ;;  # feature not enabled — nothing to do
            *) c_warn "${tw} service refresh returned rc=${_rc}" ;;
        esac
    done

    refresh_caches
    echo
    c_ok "Installed. Launch it from the KDE menu ('TuxThrottle') or run: ${APPID}"
    c_ok "All users on this system can now find it."
}

case "${1:-}" in
    --uninstall|-u|uninstall) do_uninstall ;;
    ""|--install|-i|install)  do_install ;;
    *) c_err "unknown option: $1"; echo "usage: sudo $0 [--install|--uninstall]"; exit 1 ;;
esac
