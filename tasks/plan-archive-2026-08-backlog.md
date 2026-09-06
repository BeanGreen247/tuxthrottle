# Implementation Plan: TuxThrottle backlog — "go full ham"

Source: user request 2026-08-30 — implement the improvement backlog, UI + core
first, plus show live CPU/GPU clockspeeds. Compare-to-peers gaps
(asusctl / LenovoLegionLinux / CoreCtrl / LACT / TuxClocker).

## Architecture decisions
- New core lives in `sensors.py` (no GUI deps) so tray/hotkey can reuse it.
- Risky/system-persistent bits ship as `config/tweaks.json` entries + helper
  scripts in `/usr/local/bin` + narrow sudoers rules — same pattern as
  `PowerProfileScripts` / `CpuMaxPerformance`.
- New GUI surface: a **"Power & Limits"** tab (category `Power`, built like the
  Fans tab — live scales + presets, worker→queue, `_begin_busy` for slow ops).
- Fan curve + AC/battery auto-switch run as a small daemon
  `tuxthrottle_powerd.py` (stdlib only) behind a tweak-installed systemd unit.
- Everything gates on DMI (`sensors.detect_model`) — no new hard 5515 assumptions
  without a model check (roadmap note in CLAUDE.md).

## Phases

### Phase 0: Quick wins  ✅ DONE
- [x] SSH `g15` HostName → 192.168.0.100 (`~/.ssh/config`). Needs host-key trust
      on first connect: `ssh-keygen -R 192.168.2.105 && ssh-keyscan -H 192.168.0.100 >> ~/.ssh/known_hosts`
- [x] Dashboard: add **iGPU clock** + **dGPU clock** RingGauges (CPU clock gauge
      already existed). 8 gauges, 2×4 grid. Wired in `_poll_dash_queue`.
- [x] Standing rule saved to memory: load all skills each session.

### Phase 1: CPU TDP control (ryzenadj)   — core + UI
- [ ] `sensors.py`: `ryzenadj_path()`, `read_ryzenadj_info()` (parse
      `ryzenadj -i` → stapm/fast/slow limits + values + tctl), `set_ryzenadj(fast,
      slow, stapm)`. All return `(ok, msg)` / dict; `exit 0` on noise.
- [ ] `config/tweaks.json` → `RyzenAdjTDP`: `dnf install -y ryzenadj` (Nobara
      repo has it) OR build from source fallback; install
      `/usr/local/bin/tuxthrottle-tdp` wrapper + `tuxthrottle-tdp.service`
      (re-applies saved limits at boot) + sudoers rule for the wrapper.
      State: `~/.config/tuxthrottle/tdp.json`.
- [ ] Power&Limits tab: 3 sliders (STAPM / fast / slow, 10–80 W), live "current"
      readout, presets (Quiet 25/30/35, Balanced 35/45/54, Performance
      54/65/80), "Reset to firmware default" button.
- [ ] Test: `--report` still clean; slider write reflected in `ryzenadj -i`.

### Phase 2: Battery charge limit
- [ ] `sensors.py`: `battery_charge_limit_info()` →
      `{supported, path, current}` from
      `/sys/class/power_supply/BAT*/charge_control_end_threshold`;
      `set_battery_charge_limit(pct)`.
- [ ] `config/tweaks.json` → `BatteryChargeLimit`: udev rule +
      `tuxthrottle-battery-limit.service` to persist the % across reboots.
      State `~/.config/tuxthrottle/battery.json`.
- [ ] Power&Limits tab: slider 50–100 (step 5) + "Full charge once" button
      (sets 100 until next reboot). Hidden with a note if unsupported.

### Phase 3: NVIDIA power-limit slider
- [ ] `sensors.py`: `nvidia_power_limit_info()` →
      `{min, max, default, current}` from
      `nvidia-smi --query-gpu=power.limit,power.min_limit,power.max_limit,power.default_limit`;
      `set_nvidia_power_limit(watts)` → `nvidia-smi -pl` (needs root; wrapper).
- [ ] `config/tweaks.json` → `NvidiaPowerLimit`: `/usr/local/bin/tuxthrottle-nvpl`
      + sudoers + optional boot service. requires_vendor: nvidia.
- [ ] Power&Limits tab: slider min..max W, "Default" button. Greys out while
      dGPU asleep (don't wake it to poll).

### Phase 4: Feral GameMode bridge
- [ ] `config/tweaks.json` → `GameModeBridge`: `dnf install -y gamemode`;
      write `/etc/gamemode.ini` (or `~/.config/gamemode.ini`) with
      `[custom] start=/usr/local/bin/gaming-performance` /
      `end=/usr/local/bin/gaming-balanced`, plus `[general] renice=10
      softrealtime=auto`. Depends on `PowerProfileScripts`.
- [ ] Dashboard Game Mode box: show whether gamemoded is active + count of
      clients (`gamemoded -s`).
- [ ] Doc: README "launch games with `gamemoderun %command%`".

### Phase 5: Closed-loop fan curve  (daemon + editor)
- [ ] `tuxthrottle_powerd.py` (stdlib): reads CPU/GPU temp every 2 s, maps
      through a piecewise-linear curve → `fanN_boost` (safe additive lever,
      never dell_smm pwm). Curve from `~/.config/tuxthrottle/fancurve.json`
      (`[[temp,boost%],...]`, hysteresis). SIGTERM → restore auto.
- [ ] `config/tweaks.json` → `FanCurveDaemon`: install script +
      `tuxthrottle-powerd.service`.
- [ ] Fans tab: a small canvas curve editor (drag points) OR a 5-row
      temp→boost table + "Apply / Enable at boot". Live temp marker.

### Phase 6: AC/battery auto profile switch
- [ ] Fold into `tuxthrottle_powerd.py`: watch
      `/sys/class/power_supply/AC*/online`; on change apply a saved profile
      bundle (platform_profile + TDP preset + fan curve on/off) from
      `~/.config/tuxthrottle/autoswitch.json`.
- [ ] Power&Limits tab: two dropdowns "On AC → <preset>" / "On battery →
      <preset>" + enable toggle.

### Phase 7: `tuxthrottlectl` CLI
- [ ] Thin `argparse` CLI (own file or `tuxthrottle.py --ctl ...`) calling
      `sensors.py`: `get|set profile|tdp|fans|nvpl|battery`, `--json`.
      Non-zero exit on failure. Man-page-ish `--help`.

### Phase 8: Hybrid graphics mode (supergfxctl-lite)  — lowest priority
- [ ] Wrap `envycontrol`/PRIME: `sensors.gpu_mode_get/set(hybrid|integrated|nvidia)`.
      Likely just surface EnvyControl (already an app) in the GPU tab with a
      warning it needs logout.

## Checkpoints
- After Phase 1–3: deploy to g15, `--report` clean, sliders round-trip, GUI
  smoke (`root.update()` loop). Relaunch on g15 desktop.
- After Phase 5–6: verify daemon restores fan auto on stop; unplug/replug AC.
- Final: `verify-install.sh` passes; README + CLAUDE.md updated; memory backlog
  entry updated.

## Risks
| Risk | Impact | Mitigation |
|---|---|---|
| `ryzenadj` not in Nobara repo | Med | source build fallback in the tweak; feature-detect and hide UI |
| Dell has no `charge_control_end_threshold` | Med | `battery_charge_limit_info().supported` → hide slider, show note |
| Fan curve daemon fighting firmware curve | Low | only ever raises `fanN_boost` (additive), never dell_smm pwm; restore on exit |
| nvidia-smi -pl needs persistence mode | Low | wrapper does `-pm 1` first; reuses NvidiaMaxPerf pattern |
| Context/scope: 8 phases is XL | High | land phase-by-phase, deploy between; todo.md tracks state |
