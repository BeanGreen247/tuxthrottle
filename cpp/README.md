# TuxThrottle (C++)

The full rewrite of TuxThrottle in C++20. No Python, no JSON library, no test framework: the core is the
standard library and libc, the window is SDL2 + FreeType + fontconfig (the same stack as Hootedit), and the system
integration shells out to the tools every Linux gaming box already has (curl, tar, systemd-run, nvidia-smi, ...).

## Binaries

| binary | what it is |
|---|---|
| `tuxthrottle` | the window (re-runs itself elevated through pkexec / sudo). `--report`, `--debug`, `--collect`, `--tui`, `--version` also work without a display |
| `tuxthrottlectl` | the headless CLI: status, get/set, profiles, snapshots, tweaks, presets, drives, Steam / Proton / MangoHud tools, diagnostics, GPU driver installer |
| `tuxthrottled` | the power daemon: fan curve, AC/battery switch, per-game profiles, schedule, thermal alerts, control socket |
| `tuxthrottle-tray` | StatusNotifierItem tray icon (own D-Bus client), popup menu, mini overlay, crash-watch notifications, G-key listener lives in `tuxthrottlectl hotkey` |

## Build and install

    sudo ./cpp/install.sh            # installs the build dependencies for your distro, builds with -O3, installs
    sudo ./cpp/install.sh --uninstall

Build only:

    meson setup cpp/build cpp --buildtype=release -Doptimization=3
    ninja -C cpp/build
    meson test -C cpp/build          # unit tests, no hardware needed

Build dependencies: a C++20 compiler, meson, ninja, SDL2, FreeType and fontconfig development packages.
Fonts: Inter (falls back to any fontconfig match, DejaVu Sans Mono for monospace).

## Layout

    src/                 core modules (json, sensors, engine, steam, compat, mangohud, diag, ...), CLI and daemon
    src/ui/              the immediate-mode toolkit: gfx (SDL2 + FreeType renderer), ui (layout and widgets)
    src/app/             the window: shell (sidebar, footer, log, jobs) and one file per page
    src/tray/            the tray process
    tests/               unit tests (custom CHECK macros)

The tweak / app / preset / game data is shared with the Python version (`config/*.json`). Commands in that data
that still name a Python helper are rewritten when they are expanded (`python3 {TOOLKIT_DIR}/tuxthrottle_X.py`
becomes `tuxthrottlectl X`), so one set of config files serves both.

## Development helpers

    tuxthrottle --no-elevate --page "Game Tools" --shot out.ppm       # render one page to a PPM
    tuxthrottle --no-elevate --script steps.txt                       # drive the UI: click X Y, text S, key NAME,
                                                                      # wheel N, page NAME, shot FILE, wait N, quit
    TT_SCALE=1.5 tuxthrottle                                          # UI scale factor
