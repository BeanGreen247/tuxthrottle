# `models/` — per-board hardware profiles

One JSON file per supported laptop, keyed by DMI. `sensors.model_profile()`
reads the current machine's `/sys/class/dmi/id/{product_name,board_name}` and
returns the first file whose `match` block matches, falling back to
**`g15-5515`** (the reference platform — see `../CLAUDE.md`).

## Why it exists

`sensors.py` reads the CPU/fan hwmon names, the `platform_profile` path, the
PWM floor, the fan count, the per-fan boost attribute, the max fan RPM, and
which `platform_profile` value equals Game Mode from the matched profile
(accessors `_cpu_temp_hwmon` / `_fan_hwmon` / `_fan_pwm_hwmon` /
`_platform_profile_path` / `_pwm_floor` / `_fan_indices` / `_fan_boost_attr` /
`_fan_rpm_max` / `_game_mode_value`), and `tuxthrottle_kbd.py` /
`hotkey_listener.py` read the RGB device name + USB id and the G-key node +
keycode. Every one of those falls
back to the reference 5515 value when a field (or the whole profile) is absent,
so a machine with no matching file behaves exactly as before. The
`config/*.json` `check`/`apply` command strings are still 5515-specific — gate
those per model with `"models": [...]`.

## Adding a board

1. Collect the target's hardware:
   * `python3 tuxthrottle.py --collect` — the full diagnostic bundle, and
   * `tuxthrottlectl collect-model` — a `models/<slug>.json` **scaffold** with
     every field this machine can auto-detect filled in and the rest left as
     `null` with a `TODO` note.
   Open a "New hardware support" issue with the bundle.
2. Finish the scaffold: fill the `null`/`TODO` fields (keycodes, RGB controller,
   battery method, stock PPT) from the bundle and the vendor's Linux notes.
3. Test on the target with `TUXTHROTTLE_MODEL=<slug> python3 tuxthrottle.py`
   (dev override — forces the profile regardless of DMI; logs a warning).
4. Gate any tweak/app that differs by adding `"models": ["g15-5515", "<slug>"]`
   to its entry in `config/tweaks.json` / `apps.json`. An entry **without** a
   `models` key applies everywhere (nothing regresses). Add ids that must never
   run on the new board to its `tweaks_skip`.

## Schema (informal)

| key | meaning |
|---|---|
| `id` / `name` | slug (must equal the filename stem) and a human label |
| `match` | `{product_name: [...], board_name: [...]}` — any value matches |
| `cpu` | `vendor`, `codename`, **`hwmon`** (temp hwmon `name`, e.g. `k10temp`), `ryzenadj`, `stock_ppt_w` `[stapm, fast, slow]` |
| `igpu` / `dgpu` | vendor, driver, PCI id, `power_limit_locked` |
| `fans` | **`hwmon`** (RPM + additive boost), **`pwm_hwmon`** (real `pwmN`), `rpm_inputs`, **`additive_boost`** (per-fan boost attr names; else `fanN_boost`), `rpm_max` (gauge top; else 4700), `pwm`, `pwm_enable`, **`count`** (fan count; else derived from `rpm_inputs`), **`pwm_floor`** (min manual PWM), **`platform_profile_path`**, `platform_profiles` |
| `game_mode` | how Game Mode is toggled on this board — **`value`** is the `platform_profile` that == Game Mode (default `performance`); `mechanism`, `driver` are informational |
| `keyboard` | `rgb` backend, **`openrgb_device`** (OpenRGB device name), **`usb`** (`vid:pid`), `zones`, `effects`, `brightness_on` |
| `gkey` | **`device`** (evdev name), `evdev` path, **`keycode_fnlock_off`** (the `KEY_*` name the listener acts on), `keycode_fnlock_on` |
| `battery` | `sysfs_threshold` vs `method: "libsmbios"`, `interval`, `min_gap` |
| `tweaks_skip` | tweak ids that must never run on this board |

**Bold** keys are the ones `sensors.py` / `tuxthrottle_kbd.py` /
`hotkey_listener.py` read at runtime; the rest are documentation / for future
use. A missing bold key falls back to the 5515 value.
