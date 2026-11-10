#!/usr/bin/env bash
# Post-install sanity check for TuxThrottle (C++ build). Read-only. Run on the target machine: sudo ./verify-install.sh
P=0 F=0
ok(){ printf '  \033[32mPASS\033[0m %s\n' "$*"; P=$((P+1)); }
no(){ printf '  \033[31mFAIL\033[0m %s\n' "$*"; F=$((F+1)); }
hdr(){ printf '\n\033[36m== %s ==\033[0m\n' "$*"; }

hdr "no legacy dellg15 residue"
LEG=$(find /opt /usr/local/bin /usr/lib/systemd /etc /usr/share/applications /usr/share/icons/hicolor \
        -iname '*dellg15*' ! -name 'purge-legacy-dellg15.sh' 2>/dev/null)
[ -z "$LEG" ] && ok "no dellg15-* paths on system" || { no "dellg15-* paths still present:"; echo "$LEG" | sed 's/^/      /'; }
[ -e "$HOME/.config/dellg15-toolkit" ] && no "~/.config/dellg15-toolkit still exists" || ok "~/.config/dellg15-toolkit gone"

hdr "installed files"
for f in /usr/local/bin/tuxthrottle /usr/local/bin/tuxthrottlectl /usr/local/bin/tuxthrottle-tray /usr/local/bin/tuxthrottled \
         /opt/tuxthrottle/config/tweaks.json /opt/tuxthrottle/config/pkgmap.json /opt/tuxthrottle/models/g15-5515.json \
         /usr/share/applications/tuxthrottle.desktop; do
    [ -e "$f" ] && ok "exists $f" || no "missing $f"
done
for b in tuxthrottle tuxthrottlectl tuxthrottle-tray tuxthrottled; do
    [ -x "/usr/local/bin/$b" ] && ok "$b is executable" || no "$b not executable"
done
file /usr/local/bin/tuxthrottle 2>/dev/null | grep -q ELF && ok "tuxthrottle is a native binary" || no "tuxthrottle is not an ELF binary"
ICON=$(ls /usr/share/icons/hicolor/256x256/apps/tuxthrottle.png 2>/dev/null)
[ -n "$ICON" ] && ok "hicolor icon installed" || no "hicolor tuxthrottle icon missing"
grep -q '^StartupWMClass=tuxthrottle$' /usr/share/applications/tuxthrottle.desktop && ok "desktop entry has StartupWMClass (taskbar icon matches)" || no "desktop entry lacks StartupWMClass=tuxthrottle"
N=$(ls /usr/share/icons/hicolor/*/apps/tuxthrottle.png 2>/dev/null | wc -l); [ "$N" -ge 5 ] && ok "$N icon sizes installed" || no "only $N icon sizes installed"
grep -q '^Name=TuxThrottle$' /usr/share/applications/tuxthrottle.desktop && ok "desktop entry Name=TuxThrottle" || no "desktop entry Name wrong"
desktop-file-validate /usr/share/applications/tuxthrottle.desktop 2>/dev/null && ok "desktop entry validates" || no "desktop-file-validate failed"
V=$(cat /opt/tuxthrottle/.version 2>/dev/null); [ -n "$V" ] && ok ".version stamped: $V" || no ".version missing"

hdr "no Python runtime needed"
if ldd /usr/local/bin/tuxthrottle 2>/dev/null | grep -qi python; then no "tuxthrottle links against Python"; else ok "tuxthrottle does not link Python"; fi
for b in tuxthrottlectl tuxthrottled tuxthrottle-tray; do ldd /usr/local/bin/$b 2>/dev/null | grep -qi python && no "$b links against Python"; done
ok "binaries checked for a Python dependency"

hdr "CLI"
tuxthrottle --version >/dev/null 2>&1 && ok "'tuxthrottle --version' runs" || no "'tuxthrottle --version' failed"
tuxthrottlectl status --json >/tmp/tt_status.json 2>&1 && ok "'tuxthrottlectl status --json' exit 0" || no "'tuxthrottlectl status --json' failed"
grep -q '"cpu"' /tmp/tt_status.json && ok "status JSON has a cpu block" || no "status JSON looks empty"
tuxthrottle --report >/tmp/tt_report.txt 2>&1 && ok "'tuxthrottle --report' exit 0" || no "'tuxthrottle --report' failed (see /tmp/tt_report.txt)"
grep -q 'applied=' /tmp/tt_report.txt && ok "--report produced a status table" || no "--report output looks empty"
tuxthrottlectl collect-model 2>/dev/null | grep -q '"_todo"' && ok "'tuxthrottlectl collect-model' emits a scaffold" || no "'tuxthrottlectl collect-model' failed"
tuxthrottlectl daemon status >/dev/null 2>&1; rc=$?
{ [ "$rc" = 0 ] || [ "$rc" = 1 ]; } && ok "'tuxthrottlectl daemon status' runs (rc=$rc)" || no "'tuxthrottlectl daemon status' crashed (rc=$rc)"
tuxthrottlectl diag --collect /tmp >/dev/null 2>&1 && ok "hardware bundle collector runs" || no "hardware bundle collector failed"
tuxthrottlectl dev subst 'python3 {TOOLKIT_DIR}/tuxthrottle_drivehealth.py report' 2>/dev/null | grep -q 'python3' \
    && no "legacy Python helper reference not rewritten" || ok "legacy Python helper references are rewritten"

hdr "clients"
for f in /opt/tuxthrottle/clients/waybar/tuxthrottle-waybar /opt/tuxthrottle/clients/mangohud/tuxthrottle-mangohud; do
    [ -x "$f" ] && ok "exists $f" || no "missing or not executable $f"
done
/opt/tuxthrottle/clients/mangohud/tuxthrottle-mangohud 2>/dev/null | grep -q '^TT ' && ok "MangoHud line prints" || no "MangoHud line failed"
/opt/tuxthrottle/clients/waybar/tuxthrottle-waybar 2>/dev/null | grep -q '"text"' && ok "waybar JSON prints" || no "waybar module failed"

hdr "polkit"
if [ -f /usr/share/polkit-1/actions/org.tuxthrottle.policy ]; then
    pkaction --action-id org.tuxthrottle.manage >/dev/null 2>&1 && ok "polkit: org.tuxthrottle.manage registered" || no "polkit action installed but not loaded"
else
    ok "polkit action not installed (PolkitTuxthrottlectl tweak is opt-in)"
fi

printf '\n%d passed, %d failed\n' "$P" "$F"
[ "$F" = 0 ]
