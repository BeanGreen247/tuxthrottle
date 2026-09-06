# TuxThrottle — Features

A checkbox-driven GUI + system-tray monitor + G-key listener that applies
**hardware-specific tweaks, drivers and gaming setup** to **one** machine: the
**Dell G15 5515 Ryzen Edition** (Ryzen 7 5800H + RTX 3050 Ti Mobile) running
**Nobara Linux** (Fedora 43 base, KDE Plasma 6 / Wayland).

Every `check` / `apply` command is written against that board — it is **not** a
general-purpose distro tool. The parts that are model-agnostic (the VRAM
budget, the KDE tweaks) say so below.

The GUI self-elevates once via `pkexec` → `sudo`. Live changes apply
immediately; the matching **tweak** on the Power / GPU tab makes a lever
survive a reboot. Nothing is turned on for you — tweaks are opt-in and every
one is reversible.

Config lives under `~/.config/tuxthrottle/` (`tdp.json`, `co.json`,
`nvpl.json`, `nvclk.json`, `battery.json`, `powerd.json`, `vram.json`,
`kbd.json`, `profiles/`, `snapshots/`, `state.json`, …).

---

## Contents

- [Dashboard](#dashboard)
- [Keyboard (RGB backlight)](#keyboard-rgb-backlight)
- [Fans](#fans)
- [Power & Limits](#power--limits)
- [Battery](#battery)
- [VRAM](#vram)
- [Profiles](#profiles)
- [Presets](#presets)
- [Updates](#updates)
- [Setup Games](#setup-games)
- [Game Tools](#game-tools)
- [Tweaks & Apps](#tweaks--apps)
- [System tray](#system-tray)
- [tuxthrottled (background daemon)](#tuxthrottled-background-daemon)
- [tuxthrottlectl (CLI)](#tuxthrottlectl-cli)
- [Panel clients](#panel-clients)
- [Report a Bug](#report-a-bug)
- [Install / uninstall](#install--uninstall)

---

## Dashboard

Eight live **ring gauges** in a 2×4 grid, updated a few times a second:

| Gauge | Source |
|---|---|
| CPU temp / clock / power | `k10temp` hwmon, `/proc` freq, RAPL (`/sys/class/powercap`) |
| iGPU clock | `amdgpu` `pp_dpm_sclk` |
| dGPU temp / clock / util / power | `nvidia-smi` (skipped while the dGPU is runtime-suspended) |

Below the gauges: rolling **sparkline history** per metric, a **session CSV
log** (written for the life of the window), and a **Game Mode** toggle
(the `performance` `platform_profile`, which flips firmware "G-Mode").

The page is **built lazily** on first entry and its poll thread idles while
another tab is showing, so it costs nothing when you're not looking at it.

> If CPU wattage reads `n/a`, the kernel's RAPL side-channel mitigation is
> keeping the counters root-only. Apply **Fix CPU Power-Reading Permissions
> (RAPL)** on the Power tab (usually a no-op on Nobara 43, which already ships
> them world-readable).

---

## Keyboard (RGB backlight)

The 5515's Alienware **AW-ELC** keyboard has no kernel driver and no SMBIOS
tokens; it is driven entirely through the **OpenRGB** CLI. It is a **single
controllable zone** — one solid colour for the whole keyboard, a brightness
level, or the firmware **Spectrum Cycle** animation. There is no per-key
colour and no gradient (the firmware ignores zone-scoped writes).

**Prerequisites:** OpenRGB installed (Game Tools → apps), the keyboard
backlight **enabled in BIOS setup** (F2 → Keyboard Backlight — off by
default), and — for the colour to come back after a reboot/resume — the
**KbdBacklightFix** tweak.

### Controls

- **Brightness** slider (0–100). The controller reports brightness inverted;
  the slider hides that — `100` is full, `0` is off.
- **Whole keyboard** — a colour swatch + `Pick colour…` + `Apply colour`, and
  quick preset buttons (White / Red / Green / Blue / Cyan / Magenta / Amber).
- **Effect** — `Spectrum Cycle` (the only firmware animation that actually
  works on this board) with a speed slider, and `Solid colour` to leave it.
- **Turn backlight off** and **↻ Reset backlight (unfreeze)** — the latter
  restarts the OpenRGB SDK server, which is the usual fix when the backlight
  "freezes" after many mode changes.

### Desktop-accent sync

Two **mutually-exclusive** toggles under *Desktop accent colour*:

- **Keyboard follows the desktop accent colour** — the keyboard takes the
  current Plasma accent now, and re-reads the **live** accent on every
  re-assert (login, resume, tray start). Change your accent later and the
  keyboard follows. Saved as `"mode": "accent"` in `kbd.json`.
- **Desktop accent follows the keyboard colour** — every colour you set on the
  keyboard here is also written into Plasma's accent
  (`kdeglobals [General] AccentColor`, with accent-from-wallpaper turned off),
  and applied to the running session with
  `plasma-apply-colorscheme --accent-color "#RRGGBB"` (no scheme argument, so
  light/dark mode is never touched). Saved as `"push_accent": true`.
  Disabled while Spectrum Cycle is running — there's no single colour to copy.

Turning either on turns the other off. TuxThrottle reads the accent once at
its own startup, so its own theming only changes when you relaunch it; the
rest of Plasma repaints immediately.

---

## Fans

`hwmon/alienware_wmi` exposes `fan{1,2}_input` (RPM, read-only) and
`fan{1,2}_boost` 0–255 (an **additive** AWCC-style boost — it can only *add*
airflow on top of the firmware curve, never slow a fan below it, which makes
it the safe lever). `hwmon/dell_smm` has real `pwm{1,2}` manual control,
floored at `PWM_FLOOR` (77) so a fan can never fully stop.

- **Thermal profile** — `balanced` / `performance` / `custom`
  (`/sys/firmware/acpi/platform_profile`).
- **Fan boost** — per-fan 0–255 sliders + presets.
- **Manual PWM** (behind a warning) — takes the EC off its curve entirely.
  Watch temps; hit *Restore automatic* when done.
- **Custom fan curve** — a **10-point** temperature→boost table with
  **Silent / Balanced / Aggressive** presets and a *Linear fill* button
  (place two endpoints, interpolate the rest). Written to `powerd.json`; the
  **FanCurveDaemon** tweak makes `tuxthrottled` run it as a closed loop with
  cool-down hysteresis and auto-restore on stop.

---

## Power & Limits

The Linux equivalent of ThrottleStop / the Armoury Crate tuning sliders.

### CPU power limits (ryzenadj)

STAPM (sustained) / Fast (burst) / Slow (medium-window) sliders in watts,
plus presets. Applies live via `ryzenadj`. The Cezanne SMU floors STAPM near
the `slow` limit, so presets keep `STAPM ≥ slow`. Stock envelope on this
board is **65 / 65 / 54 W**. State → `tdp.json`.

> Needs `ryzenadj`. Install the **CPU TDP control (ryzenadj)** tweak — it
> builds/installs ryzenadj, a `/usr/local/bin/tuxthrottle-tdp` re-apply
> script, a boot + resume unit and a sudoers rule so the limits persist.

### Curve Optimizer (Ryzen undervolt)

Shown only when `/proc/cpuinfo` says `AuthenticAMD`. A `0 … −40` all-core
offset slider. **Apply & stress-test 5 min** runs `tuxthrottle_co_stress.py`
which: snapshots state, writes `co.json`, arms a watchdog, applies the offset
(`ryzenadj --set-coall`), runs `stress-ng --cpu 0` + a GPU load for N minutes
while polling `dmesg` for MCE/WHEA/hardware-error lines, and **auto-reverts to
0 on any fault**. A clean run leaves it applied; **Keep (confirm)** marks it
`confirmed:true` so the boot service re-applies it — an unconfirmed offset
that hung the box never comes back. **Revert** sets CO back to 0.

### NVIDIA power limit

A `-pl` slider **or** a "firmware-locked" note — the RTX 3050 Ti Mobile's
board power limit is locked (`nvidia-smi --query-gpu=power.limit` → `[N/A]`),
so the slider is hidden and you get the note instead. Kept for laptops where
`-pl` works. State → `nvpl.json`; **NVIDIA Power Limit** tweak persists it.

### NVIDIA graphics-clock lock

`nvidia-smi --lock-gpu-clocks` min/max MHz — the GPU-clock lever that **still
works** when `-pl` is firmware-locked, so you can under-clock the dGPU for
heat/battery. State → `nvclk.json`; **NVIDIA GPU clock-lock persistence**
tweak installs `/usr/local/bin/tuxthrottle-nvclk` + a boot service + a sleep
hook + sudoers.

### Hybrid graphics (EnvyControl)

Radios for `integrated` / `hybrid` / `nvidia`. Needs a logout (or reboot).

### Battery charge limit

The 5515 has **no** `charge_control_end_threshold`, so this uses Dell
firmware via `libsmbios`: `smbios-battery-ctl --set-custom-charge-interval
<start> <end>` (50–100, gap ≥ 5), stored in **NVRAM** — it persists with no
service. Also a **Charging speed** standard/express radio (the firmware can't
read the mode back but *setting* it works). Install **Dell battery threshold
(libsmbios)**.

### Panel refresh rate

The 5515 panel is **144 Hz**; dropping to 60 on battery is a real power lever
(`kscreen-doctor`, resolution preserved). State → `powerd.json`
`autoswitch.refresh_ac` / `refresh_battery`.

### AC ↔ battery auto-switch

Comboboxes: on an AC-plug transition the daemon applies a `platform_profile`
+ ryzenadj TDP preset + panel refresh for AC vs battery.

### Thermal-event alerts

The **ThermalWatcher** in `tuxthrottled` (`thermal_notify` block in
`powerd.json`) sends a desktop notification + a `THERMAL-EVENT` log line for:
sustained tctl ≥ Tjmax, a **0-RPM fan while hot** (the G15 firmware bug), or
a `performance`/`custom` profile on battery below a % threshold. With
`stalled_fan_recover: true` a stalled fan also kicks `platform_profile →
performance` (the only thing that revives a fan stuck by that firmware bug)
until the fans turn again.

---

## Battery

Read from `power_supply` sysfs — no root, no extra deps.

- **Wear %** — design capacity vs full-charge capacity, colour-coded
  green / amber / red.
- **Charge cycles**, **chemistry**.
- A live **Now** card (polled every 4 s): charge %, charging/discharging
  state, power flow in watts, and **time to empty / full** estimated from the
  current rate + voltage.
- The same charge-limit control as Power & Limits (kept in sync).
- An informational **Adaptive Sync (VRR)** line.

---

## VRAM

**Model-agnostic** — every tier is plain KWin/Plasma KConfig, no vendor
calls, so it degrades gracefully on AMD / NVIDIA / Intel. On this laptop the
Ryzen iGPU shares a tiny **~512 MiB** UMA carveout with system RAM that the
KDE desktop routinely fills (spilling to slower GTT), while the RTX 3050 Ti's
4 GiB is kept free for DaVinci Resolve / games / 3D.

### Live VRAM usage

A bar per detected GPU (used / total MiB + %), the AMD **GTT** overflow, and
a **top-consumers** list built from `/proc/<pid>/fdinfo` `drm-*-vram` lines +
`nvidia-smi`. GPU names and bars are read live from the hardware, never
hard-coded. Polled only while the tab is visible.

### VRAM budget tier

| Tier | What it does |
|---|---|
| **Regular** | Full desktop. Restores the exact KWin/Plasma values captured the first time you left this tier (`~/.config/tuxthrottle/vram-baseline.json`) — not necessarily stock Plasma defaults. |
| **Medium** | Blur + background-contrast off, quicker animations (`AnimationDurationFactor` 0.25), bilinear textures (`GLTextureFilter=1`), fewer hidden-window pixmaps (`HiddenPreviews=5`). Barely visible; frees tens of MiB. |
| **Extreme** | Everything in Medium plus: solid-colour wallpaper (drops a full-screen texture per screen), no Overview / Present-Windows / Desktop-Grid, nearest-neighbour textures (`GLTextureFilter=0`), `HiddenPreviews=4`, on-screen (Maliit) keyboard off. Restarts the panel in the background; the keyboard + some savings fully land at next login. |

A whole tier is applied as **one** batched `bash -lc` session script (≈ 40
`kwriteconfig6`/`qdbus` calls collapsed into one), so it completes in under a
second.

### Free VRAM now

Evicts driver buffers to system RAM (they page back in on demand):

- **AMD** — *reads* `/sys/kernel/debug/dri/<N>/amdgpu_evict_{vram,gtt}`
  (current kernels evict on read; older ones took a write of `1`).
- **Intel** — writes `0xf` to `i915_gem_drop_caches` / `xe_drop_caches`.
- **Restart compositor** (separate button, with a confirm) — `systemctl
  --user restart plasma-kwin_wayland` to release KWin's own allocations.
  Windows stay open; ~1 s black flash.

Shows before/after per GPU.

### Which GPU renders the desktop

`KWIN_DRM_DEVICES` written to `~/.config/plasma-workspace/env/09-tuxthrottle-gpu.sh`:

- **Automatic** — remove the file, let KWin choose (already the iGPU here).
- **Integrated … (pin)** — pin the compositor to the integrated GPU's real
  `/dev/dri/cardN`, `[ -e … ]`-guarded so a vanished node is skipped rather
  than crashing the session. Carries a `Ctrl+Alt+F3 → rm …` recovery comment.
- **Discrete …** — offered only where a hardware MUX exists. Refused on this
  (muxless) laptop: the panel is wired to the iGPU, so KWin can't render the
  whole desktop on the RTX — use per-app PRIME offload instead.

Takes effect after logout.

### Discrete GPU idle power

A live checkbox flips the dGPU's PCI `power/control` between `auto` (allow
D3-cold when idle → frees its VRAM + ~5 W) and `on` (pin awake). The
**NVIDIA runtime power management** tweak (`NVreg_DynamicPowerManagement=0x02`
in `/etc/modprobe.d/` + initramfs rebuild, needs a reboot) makes it stick.

---

## Profiles

A **profile** is a named snapshot of the whole power surface:
`platform_profile`, ryzenadj TDP, battery limit, NVIDIA power limit + clock
lock, fan curve, panel refresh, AC/battery auto-switch, optional hybrid-GPU
mode, keyboard state.

- **Capture** / **Apply** / **Delete** named profiles
  (`~/.config/tuxthrottle/profiles/`).
- Automatic **snapshots** (`snapshots/`, pruned to 20) before **every** apply
  or rollback, with per-row and "latest" **rollback**.
- A **per-game auto-profile map** — map an executable name to a profile;
  `tuxthrottled` scans `/proc` and applies it on launch, reverts on exit.
- A **time-of-day schedule** — rules `{from, to, apply, days}` where `apply`
  is a bundle (Quiet / Balanced / Performance) or a saved profile; `outside`
  covers the gaps. Overnight wrap handled. Written to `powerd.json`.

`active_state.json` is what the **StateResume** sleep hook re-applies after a
resume.

---

## Presets

One-click curated bundles of tweaks **and** app installs (e.g. "Safe
Baseline", "Competitive Gaming"). A snapshot is taken first. The Presets tab
also has a global **★ Apply all recommendations** button that applies every
tweak the developer marked `recommended` across all categories (and folds in
FanCurveDaemon if it isn't enabled yet). Each tweak/app category page also
grows a per-section **★ Apply section recommendations** button when it still
has an un-applied recommendation.

---

## Updates

Wraps **`nobara-sync`** (check-updates / `cli` / install-updates /
install-fixups / repair) and adds:

- Per-manager sections — **dnf**, **Flatpak**, **fwupd** (refresh / list /
  apply, with a reboot flag on firmware).
- **Fix Fedora GPG keys** — imports the Fedora 44 key that Nobara ships on
  disk un-imported (some `.fc44` packages are signed with it), which
  otherwise aborts updates with a GPG error.
- An **Update everything** button.

The pending-update count uses `dnf -q --cacheonly check-update` (a plain
`check-update` can take 3+ minutes on this box's mirrors), so it's shown "as
of last metadata sync" with the age of the repo metadata, and self-corrects
after any check/update.

---

## Setup Games

A real tab-strip, one page per game (from `config/games.json`). Each game is
an ordered list of **step cards**: a per-step `check` sets a status pill
(done ✓ / to do / manual / optional) and the step is either

- a **▶ Run step** button that streams its script to the log, or
- a **manual** step with a *Mark done* toggle and an optional *⧉ Copy
  command*.

A **▶▶ Run all N automatic steps** header button chains the runnable steps,
skipping ones already done. `{USER}` / `{TOOLKIT_DIR}` / `{APPID}` are
substituted. GTA V Online is first and mirrors the README's "Route A".

---

## Game Tools

Any-game Steam / Proton helpers (split out of Setup Games so that tab is just
walkthroughs). Everything here runs as your user.

### Proton prefix & save-file tools

- **Prefix relocation** — a Proton prefix on an NTFS/exFAT drive can't work
  (`:` in `dosdevices/c:` → `OSError 22`, the game won't start).
  `tuxthrottle_prefix_relocate.py` moves `compatdata/<appid>` onto the native
  Steam library and symlinks it back. `--scan` lists all at-risk prefixes;
  `--all` migrates every one. Copy-not-delete; refuses root or a running
  Steam.
- **Save-game vault** — `tuxthrottle_savevault.py` `list` / `export` /
  `import` a save vault (a folder on a drive that is **not** the OS
  filesystem, enforced by an `st_dev` check), laid out
  `<vault>/<appid>/{Documents,Saved Games,AppData/…}`. Vault path persists in
  `~/.config/tuxthrottle/saves_vault`.

### Shader / pipeline cache storage

`tuxthrottle_shadercache.py` — one configurable folder for **every**
shader/pipeline cache (`mesa-shader-cache` / `dxvk-state-cache` /
`nv-shader-cache` / `steam-shadercache`). Config
`~/.config/tuxthrottle/shadercache.json` `{dir, max_size_gb}`.

- **Save location** — persists the dir + size and re-points any existing
  Steam `steamapps/shadercache` symlink at it (without this, moving the
  folder left the link dangling → **Steam "disk write error"** on every
  download/verify).
- **Link Steam cache** — moves each Steam library's `steamapps/shadercache`
  into the shared folder and symlinks it back (repairs a broken link).
- **Check links** — reports each library's link as `ok` / `broken` /
  `unlinked` (auto-runs when the box opens).
- **Show sizes** — three live `du` totals (total / Steam / the rest).
- **Clean** — empty a subdir (optional; caches self-rebuild).

### Launch-options builder

Tick what you want and copy an `[env] [wrappers] %command%` string for a
game's Steam launch options:

- MangoHud, `gamemoderun`, `gamescope` (+ W/H/fps), PRIME offload
- persistent NVIDIA + Mesa + DXVK shader caches (paths + sizes from
  `shadercache.json`)
- NVIDIA threaded optimizations, `RADV_PERFTEST=gpl`, `DXVK_ASYNC`,
  disable-vsync + threaded-GL (AMD), `PROTON_LOG=0`
- **anti-cheat safe** — `MANGOHUD=0 DISABLE_VKBASALT=1
  VK_LOADER_LAYERS_DISABLE=~implicit~` (also suppresses the mangohud
  wrapper), for BattlEye/EAC titles.

Example output:

```
DXVK_ASYNC=1 MANGOHUD=1 __NV_PRIME_RENDER_OFFLOAD=1 __GLX_VENDOR_LIBRARY_NAME=nvidia gamemoderun mangohud %command%
```

### MangoHud overlay editor

Edits `~/.config/MangoHud/MangoHud.conf` (or a per-game
`~/.config/MangoHud/<exe>.conf`). Every *Write* rewrites the stat section
to exactly FPS + graphics-API line + each GPU's real name + your chosen
groups/extras, dedupes every key, keeps styling/keybind lines, and
recomputes `width` from the longest CPU/GPU label (MangoHud's auto-width
doesn't grow for a long custom `cpu_text`).

- **One CPU field + one GPU field per detected GPU**, each showing its PCI
  address (`sensors.gpu_devices()`), so two identical cards are told apart.
- Per-group **minimal ↔ full** toggles (CPU / GPU / Memory) — off = load %
  only, on = adds temp + power (or + VRAM).
- Explicit **Frametime graph**, **GPU core/mem clock**, and **Feral GameMode
  status** toggles (the last adds MangoHud's `gamemode` line → *GAMEMODE
  ON/OFF* in the overlay, so you can see whether `gamemoderun` engaged).
- **Place on screen…** — a translucent full-screen window; drag the overlay
  box (snaps to a 16×16 grid), release, and it snaps to the nearest of the 8
  MangoHud anchors + `offset_x/y`.
- **↻ Detect** fills the CPU/GPU name fields from the hardware.
- **Reset config (clean)** — a fresh minimal baseline (old file kept `.bak`).

### Last game session

A card that reads `~/.config/tuxthrottle/last_session.json` — the summary
`tuxthrottled` writes when a mapped game exits (max temps, avg clocks,
throttle %, duration).

---

## Tweaks & Apps

Reversible system tweaks by category — **Gaming**, **GPU**, **Power**,
**Performance**, **KDE (Desktop GUI Tweaks)**, **Stability** — plus
one-directional **app installs**. Each tweak has a `check` (current state),
an `apply` list and an `undo` list; the GUI's **apply ledger**
(`state.json`) adds "we set this" so it can tell *reverted* / *apply failed*
from *not applied*. **Apply Selected** acts on the ticks (apply ticked, undo
un-ticked, install ticked-but-missing apps), taking a `pre-apply-selected`
snapshot first. **Status report** / `--report` prints the full table.

App-install detection is cross-manager: an app counts as installed by **any**
route — its own `check`, a `provides` probe, `command -v <binary>`, or a
system-or-per-user flatpak-id probe — so Presets / Apply never add a second
colliding copy. Native rpm is preferred for Heroic / Prism / Protontricks /
Bottles / Discord / Sunshine …; Steam / OpenRGB / gamescope / MangoHud /
vkBasalt are native-only (a Flatpak of those breaks controller/udev, mods,
`LD_PRELOAD` layers).

### Notable tweaks

**Power** — `RyzenAdjTDP`, `BatteryChargeLimit`, `DellBatteryThreshold`,
`FanCurveDaemon` (`tuxthrottled` unit), `RyzenCurveOptimizer` (boot + sleep
`reapply`, confirmed offsets only), `StateResume` (sleep hook + boot service
→ `profiles reassert`), `GameModeBridge` (`/etc/gamemode.ini` custom
start/end → `gaming-performance` / `gaming-balanced`),
`PolkitTuxthrottlectl` (one `.policy` file so `pkexec tuxthrottlectl set …`
is passwordless for an active local user — no broad sudoers).

**GPU** — `NvidiaPowerLimit`, `NvidiaClockLock`, `NvidiaShaderCache`
(`/etc/environment.d/` `__GL_SHADER_DISK_CACHE_SKIP_CLEANUP=1` etc. — stops
the every-launch shader recompile), `NvidiaDynamicBoost` (`nvidia-powerd` —
un-caps the dGPU), `NvidiaRuntimePM` (`NVreg_DynamicPowerManagement=0x02` —
lets the idle dGPU D3-cold, `check_pending`, needs a reboot).

**Gaming / Performance** — `SchedExtGaming` (`scx_lavd`, the Steam-Deck
scheduler, as a self-owned service), `SplitLockMitigateOff`
(`kernel.split_lock_mitigate=0`; harmless no-op on Zen 3),
`AmdPstateActive` (grubby `amd_pstate=active`, `check_pending`),
`AcpiBacklightNvidiaWmiEc` (grubby kernel arg — brightness fix for some
Optimus G15s, `check_pending`).

**KDE (Desktop GUI Tweaks)** — 13 Plasma 6 toggles: `KdeAnimationsOff`,
`KwinGamingCompositor`, `KdeScreenEdgesOff` (incl. `[Effect-*]
BorderActivate=9` hot corners), `KdeActivitiesRecentOff`,
`KdeThumbnailIoLimit`, `KdeLaunchFeedbackOff`, `KdeSplashOff`,
`KdeClockSeconds`, `KdeClassicMenu`, `KdePanelFlush` (removes the floating
panel gap — writes both appletsrc **and** `plasmashellrc [PlasmaViews]`),
`KdeKwalletOff` (kills the login wallet prompt + KWallet passphrase caching,
`recommended`), `KdeMetaKeyOff` (a lone Meta/Super tap no longer opens the
launcher — the "Win key drops me out of a game" fix, `recommended`),
`KwinAllowTearing`. Each runs `kwriteconfig6` in your session and reloads the
live component. **Model-agnostic** — all pure KWin/Plasma config.

---

## System tray

An always-on **PySide6** tray icon (`tray_monitor.py`, unprivileged;
`/usr/local/bin/tuxthrottle-tray`):

- **Left-click** → opens the main TuxThrottle window (via `kstart`, so
  `pkexec`'s KDE auth agent is reachable — a detached spawn silently fails
  on KDE Wayland).
- **Middle-click** → toggle Game Mode.
- **Right-click** menu → a bold **Open TuxThrottle**, then live CPU / iGPU /
  dGPU readouts, a Game Mode item, **Power profile** and **Fan boost**
  submenus (`pkexec tuxthrottlectl set …`), and Quit.
- **Tooltip** → `TuxThrottle — Dell G15 power & gaming tuning` / `by
  BeanGreen247` / `Click to open · CPU … · dGPU …`.
- On start it re-applies the last saved **keyboard RGB** (background thread,
  `tuxthrottle_kbd apply-saved` — waits for the OpenRGB server + USB device).
- Single-instance via an `flock` on `$XDG_RUNTIME_DIR/tuxthrottle-tray.lock`.

**About tab → System tray** section: a checkbox that adds/removes
`~/.config/autostart/tuxthrottle-tray.desktop`, and a **Launch tray now**
button.

`--toggle` (from the G-key listener) just flips Game Mode and exits.

---

## tuxthrottled (background daemon)

`tuxthrottle_powerd.py`, installed by the **FanCurveDaemon** tweak. One poll
loop:

1. **Closed-loop fan curve** — `interp()` maps temp through
   `powerd.json fan_curve.points` to an additive `fanN_boost`, with cool-down
   hysteresis; restores auto on `SIGTERM`/`SIGINT`.
2. **AC ↔ battery auto-switch** — on an `AC*/online` transition, applies a
   `platform_profile` + ryzenadj TDP preset + panel refresh.
3. **Per-game auto-profiles** — scans `/proc` (comm + argv0); a matched exe
   (or `*` + a live Feral GameMode session) → snapshot + apply the mapped
   profile, revert on exit. While a game runs it samples clocks/temps/power
   each tick and writes `last_session.json` on exit.
4. **ThermalWatcher** — the alerts + fan-stall auto-recovery described under
   Power & Limits.
5. **ScheduleController** — the time-of-day schedule from Profiles.
6. **Control socket** — a newline-delimited-JSON RPC on
   `/run/tuxthrottle/control.sock` (`srw-rw---- root root`) exposing
   `status` / `reload` / `apply_profile` / `snapshot` / `rollback` / `set`.
   The GUI and CLI route writes through it when it's up, else act directly.

`run` / `once`. Config re-read on mtime change; `reload` over the socket
forces it.

---

## tuxthrottlectl (CLI)

`stdlib argparse` over `sensors.py` + `tuxthrottle_profiles` +
`tuxthrottle_control`, installed as `/usr/local/bin/tuxthrottlectl`.
`--json` on most commands; non-zero exit on failure. `set` / `profile apply`
/ `rollback` need root (or the `PolkitTuxthrottlectl` tweak + `pkexec`);
when the daemon socket is up they're routed through it.

| Command | Does |
|---|---|
| `status` / `status --json` | full state dump |
| `watch [interval]` | clear-screen one-line summary every N s (default 2) |
| `get {power-profile,tdp,fans,battery,nvpl,gamemode,clocks,gpumode}` | read one thing |
| `set {power-profile,tdp,fan-boost,battery,nvpl,gpumode,refresh,gpu-clock} …` | change one thing |
| `gamemode {on,off,toggle}` | Game Mode |
| `schedule {show,on,off}` | the time-of-day schedule |
| `profile {list,apply,save,show,delete} [name]` | named bundles |
| `snapshot [label]` / `rollback [last\|<file>]` | rollback points |
| `vram {status,profile <tier>,free,compositor-gpu <mode>}` | the VRAM tab, headless |
| `daemon {status,ping,reload}` | control-socket actions |
| `collect-model [--slug] [--out]` | emit a `models/<slug>.json` scaffold for a new board |

Examples:

```bash
tuxthrottlectl status --json | jq .cpu
tuxthrottlectl set power-profile performance
tuxthrottlectl set tdp --stapm 45 --fast 54 --slow 45
tuxthrottlectl set fan-boost both 60
tuxthrottlectl set gpu-clock 1200 --min 600      # lock the dGPU gr clock
tuxthrottlectl set refresh 60                     # panel Hz
tuxthrottlectl gamemode toggle
tuxthrottlectl profile apply "battery-quiet"
tuxthrottlectl vram profile extreme
tuxthrottlectl vram free
tuxthrottlectl watch 1
```

---

## Panel clients

Optional read-mostly front-ends in `clients/`, shipped to `/opt`:

- **waybar** — `clients/waybar/tuxthrottle-waybar`, a `return-type:json`
  module over `tuxthrottlectl status --json`; `--toggle` flips
  balanced ↔ performance.
- **plasmoid** — a Plasma 6 applet (`clients/plasmoid/package/`), install
  with `kpackagetool6`; polls `tuxthrottlectl`, has profile buttons.
- **mangohud** — `clients/mangohud/tuxthrottle-mangohud`, a one-line `exec=`
  bridge for MangoHud (profile + CPU W/°C + dGPU °C).

The profile buttons need root — the daemon socket or the
`PolkitTuxthrottlectl` policy.

---

## Report a Bug

The one non-hardware page (amber nav style, pinned to the bottom of the
rail). Reads only, uploads nothing.

- **Generate report** — a readable hw/OS/toolkit dump (`--debug`).
- **⧉ Copy for GitHub issue** — the same wrapped in a `<details>` block.
- **Collect hardware bundle** — a `.tar.gz` of raw dumps for onboarding a new
  laptop model: DMI, CPU/fan hwmon, `platform_profile` choices, GPU PCI ids,
  battery method, a `models/<slug>.json` scaffold, `lscpu` + `ryzenadj -i`,
  the ACPI DSDT (base64, for `iasl -d`), display modes, SMBIOS tokens, a
  fuller `nvidia-smi -q`, and a README spelling out the finish-the-model
  steps. Run the collector as root.

CLI equivalents: `python3 tuxthrottle.py --debug` / `--report` / `--collect
[dir]`.

---

## Install / uninstall

```bash
# system-wide install → /opt/tuxthrottle, a launcher, icon, .desktop entry
sudo ./install.sh

# remove the tool (tweaks kept)
sudo ./uninstall.sh
#   --purge   also undo every tweak's system bits (services, scripts, sudoers)
#   --grub --fstab --pip --all   for the boot-affecting extras
```

`install.sh` also refreshes the unit files + `/usr/local/bin` scripts of any
**already-enabled** service tweak, so an existing install picks up a new
version without re-toggling anything. A **COPR / RPM** path exists in
`packaging/` (`tuxthrottle.spec`, noarch); tweaks stay opt-in — `%post`
never touches them.

The reference machine is reachable during development as `ssh g15`
(hostname `Ashblade`, user `bean`).
