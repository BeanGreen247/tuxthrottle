# Panel / status-bar clients

Thin read-mostly front-ends over `tuxthrottlectl status --json`. They ship as
**optional extras** — nothing in the app depends on them.

## waybar / eww — `waybar/tuxthrottle-waybar`

A `return-type: "json"` custom module: CPU/dGPU temp + a one-letter profile
badge, a `class` of cool/warm/hot/critical for CSS, and `--toggle` on
`on-click` to flip balanced ↔ performance. The toggle tries `tuxthrottlectl`
directly, then `pkexec tuxthrottlectl` (passwordless with the
**PolkitTuxthrottlectl** tweak, otherwise an auth dialog).

```jsonc
"custom/tuxthrottle": {
    "exec": "/opt/tuxthrottle/clients/waybar/tuxthrottle-waybar",
    "return-type": "json",
    "interval": 5,
    "on-click": "/opt/tuxthrottle/clients/waybar/tuxthrottle-waybar --toggle"
}
```

Glyphs in the default output assume a Nerd Font; swap them in the script if you
don't use one.

## KDE plasmoid — `plasmoid/package/`

A Plasma 6 applet: compact view = icon + CPU temp; expanded = profile + CPU/dGPU
temps + Balanced/Performance buttons. It polls `tuxthrottlectl` every 5 s via the
`executable` data source.

Install for the current user:

```bash
kpackagetool6 --type Plasma/Applet --install  clients/plasmoid/package
# update in place later:
kpackagetool6 --type Plasma/Applet --upgrade  clients/plasmoid/package
```

Then add the **TuxThrottle** widget to a panel or the desktop.

The profile buttons run `pkexec /usr/local/bin/tuxthrottlectl set power-profile …`.
Apply the **PolkitTuxthrottlectl** tweak (Power tab) and that runs passwordlessly
for an active local user; otherwise `pkexec` shows a normal auth dialog (or the
`tuxthrottled` control socket handles it if the FanCurveDaemon tweak is on). The
temps update regardless.

## MangoHud — `mangohud/tuxthrottle-mangohud`

A one-line in-game overlay via MangoHud's `exec=` directive: current profile
(abbreviated), CPU package power + temp, and dGPU temp when it's awake.

```ini
# ~/.config/MangoHud/MangoHud.conf
exec=/opt/tuxthrottle/clients/mangohud/tuxthrottle-mangohud
exec_name=TuxThrottle
```

Read-only — it never changes anything. MangoHud calls it ~1×/s; the script uses
short timeouts and prints `TT n/a` if `tuxthrottlectl` can't be reached.
