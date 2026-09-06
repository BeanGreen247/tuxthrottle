#!/usr/bin/env bash
#
# Uninstall TuxThrottle (Nobara Linux).
#
# By default this removes **only the tool** — the /opt install, the launcher,
# the icon, the menu entry and your per-user toolkit config. Everything the
# tool's *tweaks* set up (the RGB-keyboard boot service, Game-Mode / MangoHud
# helper scripts, the G-key listener, the passwordless-sudo rule, kernel
# cmdline args, fstab entries) is left running, because those work fine
# without the GUI. Apps it installed (Steam, Lutris, …) are never touched.
#
#   sudo ./uninstall.sh            # remove the tool only  (same spirit as install.sh --uninstall)
#   sudo ./uninstall.sh --purge    # ALSO remove every tweak's system bits (services, scripts, sudoers…)
#   sudo ./uninstall.sh --purge --grub    #   ...and strip the kernel-cmdline tweaks (reboot after)
#   sudo ./uninstall.sh --purge --fstab   #   ...and revert the /etc/fstab btrfs-noatime edit
#   sudo ./uninstall.sh --purge --pip     #   ...and pip-uninstall the system-wide ttkbootstrap
#   sudo ./uninstall.sh --purge --all     #   --purge + --grub + --fstab + --pip

set -u

SRC="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
DO_PURGE=0 DO_GRUB=0 DO_FSTAB=0 DO_PIP=0
for a in "$@"; do
    case "$a" in
        --purge) DO_PURGE=1 ;;
        --grub)  DO_GRUB=1 ;;
        --fstab) DO_FSTAB=1 ;;
        --pip)   DO_PIP=1 ;;
        --all)   DO_PURGE=1 DO_GRUB=1 DO_FSTAB=1 DO_PIP=1 ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
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

run()  { c_info "$*"; "$@" >/dev/null 2>&1 || true; }
urun() { [[ -n "$U" ]] && sudo -u "$U" XDG_RUNTIME_DIR="$URUN" "$@" >/dev/null 2>&1 || true; }

echo
c_info "Uninstalling TuxThrottle${U:+ (user: $U)}$([[ $DO_PURGE -eq 1 ]] && echo '  [--purge]')"
echo

# ---- stop anything the tool has running --------------------------------
pkill -f 'tuxthrottle.py'     2>/dev/null || true
# legacy: pre-single-zone versions ran software keyboard-wave daemons
pkill -f 'tuxthrottle_kbd.py .*-wave' 2>/dev/null || true
[[ -n "$UHOME" && -f "$UHOME/.config/tuxthrottle/fx.pid" ]] && \
    kill "$(cat "$UHOME/.config/tuxthrottle/fx.pid" 2>/dev/null)" 2>/dev/null || true

# ---- the app itself (/opt + launcher + icon + system menu entry) -------
if [[ -x "$SRC/install.sh" ]]; then
    "$SRC/install.sh" --uninstall || true
else
    rm -rf /opt/tuxthrottle /usr/local/bin/tuxthrottle /usr/local/bin/tuxthrottlectl \
           /usr/share/applications/tuxthrottle.desktop
    for s in 16 24 32 48 64 128 256 512; do
        rm -f "/usr/share/icons/hicolor/${s}x${s}/apps/tuxthrottle.png"
    done
    rm -f /usr/share/icons/hicolor/scalable/apps/tuxthrottle.svg
    c_ok "removed /opt/tuxthrottle + launcher + icon + desktop entry"
fi

# ---- per-user toolkit files ------------------------------------------
if [[ -n "$UHOME" ]]; then
    rm -rf "$UHOME/.config/tuxthrottle" \
           "$UHOME/.local/share/applications/tuxthrottle.desktop"
    c_ok "removed per-user config ($UHOME/.config/tuxthrottle) + menu entry"
fi

if [[ $DO_PURGE -eq 0 ]]; then
    echo
    c_ok "Tool removed. Tweaks, services and helper scripts were kept —"
    c_ok "  re-run with --purge to remove those too."
    exit 0
fi

# ======================================================================
#  --purge : also undo everything the tweaks installed system-wide
# ======================================================================
echo
c_info "--purge: removing tweak-installed system components…"

for svc in tuxthrottle-kbd.service tuxthrottle-openrgb.service tuxthrottle-cpu-perf.service; do
    run systemctl disable --now "$svc"
done
urun systemctl --user disable --now tuxthrottle-hotkey.service

# automount: tear down its managed /etc/fstab block + /mnt dirs first
if [[ -x /usr/local/bin/tuxthrottle-automount && -n "$U" ]]; then
    run /usr/local/bin/tuxthrottle-automount --user "$U" disable-all
    c_ok "reverted AutoMountDrives (its managed /etc/fstab block + /mnt dirs)"
fi

rm -f /etc/systemd/system/tuxthrottle-kbd.service \
      /etc/systemd/system/tuxthrottle-openrgb.service \
      /etc/systemd/system/tuxthrottle-cpu-perf.service \
      /usr/lib/systemd/system-sleep/tuxthrottle-kbd \
      /etc/systemd/zram-generator.conf.d/gaming.conf \
      /etc/modprobe.d/tuxthrottle-fan.conf \
      /etc/sudoers.d/tuxthrottle-gamemode-toggle
rm -f /usr/local/bin/tuxthrottle-kbd \
      /usr/local/bin/tuxthrottle-automount \
      /usr/local/bin/tuxthrottle-cpu-perf \
      /usr/local/bin/tuxthrottle-hotkey-listener.py \
      /usr/local/bin/gaming-performance /usr/local/bin/gaming-balanced \
      /usr/local/bin/amdgpu-perf-high /usr/local/bin/amdgpu-perf-auto \
      /usr/local/bin/nvidia-max-perf \
      /usr/local/bin/mangohud-global-on /usr/local/bin/mangohud-global-off
c_ok "removed systemd units, sleep hook, zram/fan drop-ins, sudoers rule, helper scripts"

if [[ -n "$UHOME" ]]; then
    rm -f "$UHOME/.config/systemd/user/tuxthrottle-hotkey.service" \
          "$UHOME/.config/environment.d/mangohud.conf"
    urun systemctl --user daemon-reload
    c_ok "removed per-user hotkey unit + mangohud env drop-in"
fi
run systemctl daemon-reload

# ---- optional: kernel cmdline ------------------------------------
if [[ $DO_GRUB -eq 1 ]] && command -v grubby >/dev/null 2>&1; then
    for args in \
        "processor.max_cstate=1" \
        "idle=nomwait" \
        "clocksource=tsc tsc=reliable" \
        "ipv6.disable=1" \
        "sysrq_always_enabled=1 split_lock_detect=off split_lock_mitigate=0 nowatchdog nosoftlockup timer_migration=0 preempt=full threadirqs ignore_rlimit_data audit=0"
    do
        run grubby --update-kernel=ALL --remove-args="$args"
    done
    c_ok "stripped the toolkit's kernel-cmdline args (reboot to take effect)"
elif [[ $DO_GRUB -eq 0 ]]; then
    c_warn "kernel-cmdline tweaks left in place — add --grub to strip them"
fi

# ---- optional: fstab btrfs-noatime ----------------------------
if [[ $DO_FSTAB -eq 1 ]]; then
    if [[ -f /etc/fstab.tuxthrottle-bak ]]; then
        mv -f /etc/fstab.tuxthrottle-bak /etc/fstab
        mount -o remount / 2>/dev/null || true
        c_ok "restored /etc/fstab from /etc/fstab.tuxthrottle-bak (BtrfsNoatime)"
    else
        c_info "no /etc/fstab.tuxthrottle-bak — BtrfsNoatime wasn't applied"
    fi
elif [[ -f /etc/fstab.tuxthrottle-bak ]]; then
    c_warn "BtrfsNoatime edited /etc/fstab — add --fstab to restore it"
fi

# ---- optional: pip ttkbootstrap -----------------------------
if [[ $DO_PIP -eq 1 ]]; then
    run python3 -m pip uninstall -y --break-system-packages ttkbootstrap
    c_ok "pip-uninstalled ttkbootstrap"
else
    c_warn "ttkbootstrap (pip, system-wide) left installed — add --pip to remove it"
fi

update-desktop-database /usr/share/applications >/dev/null 2>&1 || true
gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor >/dev/null 2>&1 || true

echo
c_ok "Purged. Left alone on purpose: apps installed from the tool (Steam, Lutris, …),"
c_ok "  TLP vs power-profiles-daemon, your 'input' group membership, /etc/sudoers.d/claude-test."
[[ $DO_GRUB -eq 1 ]] && c_warn "Reboot to drop the kernel-cmdline changes."
