# Session handoff — 2026-09-05 → 2026-09-06

Written at the end of a very long session (many hours, dozens of features)
specifically so the next session can pick up cold without re-deriving any of
this. Read this whole file before doing anything else.

## Where things are

- **Repo**: `/home/bean/dellg15_tool_nobara/tuxthrottle`, GitHub
  `BeanGreen247/tuxthrottle`.
- **Active branch**: `refactor-modular-ui` (NOT merged to `main` yet). Draft PR
  open: https://github.com/BeanGreen247/tuxthrottle/pull/2 — keep pushing to
  this branch, CI runs automatically on every push because a PR is open
  against `main` (pushing to a bare feature branch with no PR does **not**
  trigger CI — learned this the hard way; had to open the PR mid-session).
- **Deploy target**: `g15` (SSH alias, IP 192.168.0.100, user `bean`). Dev
  source synced to `~/tuxthrottle_src/` on g15, installed to `/opt/tuxthrottle`
  via `sudo ./install.sh`. Real Wayland session is active there (`Ashblade`
  hostname) — GUI/tray can be launched for real (not just headless) via SSH,
  see "How to verify a change" below.
- **New skill pushed upstream**: `~/.claude/skills/module-extraction-verification/`
  (repo `BeanGreen247/claude-skills`, already committed + pushed to `main`).
  **Load this skill before doing any more module-split work.** It documents
  the diff-then-ruff-lint workflow this session used for every extraction.

## What this session actually did (roughly chronological)

1. **Diagnosed and fixed a real "Steam crashed" report** — turned out to be
   Steam's autostart racing an NTFS drive's mount at login. Shipped as a
   `SteamAutostartMountWait` Stability tweak, plus made `steamperf.py`'s
   low-resource-mode toggle preserve that wrapper across regeneration
   (previously it silently wiped it).
2. **Built a "Fixes" box** in Game Tools: one-click "Steam won't start"
   diagnostic, an unmounted-drive checker with a per-drive Mount button, and
   a shared fix-history log (`tuxthrottle_fixlog.py`).
3. **Background crash watcher** in the tray (`tuxthrottle_crashwatch.py`,
   polls every 30s) — classifies coredumps/journal signatures (benign Proton
   bootstrap self-quit vs. real crashes vs. dirty-NTFS vs. scx_lavd stalls),
   notifies + logs.
4. **ProtonDB badges** on every game card in Setup Games
   (`tuxthrottle_protondb.py`, disk-cached, offline-safe).
5. **"Boost 100% for 60s"** fan quick-action with an independent systemd
   revert timer (survives the GUI closing) — verified live, boosts and
   reverts correctly.
6. **Fan-curve live-position dot**, **MangoHud status-line tie-in**
   (`tuxthrottle_mangohud_status.py` — periodically rewrites one config line,
   relies on MangoHud's own inotify hot-reload), **mini always-on-top tray
   overlay** (visual placement not independently screenshot-confirmed —
   worth a manual glance), **Dashboard STAPM-vs-stock-TDP delta**.
7. **Two real bugs found by testing everything end-to-end, not just
   individually**:
   - `tuxthrottle_crashwatch.py`'s coredumpctl parser broke on any EXE path
     containing a space, or a row with a trailing placeholder column — fixed
     by switching to `coredumpctl list --json=short`.
   - The Diagnostics hardware-bundle collector ran `kscreen-doctor` as bare
     root with no display session, which **SIGABRTs instead of failing
     cleanly** — was silently coredumping on every bundle collection. Fixed
     via `sensors._session_cmd()` (the same real-session hop the Display
     tab's refresh-rate switcher already used).
8. **CI hardening** (explicitly asked for, ongoing standing instruction — see
   "Standing instructions" below): added a `tui-smoke` job, gated 7 new
   modules through mypy, fixed real lint/typing issues each one surfaced.
9. **Textual TUI** (`tuxthrottle_tui.py`) added *alongside* the Tkinter GUI,
   not replacing it — dashboard + Game Mode toggle + fan boost + fixes log,
   reusing the same headless modules the tray uses. `install.sh` now pulls in
   `python3-textual`/`python3-rich`.
   - Caught and fixed a real CI-only bug here too: `Static.renderable` isn't
     a stable attribute across Textual versions (pip's vs. Fedora's) — now
     tracks `.value` itself instead.
   - Also caught (and reverted) a test that briefly flipped Game Mode for
     real on live hardware because it only mocked `shutil.which()`, not
     `subprocess.run()` — the fixed test mocks both.
10. **The internal module-split refactor** (the big one, still in progress —
    see "What's left" below). Flat-file layout preserved on purpose — **no**
    `pyproject.toml` package restructure, no path changes anywhere tweaks.json
    /systemd/polkit reference a script by filename. Method bodies moved
    **verbatim** into mixin classes in new files; `ToolkitApp` multiply-
    inherits from all of them. `self.foo()` resolves through the MRO
    regardless of which file defines it, so call sites never needed to change.
11. **Found and fixed a real bug in the refactor itself, right before writing
    this handoff**: the Profiles-tab extraction never actually deleted the
    original from `tuxthrottle.py` — it sat there as dead code, silently
    shadowed by Python's MRO (ToolkitApp's own copy wins over an inherited
    mixin's). Not a functional bug (both copies were identical), but the
    extraction hadn't taken effect. Caught by an AST-based script comparing
    every mixin's method names against `ToolkitApp`'s own — **do this check
    after every future batch of extractions**, don't just trust "it still
    runs fine" as proof nothing was left behind. Fixed and pushed
    (`ca8d971`).
12. **Unit test coverage improved on every push** (standing instruction):
    189 passing (was 150 at the start of the refactor work), covering 5
    previously-untested modules plus the two data/widget extraction modules.

## Standing instructions from the user this session (still apply)

- **After every push, check real CI**, not just local test replication:
  `gh run list --branch refactor-modular-ui --limit 3` then
  `gh run watch <id> --exit-status`. This is why the draft PR exists — CI
  doesn't run on a bare branch push otherwise.
- **Keep improving unit tests** as you go, not just maintaining the existing
  count.
- **Use the `module-extraction-verification` skill** for any further
  extraction work (load it explicitly via the Skill tool at the start of a
  session that continues this refactor).
- Standing model/effort cap from session start: Sonnet 5, medium effort —
  don't self-escalate without the user asking.
- No RPM builds — the existing spec-lint step (parse-only) is fine, don't add
  an actual `rpmbuild` step.
- No AI attribution trailers in commit messages (user's global CLAUDE.md) —
  this was already the effective behavior all session despite an
  in-conversation instruction that briefly said to add them; the harness
  enforced the user's own preference either way. The `claude-skills` repo has
  its own explicit "no Claude-Session links / AI attribution" commit rule too.

## How to verify a change (the rigor established this session)

For anything touching `tuxthrottle.py` or its extracted modules, in order:

1. `python3 -m py_compile <files>` then `ruff check <files>` — fix every
   F821/F401 before moving on (see the skill for why this matters).
2. `python3 -m pytest tests/ -q` locally.
3. `rsync -a --exclude='.git' --exclude='__pycache__' ./ g15:~/tuxthrottle_src/`
   then `ssh g15 'sudo ~/tuxthrottle_src/install.sh'` then
   `sudo /opt/tuxthrottle/verify-install.sh` (want 32/32).
4. **A real mainloop GUI launch on g15** — not just the headless
   `r.update()`-loop smoke test CI uses. This distinction caught a real
   threading bug this session (`main thread is not in main loop`) that the
   headless style never would have. Pattern:
   ```bash
   ssh g15 'sudo pkill -f "tuxthrottle.py$" 2>/dev/null; sleep 1
   sudo env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-0 DISPLAY=:0 \
     XAUTHORITY=/run/user/1000/xauth_PDTpSV setsid /usr/bin/python3 \
     /opt/tuxthrottle/tuxthrottle.py >/tmp/tt.log 2>&1 &'
   sleep 6; ssh g15 'pgrep -af tuxthrottle.py; cat /tmp/tt.log'
   ```
   (find the current `xauth_*` filename via `ls /run/user/1000/ | grep xauth`
   if it's changed since a reboot).
5. Screenshot when useful:
   `sudo -u bean env XDG_RUNTIME_DIR=/run/user/1000 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus WAYLAND_DISPLAY=wayland-0 spectacle -b -n -o /tmp/x.png`
   then `scp` it back and Read it. No click/input-injection tool exists on
   g15 (no xdotool/ydotool/kdotool) — to exercise a specific tab, drive it
   programmatically instead: build `ToolkitApp` in a script, call
   `a.notebook.select(frame)` for the target tab by walking
   `a.notebook._pages`, *then* screenshot (screenshot must happen while the
   script's `r.update()` loop is still spinning, mind the timing — a script
   that exits before you screenshot just gets you the bare desktop).
6. For non-GUI logic, `pytest.importorskip("ttkbootstrap"/"textual", exc_type=ImportError)`-guarded
   test files are the pattern — this sandbox's own ttkbootstrap is broken
   (PIL/ImageTk), and CI's main `checks` job doesn't install ttkbootstrap
   either (only the separate `gui-smoke` job does).
7. After any batch of mixin extractions specifically, run an AST audit
   (see below) to make sure nothing was left duplicated:
   ```python
   import ast
   def class_methods(path, target_class=None):
       tree = ast.parse(open(path).read(), filename=path)
       out = {}
       for node in ast.walk(tree):
           if isinstance(node, ast.ClassDef):
               if target_class and node.name != target_class: continue
               out[node.name] = [n.name for n in node.body
                                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
       return out
   # then compare ToolkitApp's own method list against every mixin's —
   # any name in both means the mixin's copy is a dead, MRO-shadowed twin.
   ```

## Module-split refactor — DONE (2026-09-06)

Finished in the 2026-09-06 session and **merged to `main`** (PR #2 merge
commit `a1fb8c9`; `refactor-modular-ui` branch deleted). `tuxthrottle.py` is
now **1450 lines** (from 8133). Final batch extracted five more mixins +
one helper module, all verbatim-diffed, `ruff`/`mypy`/`pytest` clean, CI
green (checks/gui-smoke/tui-smoke/typecheck), and verified with a real
mainloop GUI launch on the g15 (Dashboard polling loop live, no errors):

- `tuxthrottle_tab_dashboard.py` (`DashboardTabMixin`) — dashboard tab +
  the `_dashboard_loop`/`_poll_dash_queue` polling worker.
- `tuxthrottle_tab_power_display.py` (`PowerDisplayTabMixin`) — battery-
  health / power / display / touchpad tabs and every interleaved helper
  section (TDP, CO, NVPL, GPU clock/mode, refresh, VRR, auto-switch).
- `tuxthrottle_tab_category.py` (`CategoryTabMixin`) — `_build_category_tab`
  + `_build_presets_tab`.
- `tuxthrottle_tab_games.py` (`GamesTabMixin`) — Setup Games + Game Tools,
  all boxes (shadercache/steamperf/launchopts/mangohud/savevault/prefix/
  Fixes). Biggest slice, ~2300 lines.
- `tuxthrottle_tab_diagnostics.py` (`DiagnosticsTabMixin`) — Diagnostics
  tab UI only.
- `tuxthrottle_diag.py` — the heavy report builders (`collect_debug_report`,
  `collect_hw_bundle`, `wrap_issue_block`, `GITHUB_ISSUE_TEMPLATE`,
  onboarding dumps). No Tk deps; moved out of module scope so the diag tab
  mixin imports it without a circular dep. Added to the CI mypy gate.

New tests: `tests/test_diag.py`, `tests/test_module_split.py` (AST guard
against MRO-shadowed method twins + monolith regrowth). 200 passing in the
sandbox / 214 on the g15.

What stayed on `ToolkitApp` (cross-cutting, deliberately not extracted):
`__init__`, `_build_ui`, the status/apply/undo/nav/preset-apply machinery,
the busy/progress + log queue state machines, `_run_stream`, `_user_py`,
`_scroll_body`/`_global_wheel`/`_tip`, and the `cli_*`/`main` entry points.
The refactor is considered complete — no more `_build_*` tab code left in
the monolith.

### (historical) earlier slices

`tuxthrottle.py` was **5513 lines** at the start of the 2026-09-06 session
(8133 at the previous session's start), across these files:

- `tuxthrottle_items.py` — Item/tweaks-engine, ledger, `toolkit_version()`,
  `PROJECT_URL`/`PROJECT_ISSUES_URL`, `_dnf_metadata_age()` (the last three
  moved here mid-refactor specifically to break circular imports — see
  "Gotchas" below).
- `tuxthrottle_gui_widgets.py` — Tooltip, RingGauge, HistoryChart, SidebarNav,
  the whole BIOS dark theme.
- `tuxthrottle_tab_{keyboard,fans,vram,profiles,updates,about}.py` — six tabs
  as mixin classes (`KeyboardTabMixin`, `FanTabMixin`, `VramTabMixin`,
  `ProfilesTabMixin`, `UpdatesTabMixin`, `AboutTabMixin`).

Still inside `tuxthrottle.py`, in build order — current line numbers, will
shift as you extract:

```
613   _build_dashboard_tab       — polling worker thread, dash_queue, RingGauge/HistoryChart wiring
775   _build_battery_health_tab  — has its OWN _build_battery_section helper — check for interleaving
957   _build_power_tab           — calls _build_tdp_section, _build_co_section, _build_nvpl_section,
                                   _build_gpuclock_section, _build_gpumode_section (DEFINED ELSEWHERE,
                                   physically inside the Touchpad stretch — confirmed interleaved by
                                   an earlier research pass this session), _build_autoswitch_section
                                   (also elsewhere)
1261  _build_display_tab         — tiny, just calls _build_refresh_section (defined elsewhere) +
                                   _build_vrr_section
1287  _build_touchpad_tab        — contiguous core, but Display/Power's _build_refresh_section /
                                   _build_battery_section / _build_gpumode_section /
                                   _build_autoswitch_section are physically sandwiched in this
                                   stretch (confirmed, not guessed — a dedicated research pass
                                   mapped this precisely earlier in the session)
1933  _build_category_tab        — data-driven, renders config/tweaks.json entries per category;
                                   used by every "Performance/GPU/Power/Stability/Gaming/KDE/
                                   Software" nav tab, not tab-specific code itself
1968  _build_presets_tab
3677  _build_games_tab           — Setup Games, per-game step cards (has this session's ProtonDB
                                   badge code)
3697  _build_gametools_tab       — the big one: shadercache/steamperf/launchopts/mangohud/
                                   savevault/prefix-relocate boxes, plus this session's new
                                   "Fixes" box. Touched heavily this session, well understood.
4474  _build_diagnostics_tab     — hw-bundle collector, _HW_BUNDLE_FILES, _collect_display_txt
                                   (this session's kscreen-doctor fix lives here)
```

**Recommendation from the earlier research pass**: don't force
Display/Touchpad/Power/Battery into 4 separate files — their helpers are
physically interleaved (confirmed, re-verify current line numbers before
trusting old ones, they've shifted). Group them into one
`tuxthrottle_tab_power_display.py` mixin instead of fighting the existing
layout.

**Cross-cutting helpers still living directly on `ToolkitApp`** (used by
nearly every tab — fine to leave alone, mixins can call `self.foo()` on
these without needing them extracted first; extraction order does NOT
matter for correctness, only for how much of `tuxthrottle.py` shrinks):
`_tip`, `_scroll_body`, `_global_wheel`, `_read_power_state`/
`_write_power_state`, `_run_stream`, `_user_py`, the busy/progress state
machine (`_begin_busy`/`_progress`/`_phase_from_line`/`_poll_busy_queue`),
`_log`/`_poll_log_queue`.

## Gotchas hit this session (read before repeating them)

1. **Circular imports when a tab mixin needs a "utility" function that lives
   in `tuxthrottle.py` itself.** Happened twice: `toolkit_version()` (needed
   by About) and `_dnf_metadata_age()` (needed by Updates) both got moved
   into `tuxthrottle_items.py` instead, since they were already
   self-contained (only needed `BASE_DIR`/`run_cmd3`/stdlib) and
   `tuxthrottle_items.py` has no back-reference to `tuxthrottle.py`. If you
   hit this again with something that ISN'T cleanly self-contained, that's a
   sign it should probably become a `ToolkitApp` method living in whichever
   mixin needs it most, not a module-level function.
2. **`ruff check` after every single extraction, before moving to the next
   one.** Every single extraction this session had at least one missed
   import that only ruff's F821 caught (a stray `shutil.which()`, a
   `BASE_DIR` reference, a `fixlog` import, a `_KBD_PROFILE_COLORS`-style
   class-attribute-via-old-class-name reference like
   `ToolkitApp._FANCURVE_DEFAULT` needing to become
   `FanTabMixin._FANCURVE_DEFAULT` since `@staticmethod`s can't use `self`).
3. **Always diff the extracted block against the original before wiring up
   imports** (`diff /tmp/block.txt <(tail -n +N new_file.py)`). Cheap, and
   it's the only thing that would have caught subtle retyping drift.
4. **Delete the original after extracting — and verify you did.** The
   Profiles duplicate (see above) is exactly what happens when this step is
   skipped. Do the AST audit (step 7 above) after every batch.
5. **This sandbox's own `ttkbootstrap` is broken** (PIL/ImageTk import
   error) — any test touching GUI-toolkit code needs
   `pytest.importorskip("ttkbootstrap", exc_type=ImportError)` and will only
   actually execute on g15 (or wherever ttkbootstrap really works). Don't
   mistake "skipped locally" for "broken."
6. **A headless `r.update()`-loop smoke test is not equivalent to a real
   `mainloop()` launch** for anything involving background threads touching
   Tk. The ProtonDB badge fetch bug (calling `root.after()` from a worker
   thread before the mainloop was pumping) only showed up on a real launch.
   CI's `gui-smoke` job uses the headless style for speed — that's fine for
   catching "does it build at all", not for catching thread-timing bugs.

## Known follow-up (not yet fixed, found ~5 minutes before writing this)

**`kscreen-doctor` is still occasionally SIGABRT-crashing on the g15**, twice
overnight (2026-09-05 23:34 and 2026-09-06 07:00, confirmed via
`coredumpctl`) with `Unit: systemd-suspend.service` — i.e. on **resume from
suspend**, not from anything TuxThrottle's GUI does interactively (that path
was already fixed this session, see item 7 above). Root cause: the
`/usr/lib/systemd/system-sleep/tuxthrottle-reassert` hook calls
`tuxthrottle_profiles.py --user bean reassert` immediately on resume with
**no delay**, which calls `sensors.set_panel_refresh()` →
`sensors._session_cmd(["kscreen-doctor", ...])` — correctly wrapped to run in
the real user's session, but the session (KWin/Wayland) likely isn't fully
back up yet in the first moment after resume. Compare to
`/usr/lib/systemd/system-sleep/tuxthrottle-kbd`, which already `sleep 2`s
before doing anything, for exactly this reason (documented in an existing
project memory about boot/resume race conditions). **Likely fix**: add a
short sleep (or a wait-for-bus-ready check) to `tuxthrottle-reassert` before
it calls `reassert()`, matching the kbd hook's existing pattern. Low
priority (cosmetic — a coredump, not a user-visible failure) but cheap to
fix and worth doing opportunistically.

## Backlog not addressed this session (from earlier brainstorming, still open)

- "Play Session" bundler (snapshot + profile + CSV logging + post-session
  summary as one action).
- Session-history viewer (a real list of past sessions, not just "last
  session" — the CSVs already exist on disk).
- Before/after sensor deltas when applying a Preset.
- Chassis heat-map diagram (larger design lift, Tkinter Canvas art).
- **RGB reacting to thermal-throttle state** — flagged explicitly as
  higher-risk than everything else on this list: this laptop's keyboard
  controller has a **documented hard-hang history** (raw HID spam can
  require a full power-off — see project memory
  `g15-5515-keyboard-single-zone`). If this gets picked up, keep it strictly
  edge-triggered (one write when throttling starts, one when it ends, never
  on a timer loop), reuse the existing lock-protected `set_all()` path in
  `tuxthrottle_kbd.py`, and never touch the user's saved color preference.
  Get explicit user go-ahead before shipping it, not just before starting it.
