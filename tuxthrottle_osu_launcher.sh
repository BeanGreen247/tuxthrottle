#!/usr/bin/env bash
# osu-lazer-launcher: run osu!lazer (AppImage) on Linux at maximum performance.
# Shipped with TuxThrottle (Setup Games → osu!lazer); "install" copies it to ~/.local/bin/osu-lazer-launcher.
#
# Usage:
#   tuxthrottle_osu_launcher.sh install [PATH/TO/osu.AppImage | --download]
#       Save the AppImage location, add "osu!" to the Games menu and the desktop, install the launcher to ~/.local/bin.
#   osu-lazer-launcher [run] [osu args...]
#       Launch osu! with every tweak below, then restore the system when osu! exits.
#   osu-lazer-launcher doctor        Show what was detected and what will be applied.
#   osu-lazer-launcher measure-dpi   Measure a mouse's hardware DPI (needs evtest + sudo).
#   osu-lazer-launcher uninstall     Remove everything "install" added (the AppImage is kept).
#
# What "run" does:
#   * always runs through Feral GameMode (gamemoderun); refuses to start without it
#   * power profile -> performance while playing (power-profiles-daemon / tuned-ppd), restored afterwards
#   * GPU: stays on the GPU driving the screen by default. On most laptops the screen is wired to the
#     integrated GPU, so offloading osu! to the dedicated GPU adds a frame copy (more latency) and can
#     cause rendering glitches. USE_DGPU=yes forces the dedicated GPU (useful with a MUX switch/external monitor).
#   * driver-level vsync off and a 1-frame render queue, so osu!'s own frame limiter decides latency
#   * mouse acceleration off while playing (KDE Plasma Wayland / GNOME), restored afterwards
#   * MangoHud overlay forced off (even if enabled globally), unless SHOW_MANGOHUD=yes (then runs via "mangohud")
#   * persistent NVIDIA GL shader cache + DXVK state cache under SHADER_CACHE_DIR (no recompiles on launch)
#   * NVIDIA threaded GL optimisations off (osu!framework already threads its own draw/update; the driver
#     worker thread adds a queued frame)
#   * low-latency audio: PipeWire quantum pinned to AUDIO_QUANTUM for osu! (default 1024 = ~21 ms at 48 kHz),
#     plus a small pipewire-alsa buffer (PIPEWIRE_ALSA, ALSA_PERIOD_FRAMES x ALSA_PERIODS)
#
# Config: ~/.config/osu-lazer-launcher/config (created by "install", plain KEY=value lines).
set -euo pipefail

APP_ID="osu-lazer"
CONF_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/osu-lazer-launcher"
CONF_FILE="$CONF_DIR/config"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
BIN="$HOME/.local/bin/osu-lazer-launcher"
DESKTOP_FILE="$DATA_HOME/applications/$APP_ID.desktop"
ICON_FILE="$DATA_HOME/icons/hicolor/256x256/apps/$APP_ID.png"
DOWNLOAD_URL="https://github.com/ppy/osu/releases/latest/download/osu.AppImage"

# Defaults, overridable in the config file or the environment.
OSU_APPIMAGE="${OSU_APPIMAGE:-}"
USE_DGPU="${USE_DGPU:-no}"                   # no | yes | auto (see header)
FLAT_MOUSE="${FLAT_MOUSE:-yes}"              # yes | no
PERFORMANCE_PROFILE="${PERFORMANCE_PROFILE:-yes}"
DISABLE_DRIVER_VSYNC="${DISABLE_DRIVER_VSYNC:-yes}"
SHOW_MANGOHUD="${SHOW_MANGOHUD:-no}"        # no = force the MangoHud overlay off for osu!
SHADER_CACHE_DIR="${SHADER_CACHE_DIR:-$HOME/.cache/osu-lazer-launcher/shader-cache}"
SHADER_CACHE_SIZE="${SHADER_CACHE_SIZE:-120000000000}"
LOW_LATENCY_AUDIO="${LOW_LATENCY_AUDIO:-yes}"  # yes | no
AUDIO_QUANTUM="${AUDIO_QUANTUM:-256}"          # PipeWire frames per period (256 @ 48 kHz = 5.3 ms)
PW_ALSA_TUNE="${PW_ALSA_TUNE:-yes}"            # yes = small pipewire-alsa buffer for osu! (PIPEWIRE_ALSA)
ALSA_PERIOD_FRAMES="${ALSA_PERIOD_FRAMES:-128}"
ALSA_PERIODS="${ALSA_PERIODS:-2}"
# shellcheck disable=SC1090
[[ -f "$CONF_FILE" ]] && . "$CONF_FILE"

c_ok=$'\e[32m'; c_warn=$'\e[33m'; c_err=$'\e[31m'; c_off=$'\e[0m'
[[ -t 1 ]] || { c_ok=; c_warn=; c_err=; c_off=; }
say()  { echo "${c_ok}::${c_off} $*"; }
warn() { echo "${c_warn}warning:${c_off} $*" >&2; }
die()  { echo "${c_err}error:${c_off} $*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

pkg_install_cmd() {
    if have dnf; then echo "sudo dnf install -y $*"
    elif have apt; then echo "sudo apt install -y $*"
    elif have pacman; then echo "sudo pacman -S --needed $*"
    elif have zypper; then echo "sudo zypper install -y $*"
    else echo ""; fi
}

find_appimage() {
    [[ -n "$OSU_APPIMAGE" && -f "$OSU_APPIMAGE" ]] && { echo "$OSU_APPIMAGE"; return; }
    local p
    for p in "$HOME/Applications/osu.AppImage" "$HOME/Applications/osu!.AppImage" "$HOME/.local/bin/osu.AppImage" \
             "$HOME/Downloads/osu.AppImage" "$HOME/osu.AppImage"; do
        [[ -f "$p" ]] && { echo "$p"; return; }
    done
    return 1
}

# ---- GPU ----------------------------------------------------------------------------------
gpu_env=()
detect_gpu() {
    gpu_env=()
    [[ "$USE_DGPU" == no ]] && return
    local gpus; gpus="$(lspci 2>/dev/null | grep -iE 'vga|3d|display' || true)"
    local n; n="$(grep -c . <<<"$gpus" || true)"
    if [[ "$USE_DGPU" == auto && "$n" -lt 2 ]]; then return; fi
    if grep -qi nvidia <<<"$gpus" && [[ -e /proc/driver/nvidia/version ]]; then
        # SDL_VIDEODRIVER=x11: PRIME offload is reliable through XWayland/GLX; native Wayland (EGL) fails to
        # compile osu!'s OpenGL shaders on NVIDIA. Use the OpenGL renderer in osu! with this.
        gpu_env+=(SDL_VIDEODRIVER=x11 __NV_PRIME_RENDER_OFFLOAD=1 __GLX_VENDOR_LIBRARY_NAME=nvidia __VK_LAYER_NV_optimus=NVIDIA_only)
    elif [[ "$n" -ge 2 ]]; then
        gpu_env+=(DRI_PRIME=1)
    fi
}

no_mangohud() {
    [[ "$SHOW_MANGOHUD" == no ]] || return 0
    # Vulkan layer: DISABLE_MANGOHUD=1 blocks the implicit layer; MANGOHUD=0 stops opt-in setups.
    # OpenGL: MangoHud is injected via LD_PRELOAD, so strip it from there.
    export DISABLE_MANGOHUD=1 MANGOHUD=0
    unset MANGOHUD_DLSYM MANGOHUD_CONFIG MANGOHUD_CONFIGFILE
    if [[ -n "${LD_PRELOAD:-}" ]]; then
        LD_PRELOAD="$(tr ': ' '\n\n' <<<"$LD_PRELOAD" | { grep -vi mangohud || true; } | paste -sd: -)"
        [[ -n "$LD_PRELOAD" ]] && export LD_PRELOAD || unset LD_PRELOAD
    fi
}

latency_env() {
    if [[ "$DISABLE_DRIVER_VSYNC" == yes ]]; then
        # NVIDIA and Mesa driver vsync off; NVIDIA: at most 1 pre-rendered frame, no threaded-GL queue.
        printf '%s\n' __GL_SYNC_TO_VBLANK=0 vblank_mode=0 __GL_MaxFramesAllowed=1 __GL_THREADED_OPTIMIZATIONS=0 \
            mesa_glthread=false
    fi
    if [[ "$LOW_LATENCY_AUDIO" == yes ]]; then
        # BASS -> ALSA -> pipewire-alsa honours PIPEWIRE_LATENCY (and pipewire-pulse via PULSE_LATENCY_MSEC).
        local rate; rate="$(pw-metadata -n settings 0 clock.rate 2>/dev/null | grep -oP "value:'\K[0-9]+" || true)"
        rate="${rate:-48000}"
        printf '%s\n' "PIPEWIRE_LATENCY=$AUDIO_QUANTUM/$rate" "PULSE_LATENCY_MSEC=$(( AUDIO_QUANTUM * 1000 / rate + 1 ))"
        if [[ "$PW_ALSA_TUNE" == yes ]]; then
            # ppy/osu-framework#6647: shrink the pipewire-alsa plugin's own buffer (F32 stereo = 8 bytes/frame).
            local pb=$(( ALSA_PERIOD_FRAMES * 8 ))
            echo "PIPEWIRE_ALSA={ alsa.format=F32_LE alsa.channels=2 alsa.rate=$rate alsa.period-bytes=$pb alsa.buffer-bytes=$(( pb * ALSA_PERIODS )) }"
        fi
    fi
}

shader_env() {
    [[ -n "$SHADER_CACHE_DIR" ]] || return 0
    mkdir -p "$SHADER_CACHE_DIR/nv-shader-cache" "$SHADER_CACHE_DIR/dxvk-state-cache" 2>/dev/null || true
    # GL/Vulkan shader disk cache (NVIDIA) + DXVK state cache; the PROTON_* pair only matters if osu! is ever
    # launched through Proton (e.g. added to Steam as a non-Steam game with a compatibility tool forced).
    printf '%s\n' __GL_SHADER_DISK_CACHE=1 "__GL_SHADER_DISK_CACHE_PATH=$SHADER_CACHE_DIR/nv-shader-cache" \
         "__GL_SHADER_DISK_CACHE_SIZE=$SHADER_CACHE_SIZE" __GL_SHADER_DISK_CACHE_SKIP_CLEANUP=1 \
         DXVK_STATE_CACHE=1 "DXVK_STATE_CACHE_PATH=$SHADER_CACHE_DIR/dxvk-state-cache" \
         PROTON_LOG=0 PROTON_USE_NTSYNC=1
}

overlay_cmd=()
set_overlay() {
    overlay_cmd=()
    if [[ "$SHOW_MANGOHUD" == yes ]]; then
        have mangohud && overlay_cmd=(mangohud) || warn "SHOW_MANGOHUD=yes but mangohud is not installed"
    fi
}

# ---- mouse acceleration (restored on exit) --------------------------------------------------
restore_cmds=()
kwin_get() { busctl --user get-property org.kde.KWin "$1" org.kde.KWin.InputDevice "$2" 2>/dev/null | awk '{print $2}'; }
kwin_set() { busctl --user set-property org.kde.KWin "$1" org.kde.KWin.InputDevice "$2" "$3" "$4" 2>/dev/null; }

flatten_mouse() {
    [[ "$FLAT_MOUSE" == yes ]] || return 0
    local changed=0
    if have busctl && busctl --user status org.kde.KWin >/dev/null 2>&1; then
        local dev path old
        for dev in $(busctl --user get-property org.kde.KWin /org/kde/KWin/InputDevice \
                        org.kde.KWin.InputDeviceManager devicesSysNames 2>/dev/null | tr -d '"' | cut -d' ' -f3-); do
            path="/org/kde/KWin/InputDevice/$dev"
            [[ "$(kwin_get "$path" pointer)" == true && "$(kwin_get "$path" touchpad)" != true ]] || continue
            old="$(kwin_get "$path" pointerAccelerationProfileFlat)"
            [[ "$old" == true ]] && continue
            kwin_set "$path" pointerAccelerationProfileFlat b true && {
                restore_cmds+=("kwin_set $path pointerAccelerationProfileFlat b $old"); changed=1; }
        done
    elif have gsettings && gsettings get org.gnome.desktop.peripherals.mouse accel-profile >/dev/null 2>&1; then
        local old; old="$(gsettings get org.gnome.desktop.peripherals.mouse accel-profile)"
        if [[ "$old" != "'flat'" ]]; then
            gsettings set org.gnome.desktop.peripherals.mouse accel-profile flat
            restore_cmds+=("gsettings set org.gnome.desktop.peripherals.mouse accel-profile $old"); changed=1
        fi
    fi
    [[ $changed == 1 ]] && say "mouse acceleration off while playing" || true
}

# ---- power profile (restored on exit) -------------------------------------------------------
performance_profile() {
    [[ "$PERFORMANCE_PROFILE" == yes ]] && have powerprofilesctl || return 0
    local old; old="$(powerprofilesctl get 2>/dev/null)" || return 0
    [[ "$old" == performance ]] && return 0
    if powerprofilesctl set performance 2>/dev/null; then
        restore_cmds+=("powerprofilesctl set $old"); say "power profile: $old -> performance"
    fi
}

restore_all() {
    local c
    for c in "${restore_cmds[@]}"; do eval "$c" || true; done
}

# ---- commands --------------------------------------------------------------------------------
cmd_run() {
    local app; app="$(find_appimage)" || die "osu! AppImage not found. Run: $0 install /path/to/osu.AppImage (or --download)"
    [[ -x "$app" ]] || chmod +x "$app" 2>/dev/null || die "$app is not executable (on NTFS/exFAT, mount with exec permissions)"
    if ! have gamemoderun; then
        local cmd; cmd="$(pkg_install_cmd gamemode)"
        die "GameMode is required but not installed.${cmd:+ Install it with: $cmd}"
    fi
    detect_gpu
    trap restore_all EXIT
    trap 'exit 130' INT TERM
    performance_profile
    flatten_mouse
    no_mangohud
    set_overlay
    local extra_env; mapfile -t extra_env < <(shader_env; latency_env)
    if [[ ${#gpu_env[@]} -gt 0 ]]; then
        say "rendering on dedicated GPU"
        grep -q '^Renderer = Vulkan' "${XDG_DATA_HOME:-$HOME/.local/share}/osu/framework.ini" 2>/dev/null \
            && warn "osu! renderer is Vulkan; OpenGL is the tested setting for the dedicated GPU (Settings > Graphics)"
    fi
    say "starting osu! with GameMode"
    local log_dir="${XDG_CACHE_HOME:-$HOME/.cache}/osu-lazer-launcher" rc=0
    mkdir -p "$log_dir"
    # stderr is kept in last.log so a crash can be recognised (and reported) after exit.
    # shellcheck disable=SC2046
    env "${gpu_env[@]}" "${extra_env[@]}" gamemoderun "${overlay_cmd[@]}" "$app" "$@" 2> >(tee "$log_dir/last.log" >&2) || rc=$?
    # OpenGL shaders failing to compile on the offloaded dGPU: retry once on the default GPU.
    if [[ $rc -ne 0 && ${#gpu_env[@]} -gt 0 ]] && grep -q "PartCompilationFailedException\|failed to compile" "$log_dir/last.log"; then
        warn "osu! could not compile its OpenGL shaders on the dedicated GPU."
        warn "Retrying on the default GPU. Fix: set Renderer to Vulkan in osu! (Settings > Graphics), or USE_DGPU=no in $CONF_FILE"
        rc=0
        # shellcheck disable=SC2046
        env "${extra_env[@]}" gamemoderun "${overlay_cmd[@]}" "$app" "$@" 2> >(tee "$log_dir/last.log" >&2) || rc=$?
    fi
    [[ $rc -eq 0 ]] || warn "osu! exited with code $rc; its error output is in $log_dir/last.log"
    return $rc
}

cmd_doctor() {
    local app; app="$(find_appimage)" && say "AppImage: $app" || warn "AppImage: not found"
    have gamemoderun && say "GameMode: $(gamemoded --version 2>/dev/null | head -1 || echo installed)" \
        || warn "GameMode: missing ($(pkg_install_cmd gamemode))"
    detect_gpu
    say "GPU: $(lspci 2>/dev/null | grep -iE 'vga|3d|display' | sed 's/^[^:]*: //' | paste -sd ';' -)"
    [[ ${#gpu_env[@]} -gt 0 ]] && say "dGPU env: ${gpu_env[*]}" || say "dGPU env: none (single GPU or USE_DGPU=no)"
    say "latency env: $(latency_env | paste -sd " " -)"
    say "shader cache env: $(shader_env | paste -sd " " -)"
    set_overlay; say "overlay: ${overlay_cmd[*]:-none (MangoHud forced off)}"
    have powerprofilesctl && say "power profile now: $(powerprofilesctl get)" || warn "powerprofilesctl not found (profile switch skipped)"
    if busctl --user status org.kde.KWin >/dev/null 2>&1; then say "desktop: KDE Plasma (mouse accel via KWin)"
    elif have gsettings; then say "desktop: GNOME/other (mouse accel via gsettings)"
    else warn "desktop: unknown (mouse accel not changed)"; fi
    say "config: $CONF_FILE"
}

extract_icon() {
    local app="$1" tmp src=""
    tmp="$(mktemp -d)"
    (cd "$tmp" && "$app" --appimage-extract .DirIcon >/dev/null 2>&1 || true
     [[ -L squashfs-root/.DirIcon ]] && "$app" --appimage-extract "$(readlink squashfs-root/.DirIcon)" >/dev/null 2>&1 || true
     "$app" --appimage-extract '*.png' >/dev/null 2>&1 || true)
    [[ -e "$tmp/squashfs-root/.DirIcon" ]] && src="$(readlink -f "$tmp/squashfs-root/.DirIcon")"
    [[ -s "$src" ]] || src="$(find "$tmp/squashfs-root" -maxdepth 1 -name '*.png' -printf '%s %p\n' 2>/dev/null \
                             | sort -rn | head -1 | cut -d' ' -f2-)"
    if [[ -s "$src" ]]; then mkdir -p "$(dirname "$ICON_FILE")"; cp "$src" "$ICON_FILE"; fi
    rm -rf "$tmp"
    [[ -s "$ICON_FILE" ]]
}

cmd_install() {
    local app="${1:-}"
    if [[ "$app" == --download ]]; then
        app="$HOME/Applications/osu.AppImage"; mkdir -p "$(dirname "$app")"
        say "downloading latest osu!lazer to $app"
        curl -fL --progress-bar -o "$app" "$DOWNLOAD_URL" || die "download failed"
    elif [[ -z "$app" ]]; then
        app="$(find_appimage)" || die "pass the AppImage path: $0 install /path/to/osu.AppImage (or --download)"
    fi
    app="$(readlink -f "$app")"; [[ -f "$app" ]] || die "$app does not exist"
    chmod +x "$app" 2>/dev/null || true

    mkdir -p "$CONF_DIR" "$(dirname "$BIN")" "$(dirname "$DESKTOP_FILE")"
    if [[ -f "$CONF_FILE" ]]; then
        sed -i "s|^OSU_APPIMAGE=.*|OSU_APPIMAGE=\"$app\"|" "$CONF_FILE"
    else
        cat > "$CONF_FILE" <<EOF
# osu-lazer-launcher settings
OSU_APPIMAGE="$app"
USE_DGPU=no                # no = built-in GPU (lowest latency on most laptops) | yes | auto
FLAT_MOUSE=yes             # turn mouse acceleration off while playing
PERFORMANCE_PROFILE=yes    # switch power profile to performance while playing
DISABLE_DRIVER_VSYNC=yes   # let osu!'s frame limiter control latency
SHOW_MANGOHUD=no           # no = force the MangoHud overlay off | yes = run through mangohud
SHADER_CACHE_DIR="$HOME/.cache/osu-lazer-launcher/shader-cache"   # NVIDIA GL + DXVK caches
LOW_LATENCY_AUDIO=yes      # pin the PipeWire quantum for osu! (lower audio latency)
AUDIO_QUANTUM=256          # 128 = even lower, raise to 512 if you hear crackling
PW_ALSA_TUNE=yes           # small pipewire-alsa buffer (ALSA_PERIOD_FRAMES x ALSA_PERIODS)
ALSA_PERIOD_FRAMES=128
ALSA_PERIODS=2
EOF
    fi
    # older configs: add any key that is missing, with its current value
    local k
    for k in USE_DGPU FLAT_MOUSE PERFORMANCE_PROFILE DISABLE_DRIVER_VSYNC SHOW_MANGOHUD SHADER_CACHE_DIR \
             LOW_LATENCY_AUDIO AUDIO_QUANTUM PW_ALSA_TUNE ALSA_PERIOD_FRAMES ALSA_PERIODS; do
        grep -q "^$k=" "$CONF_FILE" || echo "$k=\"${!k}\"" >> "$CONF_FILE"
    done
    local self; self="$(readlink -f "$0")"
    if [[ "$self" != "$HOME"/* ]]; then
        # System copy (TuxThrottle in /opt): install a thin wrapper so updates to that copy apply
        # immediately. Settings still come from ~/.config/osu-lazer-launcher/config.
        cat > "$BIN" <<EOF
#!/usr/bin/env bash
# osu-lazer-launcher: thin wrapper installed by TuxThrottle. It runs the system copy below, so a TuxThrottle
# update applies straight away; settings are read from ~/.config/osu-lazer-launcher/config.
LAUNCHER="$self"
[[ -f "\$LAUNCHER" ]] || { echo "osu-lazer-launcher: \$LAUNCHER is missing (TuxThrottle uninstalled?)." >&2; exit 1; }
exec bash "\$LAUNCHER" "\$@"
EOF
        chmod 755 "$BIN"
        say "launcher: thin wrapper -> $self"
    else
        install -m 755 "$self" "$BIN"
    fi

    local icon=applications-games
    extract_icon "$app" && icon="$APP_ID" || warn "no icon found in the AppImage, using the generic games icon"
    cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=osu!
GenericName=Rhythm Game
Comment=osu!lazer (GameMode, performance tweaks)
Exec=$BIN run %U
Icon=$icon
Terminal=false
Categories=Game;
Keywords=osu;lazer;rhythm;
StartupWMClass=osu!
MimeType=x-scheme-handler/osu;application/x-osu-beatmap-archive;application/x-osu-skin-archive;application/x-osu-replay;
EOF
    local desk; desk="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
    if [[ -d "$desk" ]]; then install -m 755 "$DESKTOP_FILE" "$desk/$APP_ID.desktop"; have gio && gio set "$desk/$APP_ID.desktop" metadata::trusted true 2>/dev/null || true; fi
    update-desktop-database "$(dirname "$DESKTOP_FILE")" 2>/dev/null || true
    gtk-update-icon-cache -q "$DATA_HOME/icons/hicolor" 2>/dev/null || true
    kbuildsycoca6 >/dev/null 2>&1 || kbuildsycoca5 >/dev/null 2>&1 || true

    say "installed: menu (Games > osu!), desktop shortcut, launcher $BIN"
    have gamemoderun || warn "GameMode is not installed yet: $(pkg_install_cmd gamemode)"
    [[ ":$PATH:" == *":$HOME/.local/bin:"* ]] || warn "~/.local/bin is not in PATH; the menu entry still works"
}

cmd_uninstall() {
    local desk; desk="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
    rm -f "$DESKTOP_FILE" "$desk/$APP_ID.desktop" "$ICON_FILE" "$BIN"
    rm -rf "$CONF_DIR"
    kbuildsycoca6 >/dev/null 2>&1 || true
    say "removed (the osu! AppImage and your osu! data were not touched)"
}

cmd_measure_dpi() {
    have evtest || die "evtest is missing: $(pkg_install_cmd evtest)"
    local mice=(/dev/input/by-id/*-event-mouse) mouse i
    [[ -e "${mice[0]}" ]] || die "no mouse found in /dev/input/by-id"
    if [[ ${#mice[@]} -eq 1 ]]; then mouse="${mice[0]}"
    else select mouse in "${mice[@]}"; do [[ -n "$mouse" ]] && break; done; fi
    local cm=10.16 runs=3 secs=5 total=0 counts inches
    inches="$(awk -v c=$cm 'BEGIN{print c/2.54}')"
    echo "Each run: press Enter, then within ${secs}s move the mouse exactly $cm cm (4 in) to the RIGHT"
    echo "in one straight stroke. Lift it to come back."
    for ((i = 1; i <= runs; i++)); do
        read -rp "Run $i/$runs, Enter to start... "
        # stdbuf: keep evtest output when timeout stops it. Only rightward counts are summed.
        counts="$(sudo timeout "$secs" stdbuf -oL evtest "$mouse" 2>/dev/null \
            | awk '/\(REL_X\), value/{v=$NF+0; if (v>0) s+=v} END{print s+0}')" || true
        echo "  $counts counts -> $(awk -v c="$counts" -v i="$inches" 'BEGIN{printf "%.0f", c/i}') DPI"
        total=$((total + counts))
    done
    awk -v t="$total" -v r=$runs -v i="$inches" 'BEGIN{printf "average: %.0f DPI (rounded to your mouse'"'"'s nearest DPI step)\n", t/r/i}'
}

case "${1:-run}" in
    run) shift || true; cmd_run "$@" ;;
    install) shift; cmd_install "${1:-}" ;;
    doctor) cmd_doctor ;;
    measure-dpi) cmd_measure_dpi ;;
    uninstall) cmd_uninstall ;;
    -h|--help|help) sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//' ;;
    *) cmd_run "$@" ;;
esac
