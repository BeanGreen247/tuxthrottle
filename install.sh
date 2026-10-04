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
    rm -f "$BIN" "$(dirname "$BIN")/tuxthrottlectl" "$(dirname "$BIN")/tuxthrottle-tray" "$DESKTOP"
    for s in "${ICON_SIZES[@]}"; do rm -f "${ICONBASE}/${s}x${s}/apps/${APPID}.png"; done
    rm -f "${ICONBASE}/scalable/apps/${APPID}.svg"
    rm -rf "$LIBDIR"
    refresh_caches
    c_ok "Uninstalled the app. (Tweaks/services applied from inside the tool are left"
    c_ok " as-is - 'sudo ./uninstall.sh --purge' removes those too.)"
}

do_install() {
    [[ -f "$SRC/tuxthrottle.py" ]] || { c_err "run this from the toolkit source dir"; exit 1; }

    # ---- dependencies -------------------------------------------------------
    c_info "Checking dependencies…"
    command -v python3 >/dev/null 2>&1 || { c_err "python3 not found - install it first"; exit 1; }
    if ! command -v dnf >/dev/null 2>&1; then
        c_err "dnf not found. TuxThrottle targets Nobara / Fedora; every tweak and"
        c_err "the dependency install below use dnf. Aborting."
        exit 1
    fi

    # tkinter - the GUI toolkit base
    if ! python3 -c 'import tkinter' 2>/dev/null; then
        c_info "installing python3-tkinter"
        dnf install -y -q python3-tkinter || { c_err "could not install python3-tkinter"; exit 1; }
    fi

    # ttkbootstrap - only via pip (not packaged for Fedora/Nobara). Make sure
    # pip itself is present first, or the install below dies with "No module
    # named pip" on a minimal system.
    if ! python3 -c 'import ttkbootstrap' 2>/dev/null; then
        if ! python3 -m pip --version >/dev/null 2>&1; then
            c_info "installing python3-pip (needed to fetch ttkbootstrap)"
            dnf install -y -q python3-pip || { c_err "could not install python3-pip"; exit 1; }
        fi
        c_info "installing ttkbootstrap (pip, system-wide - it isn't packaged for Fedora/Nobara)"
        python3 -m pip install --break-system-packages --root-user-action=ignore -q ttkbootstrap \
            || python3 -m pip install --root-user-action=ignore -q ttkbootstrap \
            || { c_err "ttkbootstrap install failed - the GUI needs it"; exit 1; }
    fi
    python3 -c 'import ttkbootstrap' 2>/dev/null && c_ok "ttkbootstrap OK" || { c_err "ttkbootstrap still not importable"; exit 1; }

    # textual + rich - the headless/SSH-friendly TUI (tuxthrottle_tui.py).
    # Both are packaged for Fedora/Nobara, unlike ttkbootstrap.
    python3 -c 'import textual, rich' 2>/dev/null \
        || dnf install -y -q python3-textual python3-rich \
        || c_warn "python3-textual/python3-rich not installed - the TUI (tuxthrottle --tui) won't run"

    # optional extras - a failure here is a warning, not a stop
    python3 -c 'import PySide6' 2>/dev/null || dnf install -y -q python3-pyside6 \
        || c_warn "python3-pyside6 not installed - the tray monitor (tray_monitor.py) won't run"
    python3 -c 'import evdev'   2>/dev/null || dnf install -y -q python3-evdev \
        || c_warn "python3-evdev not installed - the G-key HotkeyListener tweak won't run"
    # Pillow (+ its Tk bridge) and the symbol font draw the sidebar icons;
    # without them the icons fall back to plain glyphs
    python3 -c 'import PIL.ImageTk' 2>/dev/null || dnf install -y -q python3-pillow python3-pillow-tk \
        || c_warn "python3-pillow-tk not installed - sidebar icons fall back to plain glyphs"
    [[ -f /usr/share/fonts/google-noto/NotoSansSymbols2-Regular.ttf ]] \
        || dnf install -y -q google-noto-sans-symbols-2-fonts \
        || c_warn "google-noto-sans-symbols-2-fonts not installed - sidebar icons fall back to plain glyphs"
    # Drives tab: SMART data, NTFS repair; cron runs the self-heal / drive-watch tweaks
    command -v smartctl >/dev/null 2>&1 || dnf install -y -q smartmontools \
        || c_warn "smartmontools not installed - the Drives tab shows no SMART data"
    command -v ntfsfix >/dev/null 2>&1 || dnf install -y -q ntfsprogs \
        || c_warn "ntfsprogs not installed - 'Repair NTFS' on the Drives tab won't run"
    command -v crontab >/dev/null 2>&1 || dnf install -y -q cronie \
        || c_warn "cronie not installed - the shader-cache self-heal and drive-watch tweaks won't run"
    # desktop plumbing used at the end of this script (non-fatal if missing)
    command -v desktop-file-validate >/dev/null 2>&1 || command -v update-desktop-database >/dev/null 2>&1 \
        || dnf install -y -q desktop-file-utils 2>/dev/null \
        || c_warn "desktop-file-utils missing - the menu entry still installs, just unvalidated"

    # ---- files ------------------------------------------------------------
    c_info "Installing to ${LIBDIR}"
    rm -rf "$LIBDIR"
    install -d "$LIBDIR"
    if command -v rsync >/dev/null 2>&1; then
        # no -o/-g: the tree must end up owned by root, not by whoever owns
        # the checkout (see the ownership note below)
        rsync -rlpt --exclude='.git' --exclude='.github' --exclude='__pycache__' \
              --exclude='.pytest_cache' --exclude='.ruff_cache' --exclude='.mypy_cache' \
              --exclude='.claude' --exclude='.venv' --exclude='*.pyc' \
              --exclude='tests' --exclude='tasks' \
              --exclude='packaging' --exclude='install.sh' "$SRC"/ "$LIBDIR"/
    else
        cp -R --preserve=mode,timestamps "$SRC"/. "$LIBDIR"/
        rm -rf "$LIBDIR/.git" "$LIBDIR/.github" "$LIBDIR/.pytest_cache" \
               "$LIBDIR/.ruff_cache" "$LIBDIR/.mypy_cache" "$LIBDIR/.claude" "$LIBDIR/.venv" \
               "$LIBDIR/tests" "$LIBDIR/tasks" "$LIBDIR/packaging" "$LIBDIR/install.sh"
    fi
    # osu_lazer_tools is a subtree of the standalone osu-lazer-tools repo. TuxThrottle only ships its osu!
    # performance setup (launcher, latency diag, competitive settings); the player/mapper/dev tools stay in
    # that repo. Keep this list in sync with packaging/tuxthrottle.spec.
    find "$LIBDIR/osu_lazer_tools" -mindepth 1 -maxdepth 1 ! -name osu-lazer-launcher.sh \
        ! -name osu_lazer_diag.py ! -name osu_lazer_settings.py ! -name LICENSE -exec rm -rf {} + 2>/dev/null || true
    find "$LIBDIR" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
    # Ownership and modes. This tree is executed as root (the elevated GUI,
    # the root cron jobs and the helper scripts tweaks install from it), so it
    # must not be writable by anyone but root - a copy that kept the checkout
    # owner would let any process of that user plant code that later runs as
    # root. Everyone may read and run it.
    chown -R root:root "$LIBDIR"
    chmod -R u+rwX,go+rX,go-w "$LIBDIR"
    # stamp the version so the Diagnostics / About page can show it (no .git in
    # /opt). Date-based YY.MM.DD keyed to the last commit day (Xylonic-style):
    # last git commit date when $SRC is a checkout → the committed VERSION file
    # (source tarball / rsync without .git). A stray $SRC/.version is ignored -
    # it is a deploy artefact, not a source of truth.
    _ver=""
    if git -C "$SRC" rev-parse --git-dir >/dev/null 2>&1; then
        _ver="$(git -C "$SRC" log -1 --format=%cd --date=format:%y.%m.%d 2>/dev/null || true)"
    fi
    # the committed VERSION file wins when it is newer than the last commit
    # day (a version bump that is not committed yet)
    _fver="$(tr -d '[:space:]' < "$SRC/VERSION" 2>/dev/null || true)"
    if [[ -z "$_ver" || ( -n "$_fver" && "$_fver" > "$_ver" ) ]]; then _ver="$_fver"; fi
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

    # ---- tray launcher (tuxthrottle-tray) -----------------------------
    TRAY="$(dirname "$BIN")/tuxthrottle-tray"
    cat > "$TRAY" <<EOF
#!/usr/bin/env bash
# TuxThrottle system-tray monitor + quick launcher (unprivileged)
exec /usr/bin/python3 "${LIBDIR}/tray_monitor.py" "\$@"
EOF
    chmod 0755 "$TRAY"
    c_ok "tray launcher: ${TRAY}"

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
Comment=Tweaks, drivers, RGB keyboard and gaming setup for Nobara Linux
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
            3) : ;;  # feature not enabled - nothing to do
            *) c_warn "${tw} service refresh returned rc=${_rc}" ;;
        esac
    done

    # helper scripts that tweaks copied out of this tree: bring the ones that
    # are already installed up to this version (never install new ones here)
    if [[ -f /usr/local/bin/tuxthrottle-wait-mounts && -f "$LIBDIR/tuxthrottle_wait_mounts.sh" ]]; then
        install -m 0755 -o root -g root "$LIBDIR/tuxthrottle_wait_mounts.sh" /usr/local/bin/tuxthrottle-wait-mounts
        c_ok "refreshed /usr/local/bin/tuxthrottle-wait-mounts"
    fi
    if grep -q tuxthrottle-mangohud-gate /usr/local/bin/mangohud 2>/dev/null; then
        if TUXTHROTTLE_ALLOW_ROOT=1 python3 "$LIBDIR/tuxthrottle_mangohud_games.py" gate-script \
                > /usr/local/bin/mangohud.new 2>/dev/null && [[ -s /usr/local/bin/mangohud.new ]]; then
            chown root:root /usr/local/bin/mangohud.new
            chmod 0755 /usr/local/bin/mangohud.new
            mv -f /usr/local/bin/mangohud.new /usr/local/bin/mangohud
            c_ok "refreshed the per-game MangoHud / dedicated-GPU gate (/usr/local/bin/mangohud)"
        else
            rm -f /usr/local/bin/mangohud.new
            c_warn "could not refresh /usr/local/bin/mangohud - press Install on the MangoHud tab"
        fi
    fi
    # a backup left in the autostart folder starts a second Steam at login
    if [[ "$_u" != root ]] && id "$_u" >/dev/null 2>&1; then
        sudo -u "$_u" python3 "$LIBDIR/tuxthrottle_steamperf.py" sweep-autostart 2>/dev/null \
            | grep -q '^moved ' && c_ok "moved a stray Steam autostart backup out of ~/.config/autostart"
    fi

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
