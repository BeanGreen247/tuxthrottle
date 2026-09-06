#!/usr/bin/env bash
# Post-install / post-rebrand sanity check for TuxThrottle. Read-only. Run on the
# target machine (needs sudo for the find + --report + dmesg checks):  sudo ./verify-install.sh
P=0 F=0
ok(){ printf '  \033[32mPASS\033[0m %s\n' "$*"; P=$((P+1)); }
no(){ printf '  \033[31mFAIL\033[0m %s\n' "$*"; F=$((F+1)); }
hdr(){ printf '\n\033[36m== %s ==\033[0m\n' "$*"; }

hdr "no legacy dellg15 residue"
LEG=$(sudo find /opt /usr/local/bin /usr/lib/systemd /etc /usr/share/applications \
        /usr/share/icons/hicolor -iname '*dellg15*' ! -name 'purge-legacy-dellg15.sh' 2>/dev/null)
[ -z "$LEG" ] && ok "no dellg15-* paths on system" || { no "dellg15-* paths still present:"; echo "$LEG" | sed 's/^/      /'; }
U=$(systemctl list-unit-files 'dellg15-*' 2>/dev/null | grep -c dellg15 || true)
[ "$U" = 0 ] && ok "no dellg15 systemd units" || no "$U dellg15 systemd unit(s) remain"
[ -e "$HOME/.config/dellg15-toolkit" ] && no "~/.config/dellg15-toolkit still exists" || ok "~/.config/dellg15-toolkit gone"
[ -e "$HOME/DellG15Toolkit" ] && no "~/DellG15Toolkit checkout still exists" || ok "old ~/DellG15Toolkit checkout gone"

hdr "tuxthrottle install"
for f in /opt/tuxthrottle/tuxthrottle.py /opt/tuxthrottle/tuxthrottle_kbd.py \
         /opt/tuxthrottle/tuxthrottle_automount.py /usr/local/bin/tuxthrottle \
         /usr/share/applications/tuxthrottle.desktop; do
    [ -e "$f" ] && ok "exists $f" || no "missing $f"
done
[ -e /opt/tuxthrottle/dellg15_toolkit.py ] && no "old dellg15_toolkit.py in /opt" || ok "no old module names in /opt"
ICON=$(ls /usr/share/icons/hicolor/256x256/apps/tuxthrottle.png 2>/dev/null)
[ -n "$ICON" ] && ok "hicolor icon installed ($ICON)" || no "hicolor tuxthrottle icon missing"
grep -q '^Name=TuxThrottle$' /usr/share/applications/tuxthrottle.desktop \
    && ok "desktop entry Name=TuxThrottle" || no "desktop entry Name wrong"
desktop-file-validate /usr/share/applications/tuxthrottle.desktop 2>/dev/null \
    && ok "desktop entry validates" || no "desktop-file-validate failed"

hdr "launcher / CLI"
head -1 /usr/local/bin/tuxthrottle | grep -q bash && \
    grep -q '/opt/tuxthrottle/tuxthrottle.py' /usr/local/bin/tuxthrottle \
    && ok "launcher points at /opt/tuxthrottle/tuxthrottle.py" || no "launcher body wrong"
V=$(cat /opt/tuxthrottle/.version 2>/dev/null); [ -n "$V" ] && ok ".version stamped: $V" || no ".version missing"
sudo tuxthrottle --report >/tmp/tt_report.txt 2>&1 \
    && ok "'tuxthrottle --report' exit 0" || no "'tuxthrottle --report' failed (see /tmp/tt_report.txt)"
grep -qi 'tuxthrottle\|G15 5515' /tmp/tt_report.txt && ok "--report produced a status table" || no "--report output looks empty"

hdr "python modules"
python3 -c "import sys; sys.path.insert(0,'/opt/tuxthrottle'); import tuxthrottle_kbd, sensors; print(1)" >/dev/null 2>&1 \
    && ok "tuxthrottle_kbd + sensors import" || no "module import error"
python3 /opt/tuxthrottle/tuxthrottle_kbd.py --help >/dev/null 2>&1 \
    && ok "tuxthrottle_kbd CLI responds" || no "tuxthrottle_kbd CLI error"
python3 /opt/tuxthrottle/tuxthrottle_shadercache.py show >/dev/null 2>&1 \
    && ok "tuxthrottle_shadercache CLI responds" || no "tuxthrottle_shadercache CLI error"
python3 -c "import sys; sys.path.insert(0,'/opt/tuxthrottle'); import sensors, json; \
[getattr(sensors,f) for f in ('battery_health_info','battery_charge_mode','nvidia_powerd_status','amd_pstate_mode','vrr_status')]; \
d=json.load(open('/opt/tuxthrottle/config/tweaks.json')); \
assert all(k in d for k in ('NvidiaShaderCache','SchedExtGaming','SplitLockMitigateOff','KwinAllowTearing')), 'gaming tweaks missing'" >/dev/null 2>&1 \
    && ok "gaming helpers + tweaks present" || no "gaming helpers/tweaks missing"
grep -rIl 'dellg15' /opt/tuxthrottle --include='*.py' >/dev/null 2>&1 \
    && no "'dellg15' still in /opt/tuxthrottle/*.py" || ok "no 'dellg15' token in installed .py"

hdr "tier-3 modules"
for f in /opt/tuxthrottle/tuxthrottle_control.py /opt/tuxthrottle/tuxthrottle_co_stress.py \
         /opt/tuxthrottle/models/g15-5515.json /opt/tuxthrottle/clients/waybar/tuxthrottle-waybar; do
    [ -e "$f" ] && ok "exists $f" || no "missing $f"
done
python3 -c "import sys; sys.path.insert(0,'/opt/tuxthrottle'); import tuxthrottle_control, tuxthrottle_co_stress, sensors; assert sensors.model_id()=='g15-5515', sensors.model_id(); print(1)" >/dev/null 2>&1 \
    && ok "control + co_stress import; model_id=g15-5515" || no "tier-3 module import / model detect failed"
tuxthrottlectl daemon status >/dev/null 2>&1; rc=$?
[ "$rc" = 0 ] || [ "$rc" = 1 ] && ok "'tuxthrottlectl daemon status' runs (rc=$rc)" || no "'tuxthrottlectl daemon status' crashed (rc=$rc)"
tuxthrottlectl collect-model 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); assert 'fans' in d and '_todo' in d" >/dev/null 2>&1 \
    && ok "'tuxthrottlectl collect-model' emits a valid scaffold" || no "'tuxthrottlectl collect-model' failed"
python3 -c "import sys,os; sys.path.insert(0,'/opt/tuxthrottle'); os.environ['TUXTHROTTLE_MODEL']='_test-fixture'; import sensors; assert sensors.model_id()=='_test-fixture' and sensors._pwm_floor()==90" >/dev/null 2>&1 \
    && ok "TUXTHROTTLE_MODEL override + profile-routed accessors work" || no "TUXTHROTTLE_MODEL override failed"
if [ -f /usr/share/polkit-1/actions/org.tuxthrottle.policy ]; then
    pkaction --action-id org.tuxthrottle.manage >/dev/null 2>&1 \
        && ok "polkit: org.tuxthrottle.manage action registered" \
        || no "PolkitTuxthrottlectl installed but polkitd didn't load the action"
else
    ok "polkit action not installed (PolkitTuxthrottlectl tweak is opt-in)"
fi

hdr "GUI — Report a Bug page"
XAUTHORITY=$(ls -t /run/user/1000/xauth* 2>/dev/null | head -1) DISPLAY=:0 python3 - <<'PY' 2>&1 | sed 's/^/  /'
import sys, time
sys.path.insert(0, "/opt/tuxthrottle")
import tuxthrottle as t
t.self_elevate = lambda: None
import ttkbootstrap as tb
r = tb.Window(themename="darkly"); a = t.ToolkitApp(r)
for _ in range(8): r.update(); time.sleep(0.08)
pages = [(x, getattr(b, "_nav_kind", "normal")) for x, f, b in a.notebook._pages]
last, lastkind = pages[-1]
print("PASS pages built:", len(pages))
print(("PASS" if last == "Report a Bug" else "FAIL"), "last nav item:", last)
print(("PASS" if lastkind == "support" else "FAIL"), "last item kind:", lastkind)
sup = [x for x, k in pages if k == "support"]
print(("PASS" if sup == ["Report a Bug"] else "FAIL"), "exactly one support page:", sup)
diag = [f for x, f, b in a.notebook._pages if x == "Report a Bug"][0]
a.notebook.select(diag)
btn = [b for x, f, b in a.notebook._pages if x == "Report a Bug"][0]
print(("PASS" if btn.cget("style") == "NavSupportActive.TButton" else "FAIL"),
      "active style:", btn.cget("style"))
r.destroy()
PY

hdr "hardware bundle prefix"
B=$(python3 -c "import sys;sys.path.insert(0,'/opt/tuxthrottle');import tuxthrottle as t;print(t.collect_hw_bundle())" 2>/tmp/tt_bundle_err.txt)
if [ -n "$B" ] && [ -f "$B" ]; then
    case "$(basename "$B")" in
        tuxthrottle-hwdump-*.tar.gz) ok "bundle: $(basename "$B") ($(du -h "$B"|cut -f1))"; rm -f "$B" ;;
        *) no "bundle name not tuxthrottle-hwdump-*: $(basename "$B")" ;;
    esac
else
    no "collect_hw_bundle failed (see /tmp/tt_bundle_err.txt)"
fi

printf '\n\033[1m%d passed, %d failed\033[0m\n' "$P" "$F"
[ "$F" = 0 ]
