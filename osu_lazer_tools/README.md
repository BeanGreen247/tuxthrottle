# osu-lazer-tools

Low-latency launcher, live latency diagnostics and a competitive-settings script for
**osu!lazer on Linux**. Works on any distro and any PC; nothing here is tied to specific hardware.
Also bundled as a module in [TuxThrottle](https://github.com/BeanGreen247/tuxthrottle)
(Setup Games → osu!lazer).

| File | What it does |
| --- | --- |
| `osu-lazer-launcher.sh` | Runs the official osu!lazer AppImage with every tweak below, restores the system when osu! exits |
| `osu_lazer_diag.py` | Live terminal dashboard of everything that adds latency, with what to change |
| `osu_lazer_settings.py` | Applies competitive `game.ini` / `framework.ini` settings, with backups |
| `install.sh` | Per-user install (no root) |

## Install

```bash
git clone https://github.com/BeanGreen247/osu-lazer-tools.git
cd osu-lazer-tools
./install.sh --download            # or: ./install.sh /path/to/osu.AppImage
```

This copies the tools to `~/.local/share/osu-lazer-tools`, puts `osu-lazer-launcher` in `~/.local/bin`
(a thin wrapper around that copy), adds **osu!** to the Games menu and the desktop, and registers
`osu://` links and `.osz` / `.osk` / `.osr` files. Re-run `./install.sh` after a `git pull` to update.
`./install.sh --uninstall` removes it all; osu! and its data are never touched.

Needs: bash, GameMode (`gamemoderun`), PipeWire. Optional: `python3-textual` for the live diag view,
`evtest` for `measure-dpi`, MangoHud only if you want the overlay.

## osu-lazer-launcher

```
osu-lazer-launcher [run]        launch osu! with the tweaks
osu-lazer-launcher doctor       show what was detected and what will be applied
osu-lazer-launcher diag         live latency dashboard (--once for a text report)
osu-lazer-launcher measure-dpi  measure your mouse's real hardware DPI
osu-lazer-launcher uninstall
```

What `run` does, each one switchable in `~/.config/osu-lazer-launcher/config`:

- **GameMode** always; power profile → `performance` while playing, restored after (`PERFORMANCE_PROFILE`)
- **Mouse acceleration off** while playing on KDE Plasma (KWin) and GNOME, restored after (`FLAT_MOUSE`)
- **Driver vsync off, 1 pre-rendered frame, no threaded-GL queue** so osu!'s own frame limiter decides latency (`DISABLE_DRIVER_VSYNC`)
- **Low-latency audio**: PipeWire quantum pinned for osu! (`AUDIO_QUANTUM`, default 256 = 5.3 ms at 48 kHz; PipeWire's default is 1024 = 21 ms) and a small pipewire-alsa buffer (`PW_ALSA_TUNE`, `ALSA_PERIOD_FRAMES`, `ALSA_PERIODS`) as measured in [ppy/osu-framework#6647](https://github.com/ppy/osu-framework/issues/6647)
- **Persistent NVIDIA shader cache** / DXVK state cache (`SHADER_CACHE_DIR`)
- **MangoHud kept out** of osu! even if it's enabled globally (`SHOW_MANGOHUD=no`)
- **Hybrid laptops**: `USE_DGPU=yes` renders on the NVIDIA dGPU through PRIME offload, with an automatic retry on the default GPU if osu!'s shaders fail to compile. On most laptops the panel is wired to the iGPU, so `USE_DGPU=no` avoids a frame copy

After changing audio settings, redo osu!'s offset calibration. If audio crackles, raise `AUDIO_QUANTUM`
to 512 or `ALSA_PERIOD_FRAMES` to 256.

## osu_lazer_diag.py

Run `osu-lazer-launcher diag` in a terminal while osu! is open (`r` refresh, `p` pause, `s` save report,
`q` quit), or `--once` / `--json`. It checks:

- the running osu! process: launched through the launcher, GameMode, MangoHud, and per-thread CPU load and
  preemptions for osu!'s Input / Audio / Update / Draw threads (osu! doesn't export its fps counters; for
  those use the in-game overlay, Ctrl+F11)
- audio: osu!'s live PipeWire quantum, xruns, pipewire-alsa buffer, an output-latency estimate, PipeWire's
  realtime priority
- GPU: which GPU osu! renders on, NVIDIA P-state, clocks, power, throttle reasons
- osu! settings: frame limiter, window mode, execution mode, renderer, audio device and offset
- CPU / power profile / temperatures, display refresh rate, VRR, KWin tearing
- input: USB mouse polling interval, `usbhid.mousepoll`, USB autosuspend, KWin pointer acceleration
- kernel: `threadirqs`, preemption, mitigations, clocksource, RT throttling, realtime limits, sound / USB IRQ
  thread priorities
- the launcher config

## osu_lazer_settings.py

Close osu! first (it rewrites its settings on exit). Sets: background dim 100 %, blur / hit lighting /
star fountains / beatmap skins, colours and hitsounds off, key overlay on, gameplay leaderboard off, mouse
buttons and wheel disabled in play, frame limiter Unlimited, fullscreen, multithreaded. Each file is backed
up next to itself first.

```bash
python3 osu_lazer_settings.py --dry-run   # show what would change
python3 osu_lazer_settings.py             # apply
python3 osu_lazer_settings.py --check     # exit 0 if already applied
```

## License

MIT
