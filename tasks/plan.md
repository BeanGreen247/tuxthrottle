# Implementation Plan: TuxThrottle — "one gaming-laptop tool, not one laptop's tool"

Source: user request 2026-08-31. Tiers 1–3 are done and verified on the g15.
Next effort = **B (generalise to multiple models)**, **C (harden)**, **D (new
capability)**. Ship-to-COPR (direction A) is explicitly deferred; the g15 stays
the test machine and gets the eventual release build.

## Overview

Today every `sensors.py` sysfs path, every hwmon name, every keycode and the
`config/*.json` `check`/`apply` commands assume the Dell G15 5515. `models/*.json`
+ `sensors.model_profile()` exist but are **advisory** — nothing reads them for
behaviour. This plan makes the model profile the single source of truth for
hardware specifics, gives a repeatable way to onboard a second board, replaces
the sudoers/socket privilege hacks with D-Bus + polkit, clears the dead keyboard
code, and adds a few model-agnostic features that raise the tool's ceiling.

## Architecture decisions

- **The model profile becomes load-bearing.** `sensors.py` gains a small set of
  private accessors (`_prof_cpu_hwmon()`, `_prof_fan_hwmon()`, `_prof_pwm_floor()`,
  `_prof_platform_profile_path()`, `_prof_battery_method()`, …). Each reads
  `model_profile()` and falls back to the current hard-coded 5515 value, so the
  refactor is a no-op on the reference board and independently testable there.
- **No behaviour change without a profile field.** If a value isn't in the schema
  yet, add it to `models/g15-5515.json` first with the current value, then route
  the code through it. The g15 file is the schema definition.
- **Auto-detection stays.** Where `sensors.py` already globs hwmon names or PCI
  vendors (`_hwmon_by_name`, GPU vendor probe), the profile supplies the *name to
  match* / *disambiguation hint*, not a replacement for detection.
- **Privilege: D-Bus + polkit is the new front door**, the unix control socket
  stays as the fallback, direct writes stay as the last resort. `tuxthrottlectl`
  and the GUI try them in that order. Sudoers rules that a polkit action fully
  replaces are removed by the tweak's `undo` + re-`apply`.
- **Dead keyboard code deletion is its own branch** (`chore/kbd-dead-code`),
  needs camera-verified keyboard smoke on the g15, and is not mixed with any
  other change.
- **New features (D) must not add a 5515 assumption** — each ships behind a
  profile capability flag or pure sysfs that exists on any laptop.

## Phases

Order note: the dead keyboard-code deletion (originally Phase 2.2) is done
**first**, as Phase P — it removes ~600 lines from `tuxthrottle_kbd.py` before
Phase 0 has to route that file through the model profile, and it needs the g15
keyboard while the hardware quirks are fresh.

### Phase P — Dead keyboard code deletion  ·  ~2 tasks

Branch `chore/kbd-dead-code`. Audit `stop_fx()` call-sites, remove
`rainbow_wave` / `gradient_wave` / `_Sdk` / `_stream_wave` / `stop_fx` and the
SDK-socket code kept only for them, drop `rainbow-test` / `gradient-test` from
`verify-install.sh`. Camera-verify solid / spectrum / brightness / off on the
g15. Merge to main before Phase 0.

### Phase 0 — Model-profile plumbing (the spine)  ·  ~4–5 tasks

Route the hard-coded 5515 specifics in `sensors.py`, `tuxthrottle_kbd.py` and
`hotkey_listener.py` through `model_profile()` with current values as fallbacks.
Ends with the g15 behaving identically (`verify-install.sh` 27/0, `--report`
unchanged, GUI smoke clean).

### Checkpoint 0
- [ ] `--report` output on the g15 is byte-identical to pre-refactor (diff saved)
- [ ] `verify-install.sh` → 27 passed, 0 failed on the g15
- [ ] `pytest` green; new tests cover "profile field missing → fallback value"
- [ ] GUI builds and every tab renders (headless smoke)

### Phase 1 — Second-model onboarding path (B)  ·  ~4 tasks

Make "add your laptop" a documented, mostly-mechanical process. No second machine
is available yet, so this phase is validated by: the scaffold generator runs on
the g15 and reproduces `g15-5515.json`'s detectable fields; a synthetic
`models/_test-fixture.json` + `TUXTHROTTLE_MODEL=` override exercises the
non-5515 code paths under pytest.

### Checkpoint 1
- [ ] `tuxthrottlectl collect-model` emits a valid `models/<slug>.json` scaffold
- [ ] `TUXTHROTTLE_MODEL=<slug>` override selects a profile regardless of DMI
      (dev/testing only; logged loudly)
- [ ] `Item.requires_models` + `tweaks_skip` verified to hide the right entries
      for a non-5515 profile (fixture test)
- [ ] `models/README.md` is a complete step-by-step onboarding guide

### Phase 2 — Harden (C)  ·  ~4 tasks

~~D-Bus + polkit control plane~~ (built then **reverted** — the tweak's
system-bus policy bricked the g15 boot; dbus-broker rejects `send_interface=`
in a policy `<allow>` and only parses it at boot. Socket stays the control
plane; retry only with VM boot-testing). Dead keyboard-code deletion (Phase P);
CI depth (headless GUI smoke, `ruff` blocking + clean, advisory `mypy`).

### Checkpoint 2
- [ ] `busctl` shows `org.tuxthrottle.Daemon`; a polkit prompt gates a profile
      apply from a non-root GUI; socket + direct fallbacks still work
- [ ] `chore/kbd-dead-code`: `rainbow_wave`/`gradient_wave`/`_Sdk`/`_stream_wave`/
      `stop_fx` gone, `verify-install.sh` no longer calls `*-test`, and
      solid-colour / spectrum / brightness / off are camera-verified on the g15
- [ ] CI runs a headless GUI-smoke job + `ruff` + `mypy`; all green
- [ ] `pytest` count up; `powerd` fan-curve + schedule paths covered

### Phase 3 — New capability (D)  ·  pick 2–3, ~2 tasks each

Independent of 0–2; can run in parallel or as a change of pace. Candidates,
highest-confidence first:
1. **Battery health page** — `cycle_count`, `charge_full` vs
   `charge_full_design` → wear %, plus the existing charge-limit controls, on one
   nav page. Pure sysfs, works on any laptop.
2. **Post-game session summary** — the daemon already sees game start/exit and
   thermal events; accumulate max tctl, avg CPU/GPU clock, seconds spent
   throttled, write `~/.config/tuxthrottle/last_session.json`, show it in the GUI.
3. **Scheduled / conditional profiles** — `powerd.json` `schedule` block:
   time-of-day or on-AC/on-battery → `apply_state(load_profile(...))`.
4. *(stretch)* **MangoHud bridge** — `clients/mangohud/` custom line fed by
   `tuxthrottlectl status --json`.

### Checkpoint 3
- [ ] Each shipped feature has tests, a README entry, and a CLAUDE.md layout-table
      update
- [ ] No feature introduces a hard-coded model assumption (grep review)
- [ ] Verified live on the g15

## Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| No second physical machine to validate B | High | Ship the *routing* + scaffold + fixture tests now; mark real second-board bring-up as blocked, not done |
| D-Bus/polkit is a large unknown on Nobara/KDE | Med | Timebox Phase 2.1; keep the socket path fully working so a stall doesn't block anything |
| Dead-code deletion breaks a live keyboard path | Med | Own branch, camera verification, `stop_fx()` call-sites audited before removal |
| Profile-refactor silently changes a g15 value | High | Fallback-to-current-value for every field; byte-diff `--report` at Checkpoint 0 |
| Scope creep in D | Low | Cap at 2–3 features; the rest go to the backlog memory |

## Open questions

- Which Phase 3 features does the user want? (Recommend 1 + 2.)
- D-Bus service name / interface — `org.tuxthrottle.Daemon1` with a versioned
  interface, or unversioned? (Lean versioned.)
- Do we want the `TUXTHROTTLE_MODEL` override in shipped builds or dev-only
  behind an env guard? (Lean dev-only, loud warning.)
