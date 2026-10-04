# osu-lazer-tools

Tools for **osu!lazer on Linux**, for players, mappers and developers. Works on any distro and any PC;
nothing here is tied to specific hardware. Also bundled as a module in
[TuxThrottle](https://github.com/BeanGreen247/tuxthrottle) (Setup Games → osu!lazer).

Everything is reachable through one command, `osu-tools` (run it with no arguments for the list):

| Who | Command | What it does |
| --- | --- | --- |
| Players | `osu-tools launch` | Run osu! with low-latency tweaks, restore the system afterwards ([details](#osu-lazer-launcher)) |
| | `osu-tools diag` | Live dashboard of everything that adds latency, with what to change |
| | `osu-tools settings` | Apply competitive `game.ini` / `framework.ini` settings, with backups |
| | `osu-tools replay` | Unstable rate, hit-error graph and offset advice from your replays; pp; export |
| | `osu-tools tap` | Tapping speed test: stream BPM, consistency, stamina drop |
| | `osu-tools input` | Tablet setup check (permissions, driver conflicts), area / DPI / sensitivity maths |
| | `osu-tools skincheck` | Check a skin for broken HD images, missing animation frames, skin.ini mistakes |
| | `osu-tools backup` | Back up and restore beatmaps, skins, scores, collections, key bindings, settings |
| | `osu-tools report` | Read osu!'s logs; build a redacted bug-report bundle for GitHub |
| Mappers | `osu-tools mapcheck` | Check a mapset before submitting: snapping, files, metadata, audio, background |
| | `osu-tools maptools` | Sync metadata, copy hitsounds between difficulties, resnap, clean timing, shift offset |
| Developers | `osu-tools dev` | Build/run/test osu! from source, try a pull request, start a ruleset, star rating |

## Install

```bash
git clone https://github.com/BeanGreen247/osu-lazer-tools.git
cd osu-lazer-tools
./install.sh --download            # or: ./install.sh /path/to/osu.AppImage
```

This copies the tools to `~/.local/share/osu-lazer-tools`, puts `osu-tools` and `osu-lazer-launcher` in
`~/.local/bin` (thin wrappers around that copy), adds **osu!** to the Games menu and the desktop, and registers
`osu://` links and `.osz` / `.osk` / `.osr` files. Re-run `./install.sh` after a `git pull` to update.
`./install.sh --uninstall` removes it all; osu! and its data are never touched.

Needs: bash, Python 3.11+, GameMode (`gamemoderun`), PipeWire. Optional: `python3-textual` for the live diag
view, `evtest` for `measure-dpi`, `ffprobe` (ffmpeg) for audio checks in `mapcheck`, `zstd` for faster
backups, `pip install --user rosu-pp-py` for star rating and pp in `replay` and `mapcheck`, MangoHud only if
you want the overlay. Every script also runs on its own (`python3 osu_replay.py ...`) from a git clone.

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

## For players

### Replay analysis: `osu-tools replay`

Reads the replays osu!lazer already keeps in its data folder, so nothing needs exporting, and finds each
replay's beatmap by itself.

```
osu-tools replay list              newest replays, numbered
osu-tools replay show 1            analyse the newest one (or: osu-tools replay path/to/file.osr)
osu-tools replay export 3          copy replay 3 out as "Player - Artist - Title [Diff] (date).osr"
```

`show` re-judges every hit from your inputs and prints the unstable rate, mean hit error and an early/late
histogram. If you hit early or late on average it tells you the offset to set, same as the results screen's
"Calibrate using last play". It also shows how evenly you used your two keys, how long you hold them, and
how far your cursor travelled. With `rosu-pp-py` installed you also get star rating, pp and pp-if-FC.
Timing analysis covers osu! and osu!mania; stacking offsets and slider tails aren't simulated, so counts can
differ from the game by a few hits.

### Tapping test: `osu-tools tap`

`osu-tools tap` (100 taps), `--seconds 10`, `--keys zx`. Prints your 1/4-stream BPM, the unstable rate of
your taps, key balance, and whether you slowed down in the second half.

### Tablet and mouse: `osu-tools input`

- `status`: finds your tablet and checks that osu! can read it. osu!lazer runs OpenTabletDriver itself, so
  it needs read/write access to the tablet's `/dev/hidraw` node; without udev rules it can't get that. Also
  warns when the OpenTabletDriver daemon or a kernel tablet driver will fight osu! for the tablet, and shows
  osu!'s current tablet area and mouse settings from `input.json`.
- `mouse --dpi 1600`: how many cm cross the screen, and the tablet area that gives the same aim.
- `tablet 80 45`: checks an area's aspect against your screen and gives its mouse eDPI.
- `dpi-change --from 800 --to 1600`: the osu! sensitivity that keeps your aim identical.

### Skin checker: `osu-tools skincheck SKIN.osk`

Finds `@2x` images that aren't exactly twice the SD version, animations with missing frames,
`cursor2x.png`-style misnamed HD files, bad `skin.ini` colours and typos, a missing `Version`, files that
differ only in upper/lower case, and oversized elements. It also lists which elements fall back to the
default skin. Works on a folder too.

### Backups: `osu-tools backup`

```
osu-tools backup backup               full backup to ~/osu-backups (.tar.zst, logs/caches skipped)
osu-tools backup backup --settings-only --keep 5
osu-tools backup list
osu-tools backup restore FILE         moves the current data aside first, deletes nothing
```

Close osu! first. Its database is only consistent while the game is shut, and the tool refuses to run
otherwise.

### Logs and bug reports: `osu-tools report`

`sessions` lists each game session with its version and error count, `logs --errors` shows errors with their
stack traces (`-f` follows live), and `bundle` makes a zip plus a `summary.md` for a
[ppy/osu issue](https://github.com/ppy/osu/issues). The zip holds system info (distro, kernel, desktop, GPU
and driver, audio server), your settings, the newest session's logs and a diag report. Your home path,
username, osu! account name and anything token-like are redacted. Auth and network logs are left out unless
you ask for them.

## For mappers

In osu!lazer's editor, **File → Edit externally** opens the mapset as a normal folder. Run these on it, then
click "Finish editing" to import. **File → Export** gives an `.osz` that `mapcheck` also reads.

### `osu-tools mapcheck PATH`

Every issue is marked ✗ problem, ! warning or i info, and the exit code is 1 when there are problems.

- **Whole set:** metadata (artist, title, unicode fields, creator, source, tags, preview point, audio file)
  identical across difficulties, romanised fields ASCII-only, duplicate difficulty names, audio codec and
  bitrate (MP3 ≤ 192 kbps / OGG ≤ 208 kbps, via ffprobe), preview point inside the song, background present,
  ≤ 2560x1440 and ≤ 2.5 MB, missing files (background, video, hitsound samples, storyboard sprites), unused
  files, custom sample indexes without samples
- **Each difficulty:** unsnapped objects and slider/spinner/hold ends (2 ms or more, against every editor
  divisor including 1/5, 1/7, 1/9), objects at the same time or overlapping in a mania column, objects
  before the first red line, objects off a 4:3 screen, drain time under 30 s, green lines that change nothing,
  near-silent hitsounds, very dark or white combo colours, difficulty values out of range
- Star rating per difficulty for the spread (with `rosu-pp-py`)

The checks were tuned against 1,876 ranked difficulties so that they don't bury you in false alarms.

### `osu-tools maptools`

| Command | What it does |
| --- | --- |
| `sync-metadata FOLDER --from Insane` | Copy artist/title/unicode/creator/source/tags and the preview point to every difficulty |
| `copy-hitsounds FOLDER --from Insane [--to Hard Normal]` | Copy additions, sample sets and volume changes, matching objects by time. The target keeps its own slider velocities and kiai |
| `resnap FOLDER [--max 10]` | Move objects that are a few ms off onto the nearest tick |
| `cleanup FOLDER` | Delete green lines that change nothing |
| `offset FOLDER 25` | Shift timing, objects, breaks, preview point, bookmarks and video by 25 ms (for re-cut audio) |

Each command lists its changes and asks before writing (`--dry-run` only lists them, `-y` skips the
question). Originals go to `~/.local/share/osu-lazer-tools/map-backups/`, never inside the mapset.
Difficulties are chosen by file name or difficulty name.

## For developers: `osu-tools dev`

```
osu-tools dev setup [--framework] [--tools] [--install-dotnet]   clone ppy/osu into ~/osu-dev (OSU_DEV_DIR)
osu-tools dev build | run | test [TestSceneName]
osu-tools dev pr 12345          check out a ppy/osu pull request, then "run" to try it
osu-tools dev update            back to master
osu-tools dev local-framework on|off
osu-tools dev ruleset MyRuleset [--scrolling]   start a ruleset from ppy's official template
osu-tools dev sr map.osu -m HD  star rating from ppy's own calculator (osu-tools PerformanceCalculator)
osu-tools dev logs --errors -f  logs of your source build
```

`setup` reads the .NET SDK version osu! needs from its `global.json`. It checks you have it and, with
`--install-dotnet`, installs it per user into `~/.dotnet` using Microsoft's install script (no root).
Debug builds keep their data in `~/.local/share/osu-development`, so your real osu! data is never touched.
`pr` is useful for players too: it's how you test a fix before it's released.

## License

MIT
