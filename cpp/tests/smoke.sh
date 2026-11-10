#!/usr/bin/env bash
# Launch smoke test: the built GUI and CLI must start, render every page at the minimum size and exit cleanly.
# usage: smoke.sh BUILD_DIR SOURCE_ROOT        (no display needed: SDL dummy video + software renderer)
set -u
B=${1:?build dir}; SRC=${2:?source root}
GUI=$B/tuxthrottle; CTL=$B/tuxthrottlectl
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
export SDL_VIDEODRIVER=dummy TT_RENDER=software TT_NOVSYNC=1 TT_NO_ELEVATE=1
export TUXTHROTTLE_STATE=$T/state.json TUXTHROTTLE_KBD_STATE=$T/kbd.json
fail=0
ok()  { echo "ok   $*"; }
bad() { echo "FAIL $*"; fail=$((fail + 1)); }
run() { ( ulimit -v 8000000; timeout "${TMO:-90}" "$@" ); }

# ---- binaries start and report a version
for b in tuxthrottle tuxthrottlectl; do
    [[ -x $B/$b ]] && ok "$b built" || bad "$b missing"
done
v=$(run "$GUI" --version 2>&1); [[ $v == tuxthrottle\ * ]] && ok "gui --version: $v" || bad "gui --version: $v"

# ---- CLI under a pkexec-like environment (stripped PATH, no display)
s=$(env -i PATH=/usr/bin:/bin HOME="$T" "$CTL" status --json 2>&1); rc=$?
(( rc == 0 )) && ok "ctl status --json (stripped env)" || bad "ctl status --json rc=$rc: ${s:0:200}"
if command -v python3 >/dev/null; then
    python3 -I -c 'import json,sys; d=json.loads(sys.stdin.read()); assert d.get("backends"), "no backends"' <<<"$s" 2>/dev/null \
        && ok "status json is valid and lists backends" || bad "status json invalid / no backends"
fi
n=$(run "$CTL" tweaks list 2>/dev/null | wc -l); (( n >= 100 )) && ok "tweaks list: $n items" || bad "tweaks list only $n lines"
run "$CTL" tweaks report >/dev/null 2>&1; (( $? == 0 )) && ok "tweaks report" || bad "tweaks report failed"
run "$CTL" dev subst 'echo {TT} {USER}' 2>/dev/null | grep -q '{TT}' && bad "placeholder {TT} survived substitution" || ok "substitution expands {TT}"
run "$CTL" kbd apply-saved >/dev/null 2>&1; (( $? == 0 )) && ok "kbd apply-saved with no saved state is a no-op" || bad "kbd apply-saved failed without state"

# ---- shipped desktop entry and installer text
d=$SRC/../assets/tuxthrottle.desktop
grep -q '^Exec=tuxthrottle' "$d" && ok "desktop Exec=tuxthrottle" || bad "desktop Exec wrong"
grep -q '^Icon=tuxthrottle$' "$d" && ok "desktop Icon=tuxthrottle" || bad "desktop Icon wrong"
grep -q '^StartupWMClass=tuxthrottle$' "$d" && ok "desktop StartupWMClass" || bad "desktop StartupWMClass missing"
grep -nE '^Exec=.*(python|\.py)' "$d" "$SRC/install.sh" >/dev/null && bad "an Exec line still launches Python" || ok "no Exec line launches Python"
for s in 32 64 128 256; do [[ -f $SRC/../assets/icon-$s.png ]] || bad "assets/icon-$s.png missing"; done

# ---- every page renders at 1024x768 and the process exits 0
mapfile -t pages < <(run "$GUI" --list-pages 2>/dev/null)
(( ${#pages[@]} >= 25 )) && ok "${#pages[@]} pages registered" || bad "only ${#pages[@]} pages registered"
i=0
for p in "${pages[@]}"; do
    i=$((i + 1)); f=$T/p$i.ppm
    run "$GUI" --page "$p" --shot "$f" --size 1024x768 >"$T/out.txt" 2>&1; rc=$?
    if (( rc != 0 )); then bad "page '$p' exited $rc: $(tail -2 "$T/out.txt" | tr '\n' ' ')"; continue; fi
    sz=$(stat -c %s "$f" 2>/dev/null || echo 0)
    if (( sz < 1024 * 768 * 3 )); then bad "page '$p' wrote a short image ($sz bytes)"; continue; fi
    if command -v python3 >/dev/null; then
        python3 -I - "$f" <<'PY' && ok "page '$p' renders" || bad "page '$p' rendered a blank image"
import sys
d = open(sys.argv[1], "rb").read()[-1024 * 768 * 3:]
sys.exit(0 if len(set(d[0::3 * 997])) > 3 else 1)
PY
    else ok "page '$p' exits cleanly"; fi
done

# ---- scrolled, large and scripted runs must not crash either
run "$GUI" --page Gaming --scroll 900 --shot "$T/s.ppm" --size 1024x768 >/dev/null 2>&1 && ok "scrolled page" || bad "scrolled page crashed"
run "$GUI" --page Dashboard --shot "$T/big.ppm" --size 1920x1080 >/dev/null 2>&1 && ok "1920x1080" || bad "1920x1080 crashed"
cat >"$T/script.txt" <<'S'
wait 2
click 30 90
wait 2
click 30 150
wheel -3
wait 1
key Tab
key Escape
page About
wait 1
page Dashboard
wait 1
shot SHOT
quit
S
sed -i "s#SHOT#$T/script.ppm#" "$T/script.txt"
run "$GUI" --script "$T/script.txt" --size 1024x768 >"$T/out.txt" 2>&1; rc=$?
(( rc == 0 )) && [[ -s $T/script.ppm ]] && ok "scripted click/key/wheel session" || bad "scripted session rc=$rc: $(tail -2 "$T/out.txt" | tr '\n' ' ')"
run "$GUI" --page Dashboard --bench 30 --size 1024x768 >"$T/out.txt" 2>&1; rc=$?
(( rc == 0 )) && ok "30-frame bench loop" || bad "bench loop rc=$rc"

echo
(( fail == 0 )) && echo "smoke: all passed" || echo "smoke: $fail failed"
exit $(( fail > 0 ))
