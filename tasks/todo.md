# TuxThrottle — todo: "gaming-laptop tool, not one laptop's tool"

Full plan: `tasks/plan.md`. Prior (completed) backlog archived at
`tasks/*-archive-2026-08-backlog.md`. g15 stays the test machine; COPR release
(direction A) deferred.

**Order:** dead-code deletion (was 2.2) is pulled to the front — it shrinks
`tuxthrottle_kbd.py` before Phase 0 routes it through the model profile. Then
Phase 0 → 1 → rest of 2 → 3. Phase 3 scope = Battery health page + MangoHud
bridge only (post-game summary + scheduled profiles → backlog).

## Phase P — Dead keyboard code deletion (pulled forward from 2.2)

- [x] **P.1 Branch `chore/kbd-dead-code`.** Audit every `stop_fx()` call-site in
      live paths first; note what each does and its replacement (usually nothing).
- [x] **P.2 Delete** `rainbow_wave` / `gradient_wave` / `_Sdk` / `_stream_wave` /
      `stop_fx` and any now-orphaned helpers (`fx.pid` handling, SDK-socket code
      kept only for the waves).
- [x] **P.3 `verify-install.sh`** — drop the `rainbow-test` / `gradient-test`
      invocations; keep the rest of the keyboard block.
- [x] **P.4 Deployed to g15** (2026-08-31) — `install.sh` OK, `verify-install.sh`
      **27 passed / 0 failed**, kbd CLI smoke passes. `tuxthrottle_kbd.py`
      1293 → 480 lines; no `stop_fx` refs anywhere. **User camera-verify pending:**
      solid colour, Spectrum Cycle, brightness up/down, off.
- [x] **P.5** Merged `chore/kbd-dead-code` → main (ff), pushed, branch deleted.

## Phase 0 — Model-profile plumbing (spine)

- [x] **0.1 Schema pass on `models/g15-5515.json`.** Confirm every hard-coded
      value in `sensors.py` has a home in the schema; add missing fields with the
      current 5515 value. Fields at least: `cpu.hwmon`, `fans.hwmon`,
      `fans.pwm_hwmon`, `fans.pwm_floor`, `fans.platform_profile_path`,
      `fans.count`, `battery.method`, `keyboard.*`, `gkey.*`.
      *Accept:* schema documented in `models/README.md`; `g15-5515.json` validates
      against it in CI.
- [x] **0.2 `sensors.py` accessors + routing.** Add `_prof_*()` helpers reading
      `model_profile()` with the current constant as fallback. Route: `k10temp`
      (L140), `_PLATFORM_PROFILE` (L440), `PWM_FLOOR` (L441), `alienware_wmi` /
      `dell_smm` names, fan-index count, battery method branch.
      *Accept:* no literal `"k10temp"` / `"alienware_wmi"` / `"dell_smm"` /
      `/sys/firmware/acpi/platform_profile` left outside a `_prof_*` fallback;
      `--report` on g15 unchanged.
- [x] **0.3 `tuxthrottle_kbd.py` + `hotkey_listener.py` routing.** OpenRGB device
      name, zone count, effect list, `brightness_on` from `keyboard.*`; evdev
      device + keycodes from `gkey.*`.
      *Accept:* keyboard solid-colour + spectrum still work on g15; G-key still
      toggles Game Mode.
- [x] **0.4 Fallback tests.** `tests/test_model_routing.py`: profile field
      present → used; absent → 5515 fallback; unknown DMI → g15-5515 profile.
      *Accept:* pytest green, coverage on every `_prof_*` helper.
- [x] **0.5 Checkpoint 0** — byte-diff `--report`, `verify-install.sh` 27/0,
      headless GUI smoke, pytest. Commit `refactor(sensors): route hw specifics
      through model_profile`.

## Phase 1 — Second-model onboarding (B)

- [x] **1.1 `tuxthrottlectl collect-model`.** Emit `models/<slug>.json` scaffold
      from the live machine: DMI match block, detected CPU/fan hwmon names, PCI
      ids, platform_profile path + choices, fan count, battery-method probe,
      OpenRGB device if present. Unknown fields → `null` + a `TODO` comment key.
      *Accept:* run on g15 reproduces every *detectable* field of
      `g15-5515.json`.
- [x] **1.2 `TUXTHROTTLE_MODEL=<slug>` override.** Force a profile regardless of
      DMI; `sensors` logs a loud one-line warning; guarded so it's obviously
      dev-only.
      *Accept:* override selects the fixture profile under pytest; warning present.
- [x] **1.3 Gating audit.** `models/_test-fixture.json` (non-5515, with
      `tweaks_skip` + `requires_models` entries). Verify `_apply_vendor_gate` /
      `Item.requires_models` / `tweaks_skip` hide the right tweaks/apps.
      *Accept:* fixture test asserts hidden vs shown item sets.
- [x] **1.4 `models/README.md` onboarding guide.** Step-by-step: run
      `--collect`, run `collect-model`, fill the TODO fields, test with
      `TUXTHROTTLE_MODEL`, add `requires_models` gates, PR.
      *Accept:* a reader with a new laptop could follow it unaided.
- [x] **1.5 Checkpoint 1.**

## Phase 2 — Harden (C)

- [~] **2.1 D-Bus + polkit control plane — ATTEMPTED, REVERTED 2026-08-31.**
      Built it (`tuxthrottle_dbus.py` system-bus service + `DbusPolkitIntegration`
      tweak). The tweak's `dbus/*.conf` **hard-bricked the g15 boot**: dbus-broker
      refuses to start the system bus when a `system.d` policy has a
      `send_interface=` clause in an `<allow>` rule, and it only parses that file
      at boot (passed a live `ReloadConfig`). Whole D-Bus layer removed; the
      `/run/tuxthrottle/control.sock` socket stays the only control plane.
      **Any retry: boot-test in a VM first, no `send_interface` in policy.**
- [x] **2.2 Dead keyboard code** — pulled forward, see Phase P.
- [x] **2.3 CI depth.** Headless Xvfb GUI-smoke job (build `ToolkitApp`, pump
      `update()`); `ruff` + `mypy` steps (start non-blocking, then gate); extra
      `powerd` tests (fan-curve interp, schedule).
      *Accept:* CI green with the new jobs; lint baseline recorded.
- [x] **2.4 Checkpoint 2.**

## Phase 3 — New capability (D) — user picked 3.1 + 3.2

- [x] **3.1 Battery health page** — wear %, cycle count, design vs full
      capacity + charge-limit controls on one nav page. Pure sysfs, model-
      agnostic.
- [x] **3.2 MangoHud bridge** — `clients/mangohud/` custom overlay line fed by
      `tuxthrottlectl status --json` (profile / TDP / temps in-game).
- [x] **3.3 Checkpoint 3.**

## Deferred / not now

- Direction A: COPR release, `v*` tag, CHANGELOG — after B/C/D land on the g15.
- D-Bus item was also Tier-3-deferred; now folded into 2.1.
- Phase 3 backlog (not this round): post-game session summary; scheduled /
  conditional profiles (`powerd.json` `schedule` block).
