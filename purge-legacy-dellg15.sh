#!/usr/bin/env bash
#
# One-shot cleanup of pre-rebrand "dellg15-*" bits left on a machine that had
# the tool installed before it was renamed to TuxThrottle.
#
# The old install put things on disk under the name "dellg15-*" (systemd
# units, /usr/local/bin scripts, sudoers/udev/sysctl drop-ins, ~/.config
# /dellg15-toolkit, menu entry, hicolor icons). The new uninstall.sh only
# knows the "tuxthrottle-*" names, so it can't see these. Run this once.
#
# It only ever removes paths whose name contains "dellg15" — it will not
# touch your fstab data-disk mounts, the Game-Mode helper scripts, or any
# tuxthrottle-* install.
#
#   sudo ./purge-legacy-dellg15.sh                  # remove the legacy bits
#   sudo ./purge-legacy-dellg15.sh --migrate-config # first copy old kbd.json/
#                                                   # state.json to ~/.config/tuxthrottle
#   sudo ./purge-legacy-dellg15.sh --dry-run        # print what it would do
#
set -u

MIGRATE=0 DRY=0
for a in "$@"; do
    case "$a" in
        --migrate-config) MIGRATE=1 ;;
        --dry-run|-n)     DRY=1 ;;
        -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
        *) echo "unknown option: $a" >&2; exit 1 ;;
    esac
done

c_ok()   { printf '\033[32m  ✓\033[0m %s\n' "$*"; }
c_info() { printf '\033[34m  →\033[0m %s\n' "$*"; }
c_warn() { printf '\033[33m  !\033[0m %s\n' "$*"; }
c_err()  { printf '\033[31m  ✗\033[0m %s\n' "$*" >&2; }

[[ $EUID -eq 0 ]] || { c_err "Run with sudo: sudo $0 $*"; exit 1; }

U="${SUDO_USER:-${PKEXEC_USER:-}}"
[[ -z "$U" || "$U" == "root" ]] && U="$(logname 2>/dev/null || echo "")"
if [[ -n "$U" ]]; then
    UHOME="$(getent passwd "$U" | cut -d: -f6)"
    URUN="/run/user/$(id -u "$U" 2>/dev/null || echo 1000)"
else
    c_warn "couldn't determine the desktop user — per-user files won't be cleaned"
    UHOME="" URUN=""
fi

# do() — run a command, or just print it under --dry-run
run() { if [[ $DRY -eq 1 ]]; then printf '   would: %s\n' "$*"; else "$@" >/dev/null 2>&1 || true; fi; }
urun() {
    [[ -n "$U" ]] || return 0
    if [[ $DRY -eq 1 ]]; then printf '   would run: sudo -u %s %s\n' "$U" "$*"
    else sudo -u "$U" XDG_RUNTIME_DIR="$URUN" "$@" >/dev/null 2>&1 || true; fi
}
rmf() {  # rm -f each existing path, report which ones existed
    local p
    for p in "$@"; do
        [[ -e "$p" || -L "$p" ]] || continue
        if [[ $DRY -eq 1 ]]; then printf '   would rm: %s\n' "$p"
        else rm -rf "$p" && c_ok "removed $p"; fi
    done
}

echo
c_info "Purging legacy dellg15-* bits${U:+ (user: $U)}$([[ $DRY -eq 1 ]] && echo '   [dry-run]')"
echo

# ---- put the hardware back to a safe state first ----------------------
if command -v openrgb >/dev/null 2>&1; then
    run openrgb -d "Dell G Series LED Controller" -m Static -c 000000 -b 0
fi
for f in /sys/class/hwmon/hwmon*/fan*_boost; do
    [[ -w "$f" ]] && run sh -c "echo 0 > '$f'"
done

# ---- optional: keep the saved keyboard colours ----------------------
if [[ $MIGRATE -eq 1 && -n "$UHOME" ]]; then
    OLD="$UHOME/.config/dellg15-toolkit"
    NEW="$UHOME/.config/tuxthrottle"
    if [[ -d "$OLD" ]]; then
        if [[ -d "$NEW" ]]; then
            c_warn "$NEW already exists — not overwriting; old files will just be deleted"
        else
            run mkdir -p "$NEW"
            run sh -c "cp -a '$OLD/.' '$NEW/'"
            run chown -R "$U:$U" "$NEW"
            c_ok "migrated $OLD  ->  $NEW"
        fi
    else
        c_info "no $OLD to migrate"
    fi
fi

# ---- system services -------------------------------------------------
for svc in dellg15-kbd.service dellg15-openrgb.service dellg15-cpu-perf.service; do
    if systemctl list-unit-files "$svc" >/dev/null 2>&1 && \
       systemctl cat "$svc" >/dev/null 2>&1; then
        run systemctl disable --now "$svc"
        c_ok "stopped + disabled $svc"
    fi
done
urun systemctl --user disable --now dellg15-hotkey.service

# ---- system files (all names contain "dellg15") ---------------------
rmf /etc/systemd/system/dellg15-kbd.service \
    /etc/systemd/system/dellg15-openrgb.service \
    /etc/systemd/system/dellg15-cpu-perf.service \
    /usr/lib/systemd/system-sleep/dellg15-kbd \
    /usr/local/bin/dellg15-kbd \
    /usr/local/bin/dellg15-cpu-perf \
    /usr/local/bin/dellg15-automount \
    /usr/local/bin/dellg15-hotkey-listener.py \
    /usr/local/bin/dellg15-toolkit \
    /opt/dellg15-toolkit \
    /etc/sudoers.d/dellg15-gamemode-toggle \
    /etc/modprobe.d/dellg15-fan.conf \
    /etc/fstab.dellg15-bak \
    /etc/nobara/automount/.dellg15-managed \
    /usr/share/applications/dellg15-toolkit.desktop

# globbed drop-ins
shopt -s nullglob
rmf /etc/udev/rules.d/*dellg15*.rules \
    /etc/sysctl.d/*dellg15*.conf \
    /etc/security/limits.d/*dellg15*.conf \
    /etc/NetworkManager/conf.d/*dellg15*.conf \
    /usr/share/icons/hicolor/*/apps/dellg15-toolkit.png \
    /usr/share/icons/hicolor/scalable/apps/dellg15-toolkit.svg
shopt -u nullglob

# ---- per-user files ------------------------------------------------
if [[ -n "$UHOME" ]]; then
    rmf "$UHOME/.config/dellg15-toolkit" \
        "$UHOME/.config/systemd/user/dellg15-hotkey.service" \
        "$UHOME/.config/systemd/user/default.target.wants/dellg15-hotkey.service" \
        "$UHOME/.local/share/applications/dellg15-toolkit.desktop"
fi

# ---- refresh caches ----------------------------------------------
run systemctl daemon-reload
urun systemctl --user daemon-reload
run update-desktop-database /usr/share/applications
run gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor

echo
if grep -qs 'dellg15-toolkit AutoMountDrives' /etc/fstab; then
    c_warn "left /etc/fstab AutoMountDrives block intact (it mounts your data disks)."
    c_warn "  re-add it from the new tool if you want, or edit the marker by hand."
fi
c_ok "Legacy dellg15-* cleanup done.$([[ $DRY -eq 1 ]] && echo '  (dry run — nothing changed)')"
echo
