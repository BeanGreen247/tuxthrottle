#!/usr/bin/env bash
# Builds the project with several flag sets (all at -O3) and measures each one with the same workloads.
#   scripts/bench-variants.sh [variant ...]      variants: base tune nortti lto tune-lto pgo
# Output: one table row per variant in $OUT (default /tmp/tt-variants.txt). Build trees live in $WORK (default /tmp/tt-variants).
set -u
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK=${WORK:-/tmp/tt-variants}; OUT=${OUT:-/tmp/tt-variants.txt}
TUNE="-freorder-blocks-algorithm=simple -fvect-cost-model=unlimited"      # same as lestrix native/Makefile
declare -A ARGS=(
  [base]=""
  [tune]="$TUNE"
  [nortti]="$TUNE -fno-rtti -fvisibility=hidden"                          # hootedit: -fno-rtti -fvisibility=hidden
  [lto]="-flto=auto"
  [tune-lto]="$TUNE -flto=auto"
  [pgo]="$TUNE"
)
build() {  # $1 variant
  local v=$1 d=$WORK/$1
  rm -rf "$d"
  local extra=""
  case $v in
    lto|tune-lto) extra="-Db_lto=true" ;;
    pgo) extra="-Db_pgo=generate" ;;
  esac
  meson setup "$d" "$SRC" --buildtype release -Doptimization=3 -Dstrip=false -Dcpp_args="${ARGS[$v]}" -Dcpp_link_args="${ARGS[$v]}" $extra >/dev/null
  ninja -C "$d" >/dev/null
}
train() {  # $1 build dir: exercise the code the way the benchmarks and real use do
  local d=$1
  "$d/tuxthrottlectl" dev bench-core >/dev/null
  for p in Dashboard Presets Gaming "Game Tools" Fans "Power & Limits" MangoHud Profiles; do TT_NOVSYNC=1 "$d/tuxthrottle" --no-elevate --size 1024x768 --bench 200 --page "$p" >/dev/null 2>&1 || true; done
  "$d/tuxthrottlectl" status --json >/dev/null; "$d/tuxthrottlectl" tweaks status >/dev/null 2>&1 || true
  "$d/tuxthrottlectl" drives report --json >/dev/null 2>&1 || true
  "$d/tuxthrottlectl" mangohud-games list --json >/dev/null 2>&1 || true
  "$d/tuxthrottlectl" diag --collect /tmp >/dev/null 2>&1 || true
  meson test -C "$d" >/dev/null 2>&1 || true
}
best() { sort -n | head -1; }   # best of N, as the playbook does
measure() {  # $1 variant
  local v=$1 d=$WORK/$1
  local t0 t1; t0=$(date +%s.%N)
  local sz; sz=$(for b in tuxthrottle tuxthrottlectl tuxthrottled tuxthrottle-tray; do strip -o /tmp/tt-strip-$b "$d/$b"; stat -c %s /tmp/tt-strip-$b; done | awk '{s+=$1}END{printf "%.2f", s/1048576}')
  declare -A core
  for i in 1 2 3; do
    while read -r name rest; do :; done < /dev/null
    "$d/tuxthrottlectl" dev bench-core | awk '{n=$NF; print}' > /tmp/tt-core-$i.txt
  done
  core_line() { for i in 1 2 3; do grep "$1" /tmp/tt-core-$i.txt | awk '{print $(NF-1)}'; done | best; }
  local jp lb mh
  jp=$(core_line "json parse"); lb=$(core_line "launchbuild"); mh=$(core_line "mhedit")
  fr() { for i in 1 2 3; do TT_NOVSYNC=1 "$d/tuxthrottle" --no-elevate --size 1024x768 --bench 300 --page "$1" 2>&1 | grep frame_avg | awk '{print $2}'; done | best; }
  local f1 f2 f3 ff
  f1=$(fr Presets); f2=$(fr "Game Tools"); f3=$(fr Gaming)
  ff=$(for i in 1 2 3 4 5; do TT_TIMING=1 timeout 4 "$d/tuxthrottle" --no-elevate --script /dev/null 2>&1 | grep first_frame | awk '{print $2}'; done | sort -n | sed -n 3p)
  printf '%-10s size_mb=%s json=%s launch=%s mhedit=%s frame_presets=%s frame_gametools=%s frame_gaming=%s first_frame=%s\n' "$v" "$sz" "$jp" "$lb" "$mh" "$f1" "$f2" "$f3" "$ff" | tee -a "$OUT"
}
mkdir -p "$WORK"; : > "$OUT"
for v in "${@:-base tune nortti lto tune-lto pgo}"; do
  for vv in $v; do
    s=$(date +%s)
    build "$vv"
    if [ "$vv" = pgo ]; then
      train "$WORK/pgo"
      meson configure "$WORK/pgo" -Db_pgo=use -Dcpp_args="$TUNE -fprofile-correction -Wno-missing-profile -Wno-coverage-mismatch" >/dev/null
      ninja -C "$WORK/pgo" >/dev/null
    fi
    echo "built $vv in $(( $(date +%s) - s )) s" >&2
    measure "$vv"
  done
done
echo done >> "$OUT"
