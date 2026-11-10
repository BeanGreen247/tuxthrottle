#!/usr/bin/env bash
#
# TEMPORARY: removes what the Python edition of TuxThrottle left on this machine, so the C++ edition can be
# installed on a clean slate (sudo ./install.sh afterwards). Delete this script once the Python edition is gone.
#
#   sudo ./uninstall-python.sh --dry-run      show what would be removed
#   sudo ./uninstall-python.sh                remove the Python app files, the pip package and stale launchers
#   sudo ./uninstall-python.sh --packages     ...and also ask before removing the distro Python packages it pulled in
#
# Never touched: the C++ binaries, your tweaks and their services, ~/.config/tuxthrottle, the python/ folder of this repo.
set -u

DRY=0 PKGS=0
for a in "$@"; do
    case "$a" in
        --dry-run) DRY=1 ;;
        --packages) PKGS=1 ;;
        -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
        *) echo "unknown option: $a" >&2; exit 1 ;;
    esac
done
c_ok()   { printf '\033[32m  ✓\033[0m %s\n' "$*"; }
c_info() { printf '\033[34m  →\033[0m %s\n' "$*"; }
c_warn() { printf '\033[33m  !\033[0m %s\n' "$*"; }
act()    { if (( DRY )); then c_info "[dry run] $*"; else c_info "$*"; eval "$@" >/dev/null 2>&1 || true; fi; }
(( DRY )) || [[ $EUID -eq 0 ]] || { echo "Run with sudo (or use --dry-run)" >&2; exit 1; }

LIB=/opt/tuxthrottle
echo; c_info "Removing the Python edition of TuxThrottle$( (( DRY )) && echo ' (dry run)')"; echo

# ---- 1. running Python processes of the tool (exact script paths only, never a name match)
for p in /proc/[0-9]*; do
    pid=${p#/proc/}; [[ $pid == $$ ]] && continue
    mapfile -d '' -t a < "$p/cmdline" 2>/dev/null || continue
    (( ${#a[@]} > 1 )) && [[ ${a[0]##*/} == python3* ]] || continue
    case "${a[1]:-}" in
        $LIB/*.py|/usr/local/bin/tuxthrottle-hotkey-listener.py|*/tuxthrottle/python/*.py|*/tuxthrottle-git/python/*.py) act "kill $pid   # ${a[*]:1:2}" ;;
    esac
done

# ---- 2. Python files in the install tree and dangling launchers
shopt -s nullglob
for f in $LIB/*.py $LIB/tests $LIB/pyproject.toml $LIB/tasks; do [[ -e $f ]] && act "rm -rf '$f'"; done
for l in /usr/local/bin/*.py; do
    [[ -L $l && ( ! -e $l || $(readlink "$l") == $LIB/* ) ]] && act "rm -f '$l'"
done
act "find $LIB -name __pycache__ -type d -prune -exec rm -rf {} +"
for u in $(getent passwd | awk -F: '$3>=1000 && $3<65000 {print $6}'); do
    for f in "$u/.local/share/applications/tuxthrottle.desktop" "$u/.config/autostart/tuxthrottle-tray.desktop"; do
        [[ -f $f ]] && grep -qE '^Exec=.*(python|\.py)' "$f" && act "rm -f '$f'   # launched the Python app"
    done
done

# ---- 3. the pip package the Python GUI needed (system-wide, with its ttkb / ttkbootstrap scripts)
if python3 -m pip show ttkbootstrap >/dev/null 2>&1; then
    act "python3 -m pip uninstall -y --break-system-packages ttkbootstrap"
fi
for f in /usr/local/bin/ttkbootstrap /usr/local/bin/ttkb; do [[ -e $f ]] && act "rm -f '$f'"; done

# ---- 4. distro packages the Python installer added (only on request, always confirmed: other programs may use them)
if (( PKGS )); then
    list=$(rpm -qa --qf '%{NAME}\n' 2>/dev/null | grep -xE 'python3-(tkinter|evdev|pillow-tk|pyside6|textual|rich)' | tr '\n' ' ')
    if [[ -z $list ]]; then c_ok "no Python helper packages installed"
    elif (( DRY )); then c_info "[dry run] would ask to remove: $list"
    else
        c_warn "these were installed for the Python edition; other software may depend on them: $list"
        read -r -p "  remove them with dnf (dnf shows what else would go)? [y/N] " ans
        [[ $ans == [yY]* ]] && dnf remove $list
    fi
fi

echo; c_ok "done. Now install the C++ edition:  sudo bash install.sh"
c_info "unchanged: C++ binaries, tweaks/services, ~/.config/tuxthrottle, python/ in the repo"
